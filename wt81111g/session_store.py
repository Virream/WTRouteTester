"""会话文件(`.wts`): **一局一个文件**, 二进制点数据 + 小 JSON 头部。

为什么要它(用户 2026-10-26 定下的规模目标: 存 500~600 局):

* 一局回放实测约 **80 万点**。五百局 = 4 亿点 —— 单文件 `routes.json` 会是
  **5~6 GB**, 而且**每次保存都要重写整个文件**(改个地图名也要等十几分钟);
  全量常驻内存则要 **32 GB**。两条路都走不通。
* 所以拆成"**一局一文件 + 用到哪局才读哪局**": 启动只读每个文件开头几 KB 的
  头部(500 局 ≈ 4 MB, 几十毫秒), 保存只重写变化的那一局(3~5 MB, 一百多毫秒)。

## 文件布局

```
  偏移 0     b"WTS1"            4 字节魔数(读到别的就当损坏, 跳过)
  偏移 4     u32 小端           head_len
  偏移 8     u32 小端           dir_len
  偏移 12    head               UTF-8 JSON —— **会话级**元数据(几百字节)
  偏移 12+H  dir                UTF-8 JSON —— **每条线路**的元数据 + 点块目录
  之后        payload            各条线路的点块(**每条一个独立 zstd 帧**)
```

为什么要拆成 `head` 与 `dir` 两段(用户 2026-10-26 的 500~600 局目标):
一局回放有 200 多条线路, 每条线 20 来个字段 —— 全塞在 head 里是 30~40 KB/局。
500 局于是变成“启动时读 20 MB JSON + 建 10 万个对象” —— 实测 **789 ms**,
而侧栏在最上面两层(地图 → 模式 → 局)根本用不到线路清单。
现在启动只读 head(几百字节/局, 500 局实测 **~40 ms**), 线路清单在**展开那一局**
时才读。

**每条线路一个独立 zstd 帧**: 可以只读需要的那一条(悬停统计 / 导出选中 /
将来按玩家筛选), 也天然能并行解。zstd 帧带**内容校验和**(`write_checksum`),
文件被改坏时解压会抛错而不是悄悄给出垃圾数据。

## 点的编码(列式)

一条线路 = 若干列 `float32` 数组首尾相接:

```
  t[n]  x[n]  y[n]  speed[n]            ← 一定有
  dx[n] dy[n]                            ← has_dir 时(单条录制才记朝向)
  hold[n]                                ← has_hold 时(静止段折叠时长)
```

为什么用 `float32` 而不是更省的 uint16 量化: 数据源本身已经 `round(x, 5)`
(1e-5, 2048 m 的地图上约 2 cm), 而 float32 在 0~1 附近的分辨率约 6e-8 ——
**无损**, 且 `frombuffer` 是纯 C, 不需要逐点转换。以后真缺磁盘再加一个
`fmt` 版本做量化(uint16 可以做到 10 字节/点), 头部的 `fmt` 字段就是为此留的。

## 为什么不用数据库

SQLite 能给"行级写 + 事务 + 查询", 但本项目是**几千行、每行一个几 MB 的
二进制块**, 查询只在内存里做(500 局全在内存索引里), 用不上索引能力; 反而要多管
schema 迁移、WAL 与双份真相。而"一局一文件 + 原子替换"天然把一致性限定在单个
文件内, 坏文件跳过即可自愈 —— 见 `scan_dir`。
"""
from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
from array import array
from operator import attrgetter

try:                                    # zstandard 是回放解析的既有依赖
    import zstandard as zstd
except Exception:                       # pragma: no cover - 兜底用 zlib
    zstd = None
    import zlib

log = logging.getLogger("session_store")

SUFFIX = ".wts"
MAGIC = b"WTS1"
FORMAT_VERSION = 1
HEADER_MAX = 64 * 1024 * 1024        # 头部上限(防御损坏文件里的离谱长度)

# 点列的格式位(头部每条线路的 `fmt`)
FMT_BASE = 1          # t/x/y/speed 各 float32
FMT_DIR = 2           # + dx/dy 各 float32(单条录制才记朝向)
FMT_HOLD = 4          # + hold 各 float32(全局录制的静止段时长)

_CMP_LEVEL = 3        # zstd 级别: 一局 12 MB 原始数据, 3 级约 2.5~5 MB / 二三十毫秒
_compressor = zstd.ZstdCompressor(level=_CMP_LEVEL, write_checksum=True) if zstd else None
_decompressor = zstd.ZstdDecompressor() if zstd else None

# 文件名里只留安全字符(route_id 是 uuid 前缀, 但测试/手工数据可能是任意串)
_SAFE = re.compile(r"[^0-9A-Za-z_.-]+")

# 列打包用(见 `pack_points`)
_AG_T = attrgetter("t")
_AG_X = attrgetter("x")
_AG_Y = attrgetter("y")
_AG_SP = attrgetter("speed")
_AG_DX = attrgetter("dx")
_AG_DY = attrgetter("dy")
_AG_HOLD = lambda p: getattr(p, "hold", 0.0) or 0.0      # noqa: E731 - RoutePoint 没有 hold

# 同一进程里会话文件可能被后台保存线程写、被界面线程读 → 用一个模块级锁串行化。
# 粒度粗, 但每次操作只有几毫秒(读)到几百毫秒(写), 不构成瓶颈。
_io_lock = threading.RLock()


def safe_name(route_id: str) -> str:
    """`route_id` → 可当文件名的安全串。"""
    name = _SAFE.sub("_", (route_id or "").strip())
    return name[:80] or "unnamed"


def compress(raw: bytes) -> bytes:
    """压缩一个点块(失败退回 zlib, 只为不丢数据)。"""
    if _compressor is not None:
        return _compressor.compress(raw)
    return zlib.compress(raw, 1)


def decompress(blob: bytes, raw_len: int) -> bytes:
    """解压一个点块。`raw_len` 是未压缩长度(调用方解开的数据总是已知的)。"""
    if _decompressor is not None:
        return _decompressor.decompress(blob, max_output_size=max(1, raw_len))
    return zlib.decompress(blob)


# ---------------------------------------------------------------------- 点块
def pack_points(points, *, fmt: int) -> bytes:
    """`[TrackPoint/RoutePoint...]` → 列式 `float32` 原始字节。

    用 `array("f", map(attrgetter(...), points))` 而不是逐点 `append`: `array` 吃可迭代
    对象时是 **C 级迭代**, 80 万点实测比 Python for 循环快 2~3 倍。
    """
    n = len(points)
    if not n:
        return b""
    cols = [array("f", map(_AG_T, points)),
            array("f", map(_AG_X, points)),
            array("f", map(_AG_Y, points)),
            array("f", map(_AG_SP, points))]
    if fmt & FMT_DIR:
        cols += [array("f", map(_AG_DX, points)),
                 array("f", map(_AG_DY, points))]
    if fmt & FMT_HOLD:
        cols.append(array("f", map(_AG_HOLD, points)))
    return b"".join(col.tobytes() for col in cols)


def unpack_points(raw: bytes, *, fmt: int, factory) -> list:
    """列式字节 → 点列表。`factory(t, x, y, speed, dx, dy, hold)` 造一个点。"""
    n = _point_count(len(raw), fmt)
    if n <= 0:
        return []
    step = 4 * n
    off = 0

    def col() -> array:
        nonlocal off
        arr = array("f")
        arr.frombytes(raw[off:off + step])
        off += step
        return arr

    ts, xs, ys, sp = col(), col(), col(), col()
    dxs = dys = hos = None
    if fmt & FMT_DIR:
        dxs, dys = col(), col()
    if fmt & FMT_HOLD:
        hos = col()
    out = []
    append = out.append
    for i in range(n):
        append(factory(ts[i], xs[i], ys[i], sp[i],
                       dxs[i] if dxs is not None else 0.0,
                       dys[i] if dys is not None else 0.0,
                       hos[i] if hos is not None else 0.0))
    return out


def _point_count(raw_len: int, fmt: int) -> int:
    """按列数反算点数(数据被截断时返回能整除的那部分, 不抛异常)。"""
    per = 4 * (4 + (2 if fmt & FMT_DIR else 0) + (1 if fmt & FMT_HOLD else 0))
    return raw_len // per if per else 0


def raw_len_for(n: int, fmt: int) -> int:
    """n 个点需要多少字节(和 `pack_points` 对齐)。"""
    per = 4 * (4 + (2 if fmt & FMT_DIR else 0) + (1 if fmt & FMT_HOLD else 0))
    return per * max(0, n)


# ---------------------------------------------------------------------- 读写
def write_session(path: str, head: dict, blocks: list[bytes],
                 tracks: list[dict] | None = None) -> None:
    """把一局写成文件(**临时文件 + `os.replace`** = 单文件原子)。

    `tracks` 是**每条线路**的元数据(只在全局录制时有); 它们的 `off` / `clen`
    会就地补上(相对 payload 起点), 所以 `blocks` 要和它同序。
    单条录制没有 `tracks` —— 它的点元数据就一个, 直接放在 head 里。
    """
    off = 0
    for entry, blob in zip(tracks or [], blocks):
        entry["off"] = off
        entry["clen"] = len(blob)
        off += len(blob)
    dir_raw = (json.dumps(tracks, ensure_ascii=False, separators=(",", ":"))
               .encode("utf-8")) if tracks else b""
    head_raw = json.dumps(head, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    if len(head_raw) > HEADER_MAX or len(dir_raw) > HEADER_MAX:   # 防御损坏
        raise ValueError(f"会话头部过大: {len(head_raw)} / {len(dir_raw)} 字节")
    tmp = path + ".tmp"
    with _io_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(MAGIC)
            fh.write(struct.pack("<II", len(head_raw), len(dir_raw)))
            fh.write(head_raw)
            fh.write(dir_raw)
            for blob in blocks:
                fh.write(blob)
        os.replace(tmp, path)


def read_header(path: str) -> dict | None:
    """只读**会话级**元数据(几百字节)。损坏/不是本格式 → None(调用方跳过)。"""
    try:
        with _io_lock, open(path, "rb") as fh:
            if fh.read(4) != MAGIC:
                return None
            raw = fh.read(8)
            if len(raw) != 8:
                return None
            head_len, dir_len = struct.unpack("<II", raw)
            if head_len <= 2 or head_len > HEADER_MAX:
                return None
            body = fh.read(head_len)
    except OSError:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["_dir_len"] = dir_len          # 供 `read_tracks` / `read_block` 定位
    return data


def read_tracks(path: str, head: dict) -> list:
    """读**每条线路**的元数据清单(展开那一局时才调)。"""
    dir_len = int(head.get("_dir_len") or 0)
    if dir_len <= 0:
        return []
    try:
        with _io_lock, open(path, "rb") as fh:
            fh.seek(4)                       # 跳过魔数
            head_len = struct.unpack("<I", fh.read(8)[:4])[0]
            fh.seek(12 + head_len)
            body = fh.read(dir_len)
    except OSError as exc:
        log.warning("读取线路清单失败 %s: %s", path, exc)
        return []
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("线路清单损坏: %s", path)
        return []
    return [x for x in data or [] if isinstance(x, dict)] if isinstance(data, list) else []


def read_block(path: str, off: int, clen: int, raw_len: int,
               dir_len: int = 0) -> bytes | None:
    """读一条线路的点块并解压。失败返回 None(这条线当作没有点)。"""
    if clen <= 0 or raw_len <= 0:
        return b""
    try:
        with _io_lock, open(path, "rb") as fh:
            fh.seek(4)                       # 跳过魔数, 读到 head_len / dir_len
            head_len, dlen = struct.unpack("<II", fh.read(8))
            fh.seek(12 + head_len + (dlen if dir_len is None else dir_len)
                    + max(0, off))
            blob = fh.read(clen)
    except OSError as exc:
        log.warning("读取线路点块失败 %s: %s", path, exc)
        return None
    if len(blob) != clen:
        log.warning("线路点块被截断 %s (要 %d, 只有 %d)", path, clen, len(blob))
        return None
    try:
        return decompress(blob, raw_len)
    except Exception as exc:      # noqa: BLE001 - zstd 的各种解压错误都在这里
        log.warning("解压线路点块失败 %s: %s", path, exc)
        return None


def scan_dir(dir_path: str, *, limit: int | None = None) -> list[tuple[dict, str]]:
    """扫目录, 返回 `[(头部, 路径)]` —— 坏文件**跳过并在日志里留一行**, 不中断。

    索引就是目录本身: 没有第二份真相, 也就不存在"索引和文件对不上"的自愈问题。
    """
    out: list[tuple[dict, str]] = []
    try:
        names = sorted(os.listdir(dir_path))
    except FileNotFoundError:
        return out
    except OSError as exc:
        log.warning("扫描会话目录失败: %s", exc)
        return out
    bad = 0
    for name in names:
        if not name.endswith(SUFFIX):
            continue
        path = os.path.join(dir_path, name)
        head = read_header(path)
        if head is None:
            bad += 1
            log.warning("跳过损坏的会话文件: %s", name)
            continue
        out.append((head, path))
        if limit is not None and len(out) >= limit:
            break
    if bad:
        log.warning("会话目录里有 %d 个损坏文件被跳过", bad)
    return out


def session_path(dir_path: str, route_id: str) -> str:
    """会话文件的规范路径(文件名 = 清洗过的 route_id)。"""
    return os.path.join(dir_path, safe_name(route_id) + SUFFIX)


def remove_session(dir_path: str, route_id: str) -> bool:
    """删除一局的会话文件(不存在返回 False)。"""
    path = session_path(dir_path, route_id)
    try:
        with _io_lock:
            os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("删除会话文件失败 %s: %s", path, exc)
        return False
