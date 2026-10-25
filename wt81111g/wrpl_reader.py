"""`.wrpl` 回放解析（自研）—— 容器 / 包流 / 地面单位米制位置。

**不依赖任何外部解析器，不读游戏安装目录，只读用户指定的回放文件。**

## 为什么自己做

路线测试器原来靠 8111 实时录整局路线，要「A 队 + B 队」两次录制再对轴。
回放文件里**一次就有两队全部单位**、文件头还直接给 `levels/<地图>.bin`，
所以“整局复盘”应该改读回放。上游有 `wrpl-acmi`(Go, AGPL-3.0) 与 `WTACRP`(MIT)
两个成熟实现，但本程序要保留**自己解析**的能力（上游停更或格式变更时不至于断供），
因此按公开格式资料自行实现，并用实测数据交叉验证。

## 格式要点（来源：公开文档 + 本机实测）

容器（定长头 1224 B）::

    0     magic u32 LE      = 0x1000ACE5
    4     version u32 LE
    8     level        char[128]      → 'levels/avg_finland.bin'
    136   levelSettings char[260]
    396   battleType   char[128]      → 'finland_Dom'
    524   environment  char[128]
    652   visibility   char[32]
    684   rezOffset    u32 LE         → 内嵌结算 BLK 的文件偏移
    748   settingsSize u32 LE
    742   serverFlag   u8             → 0x5A = 服务器分片
    settings BLK @ 1226 → +settingsSize → (+8) zstd 帧 → rezOffset 之后是结算 BLK

包流：`[VLQ 长度前缀][包体]`；包体首字节 bit4 置位 = 沿用上一时间戳，
否则包头 6 字节（`[type][0x00][u32 LE 毫秒]`）。

地面位置：`ptype=4` 的 MPI 消息 `0xF073`（客户端）/ `0xF074`（服务器变体）。
体数据 = 4 字节签名 + ULEB 位长 + 对齐 + 体；体内按 uid 区间批量带增量，
增量是 Dagor 的 2-bit RLE + 与 32 槽历史基线逐字节 XOR。

## 已验证（2026-10-25，同一局本地 + 服务器回放）

* 包流**恰好用尽**（零剩余字节），飞机包间隔众数 = 250 ms（4.00 Hz）；
* 地面包 uid 范围 100% 合法（`下界 < 上界 ≤ 2048`），解析失败 **0**；
* 位置样本数与上游 `wrpl-acmi` 相差 1.2%（151,611 vs 149,807）。
"""
from __future__ import annotations

import os
import re
import struct
from collections import defaultdict
from dataclasses import dataclass, field

HEADER_SIZE = 1224
HEADER_PAD = 2
OFF_REZ = 684
OFF_SETTINGS_SIZE = 748
OFF_SERVER_FLAG = 742
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

PACKET_MPI = 4
SIG_GROUND_CLIENT = 0xF073
SIG_GROUND_SERVER = 0xF074
GROUND_UID_MAX = 1 << 11
DELTA_HISTORY = 32
DELTA_TAG_MASK = 0x1FFF
MAX_DELTA_BYTES = 64 << 10
MAX_PACKET_BYTES = 1 << 20
COORD_LIMIT = 36000.0


# ---------------------------------------------------------------- 位流
class BitReader:
    """位流读取：字节内**从最高位开始**（与 Dagor 一致）。

    因此 `u16()/u32()` 不是“小端整数”，而是“按字节顺序读、字节内 MSB 优先”，
    再把字节按小端拼起来 —— 与格式里的 `ReadU16LE` 语义一致。
    """

    __slots__ = ("d", "nbits", "pos")

    def __init__(self, data: bytes, bit: int = 0):
        self.d = data
        self.nbits = len(data) * 8
        self.pos = bit

    def bits(self, n: int) -> int:
        if n <= 0:
            return 0
        if self.pos + n > self.nbits:
            raise EOFError(f"读越界: 需 {n} 位, 只剩 {self.nbits - self.pos}")
        v, d, p = 0, self.d, self.pos
        for _ in range(n):
            v = (v << 1) | ((d[p >> 3] >> (7 - (p & 7))) & 1)
            p += 1
        self.pos = p
        return v

    def byte(self) -> int:
        return self.bits(8)

    def bytes_n(self, n: int) -> bytes:
        return bytes(self.byte() for _ in range(n))

    def skip(self, n: int) -> None:
        if self.pos + n > self.nbits:
            raise EOFError(f"跳过越界: 需 {n} 位, 只剩 {self.nbits - self.pos}")
        self.pos += n

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7

    def u16(self) -> int:
        b = self.bytes_n(2)
        return b[0] | (b[1] << 8)

    def u32(self) -> int:
        b = self.bytes_n(4)
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)

    def f32(self) -> float:
        return struct.unpack("<f", self.bytes_n(4))[0]

    def compressed(self) -> int:
        """LEB128：低 7 位有效，最高位是继续标志。"""
        v, shift = 0, 0
        while True:
            b = self.byte()
            v |= (b & 0x7F) << shift
            if not (b & 0x80):
                return v
            shift += 7
            if shift > 63:
                raise ValueError("变长整数过长")


def bits_le(br: BitReader, n: int) -> int:
    """「≤8 位一段、段内 MSB 优先、低位段先」读 n 位（格式里 5/13 位字段用）。"""
    v, shift, left = 0, 0, n
    while left > 0:
        k = 8 if left >= 8 else left
        v += br.bits(k) << shift
        shift += 8
        left -= k
    return v


# ---------------------------------------------------------------- 容器
def _cstr(buf: bytes, off: int, size: int) -> str:
    return buf[off:off + size].split(b"\x00")[0].decode("utf-8", "replace")


@dataclass
class ReplayInfo:
    """一个回放分片(或单文件)的容器信息。"""
    path: str
    size: int
    level: str              # 'levels/avg_finland.bin'
    battle_type: str        # 'finland_Dom'
    environment: str
    visibility: str
    server: bool
    settings_size: int
    zstd_offset: int | None
    results_offset: int


def parse_header(buf: bytes, path: str = "") -> ReplayInfo:
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"{os.path.basename(path) or '回放'} 只有 {len(buf)} B，"
                         f"比 {HEADER_SIZE} B 的回放头还短")
    magic = struct.unpack_from("<I", buf, 0)[0]
    if magic != 0x1000ACE5:
        raise ValueError(f"不是 .wrpl 回放(magic=0x{magic:08X})")
    settings_size = struct.unpack_from("<I", buf, OFF_SETTINGS_SIZE)[0]
    rez = struct.unpack_from("<I", buf, OFF_REZ)[0]
    settings_end = HEADER_SIZE + HEADER_PAD + settings_size
    search_end = rez if 0 < rez <= len(buf) else len(buf)
    frame = buf.find(ZSTD_MAGIC, settings_end, search_end)
    return ReplayInfo(
        path=path,
        size=len(buf),
        level=_cstr(buf, 8, 128),
        battle_type=_cstr(buf, 396, 128),
        environment=_cstr(buf, 524, 128),
        visibility=_cstr(buf, 652, 32),
        server=(buf[OFF_SERVER_FLAG] == 0x5A),
        settings_size=settings_size,
        zstd_offset=frame if frame >= 0 else None,
        results_offset=rez,
    )


def decompress_stream(buf: bytes, info: ReplayInfo) -> bytes:
    """解压包流（zstd 单帧）。"""
    if info.zstd_offset is None:
        raise ValueError("找不到 zstd 帧（可能是旧版 zlib 回放，暂不支持）")
    try:
        import zstandard                                    # noqa: PLC0415
    except ImportError as exc:                              # pragma: no cover
        raise ValueError("缺少 zstandard 库：pip install zstandard") from exc
    end = info.results_offset or len(buf)
    raw = buf[info.zstd_offset:end]
    return zstandard.ZstdDecompressor().decompressobj().decompress(raw)


def read_vlq_size(buf: bytes, p: int) -> tuple[int, int]:
    b0 = buf[p]
    if b0 & 0x80:
        return b0 & 0x7F, p + 1
    if b0 & 0x40:
        return (((b0 << 8) | buf[p + 1])) ^ 0x4000, p + 2
    if b0 & 0x20:
        return (((b0 << 16) | (buf[p + 1] << 8) | buf[p + 2])) ^ 0x200000, p + 3
    if b0 & 0x10:
        v = (b0 << 24) | (buf[p + 1] << 16) | (buf[p + 2] << 8) | buf[p + 3]
        return v ^ 0x10000000, p + 4
    return struct.unpack_from("<I", buf, p + 1)[0], p + 5


Packet = tuple[int, int, int, int]        # (ptype, 毫秒, 载荷起, 载荷止)


def parse_packets(payload: bytes) -> tuple[list[Packet], int]:
    """切成 `[(ptype, ts_ms, payload_start, payload_end)]`；返回 (包, 消耗字节)。"""
    out: list[Packet] = []
    p, last_ms, n = 0, 0, len(payload)
    while p < n:
        try:
            size, p2 = read_vlq_size(payload, p)
        except IndexError:
            break
        if size == 0 or p2 + size > n:
            break
        h0 = payload[p2]
        if h0 & 0x10:                       # 沿用上一时间戳，包头 2 字节
            ptype, ms, hl = h0 ^ 0x10, last_ms, 2
        else:
            ptype = h0
            ms = struct.unpack_from("<I", payload, p2 + 2)[0]
            last_ms = ms
            hl = 6
        out.append((ptype, ms, p2 + hl, p2 + size))
        p = p2 + size
    return out, p


# ------------------------------------------------------- 2-bit RLE / 增量
def rle0ki_decode(src: bytes) -> bytes:
    """Dagor 的 2-bit RLE 解压（末字节 0x6F/0xDE 标记最后一段的码数）。"""
    if not src:
        raise ValueError("RLE 载荷为空")
    limit = len(src) - 1
    parity = src[limit]
    if parity not in (0x6F, 0xDE):
        raise ValueError(f"RLE 末字节不是 0x6F/0xDE(实际 0x{parity:02X})")
    out = bytearray(MAX_DELTA_BYTES)
    out_bits, code, sup = 0, -1, -1
    for i in range(limit):
        k_last = 1 if (i == limit - 1 and parity == 0x6F) else 2
        for k in range(k_last):
            v0 = (src[i] >> (k * 4)) & 3
            v1 = (src[i] >> (2 + k * 4)) & 3
            num = 0
            if code != 4:
                code = v0
            if code == 0:
                num, v0, v1 = v1 + 3, 0, 0
            elif code == 3:
                num, v0, v1 = v1 + 7, 0, 0
            elif code == 1:
                code, sup, num, v0 = 4, v1, 1, v1
            elif code == 2:
                num, v0 = 1, v1
            elif code == 4:
                code, num = -1, 2
                if sup == 0 and v0 == 0 and v1 == 0:
                    num = 39
            if out_bits + num * 2 > len(out) * 8:
                raise ValueError("RLE 输出超出上限")
            for n in range(num):
                v = v0 if (n & 1) == 0 else v1
                bi, sh = out_bits >> 3, out_bits & 7
                out[bi] = (out[bi] | ((v << sh) & 0xFF)) & 0xFF
                out_bits += 2
    return bytes(out[:(out_bits + 7) >> 3])


def xor_patch(base: bytes, delta: bytes) -> bytes:
    """增量还原：与基线逐字节 XOR，长度不同时按较长者补齐。"""
    n = max(len(base), len(delta))
    if n > MAX_DELTA_BYTES:
        raise ValueError(f"还原体 {n} 字节超出上限")
    out = bytearray(n)
    common = min(len(base), len(delta))
    for i in range(common):
        out[i] = base[i] ^ delta[i]
    out[common:] = delta[common:] if len(delta) > len(base) else base[common:]
    return bytes(out)


class DeltaHistory:
    """单条 uid 的增量基线历史环（32 槽；槽位撞车但标签不同 = 基线过期）。"""

    __slots__ = ("entries", "nos", "valid")

    def __init__(self):
        self.entries: list[bytes | None] = [None] * DELTA_HISTORY
        self.nos: list[int] = [0] * DELTA_HISTORY
        self.valid: list[bool] = [False] * DELTA_HISTORY

    def apply(self, full: bool, pnd: int, npn: int, packed: bytes) -> bytes:
        if full:
            result = packed
        else:
            if pnd >= DELTA_HISTORY:
                raise ValueError(f"增量基线距离 {pnd} 超出历史环")
            base_no = (npn - pnd) & 0xFFFFFFFF
            slot = base_no % DELTA_HISTORY
            if not self.valid[slot] or self.nos[slot] != (base_no & DELTA_TAG_MASK):
                raise ValueError(f"增量基线缺失: 包号 {base_no}(槽 {slot} 已过期)")
            result = xor_patch(self.entries[slot] or b"", rle0ki_decode(packed))
        slot = npn % DELTA_HISTORY
        self.entries[slot] = result
        self.nos[slot] = npn & DELTA_TAG_MASK
        self.valid[slot] = True
        return result


def _clamp_unit(v: float) -> float:
    return -1.0 if v < -1.0 else (1.0 if v > 1.0 else v)


def parse_vehicle_position(data: bytes, has_dir: bool) -> tuple[float, float, float] | None:
    """从记录体取 `(x 东, 高度, z 北)`（米）。无位置返回 None。"""
    if not data:
        return None
    r = BitReader(data)
    r.byte()                                # 记录头
    if not r.bits(1):                       # hasPosition
        return None
    if has_dir:
        r.skip(4 * 32)                      # 方向四元数（暂不使用）
        if r.bits(1):
            r.skip(21 * 32)                 # 历史四元数
    else:
        r.skip(3 * 16)
    r.bits(1)                               # 未解位
    if r.bits(1):                           # full=1: 三个 float32
        x, y, z = r.f32(), r.f32(), r.f32()
    else:                                   # full=0: 量化（1/2^21 映射到 [-1,1]）
        tv1, tv2 = r.u32(), r.u32()
        x = _clamp_unit((tv1 >> 10) * 4.7683727e-07 - 1.0) * COORD_LIMIT
        y = ((((tv1 & 0x3FF) << 10) | (tv2 >> 22)) * 0.007629402) - 100.0
        z = _clamp_unit((tv2 & 0x3FFFFF) * 4.7683727e-07 - 1.0) * COORD_LIMIT
    for v in (x, y, z):
        if v != v or abs(v) > 1e6:
            return None
    return x, y, z


def _skip_turrets(g: BitReader, count: int) -> None:
    for _ in range(count):
        if not g.bits(1):
            continue
        g.skip(8)
        g.skip(32)
        g.skip(32)
        g.skip(g.bits(8) * 16)


def ground_parse_packet(payload: bytes, hist: dict[int, DeltaHistory],
                        start: int = 0, end: int | None = None
                        ) -> list[tuple[int, float, float, float]]:
    """解析一个地面同步包，返回 `[(uid, x, 高度, z)]`（米）。"""
    out: list[tuple[int, float, float, float]] = []
    view = payload[start:end]
    r = BitReader(view, 32)                 # 跳过 4 字节签名
    body_bits = r.compressed()
    r.align()
    if body_bits <= 0 or body_bits > MAX_PACKET_BYTES * 8:
        raise ValueError(f"体比特长度异常: {body_bits}")
    body = r.bytes_n((body_bits + 7) // 8)

    g = BitReader(body)
    lower, upper = g.u16(), g.u16()
    g.u32()                                 # 同步时刻（包时间戳够用）
    g.u16()                                 # 可靠性序号
    if not (lower < upper <= GROUND_UID_MAX):
        raise ValueError(f"uid 范围异常: [{lower}, {upper})")

    for uid in range(lower, upper):
        if not g.bits(1):                   # hasUnit
            continue
        g.bits(1)
        if g.bits(1):
            g.skip(4)
        v2, v3 = g.bits(1), g.bits(1)
        if v2 and v3:
            continue
        g.bits(1)
        has_dir = g.bits(1)
        turrets = g.byte()
        if not g.bits(1):
            raise ValueError(f"uid {uid} 用了未压缩体（不支持）")
        full = g.bits(1)
        pnd = bits_le(g, 5)
        npn = bits_le(g, 13)
        size = g.compressed()
        g.align()
        if size <= 0 or size > MAX_DELTA_BYTES:
            raise ValueError(f"uid {uid} 增量体长度异常: {size}")
        packed = g.bytes_n(size)

        hist_item = hist.get(uid)
        if hist_item is None:
            hist_item = hist[uid] = DeltaHistory()
        pos = parse_vehicle_position(hist_item.apply(bool(full), pnd, npn, packed),
                                     bool(has_dir))
        if pos is not None:
            out.append((uid, pos[0], pos[1], pos[2]))

        if g.bits(1):                       # point4
            g.skip(4 * 32)
        if g.bits(1):                       # 炮塔
            _skip_turrets(g, turrets)
        if g.bits(1):                       # 传感器
            if g.bits(1):
                n = g.u32()
                if n > 1 << 20:
                    raise ValueError(f"uid {uid} seeker 长度异常: {n}")
                g.skip(n)
        g.align()
    return out


# ---------------------------------------------------------------- 高层入口
@dataclass
class ReplayData:
    """一局回放解析出来的全部地面位置。"""
    level: str = ""                 # 'avg_finland'（已去掉 levels/ 与 .bin）
    battle_type: str = ""
    environment: str = ""
    visibility: str = ""
    server: bool = False
    parts: int = 1
    source: str = ""                # 第一个分片的路径（用来从文件名取录制时刻）
    packets: int = 0
    failed_packets: int = 0
    # uid -> [(秒, x 东, 高度, z 北)]，已按时间排序
    units: dict[int, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    t_first: float = 0.0
    t_last: float = 0.0

    @property
    def sample_count(self) -> int:
        return sum(len(v) for v in self.units.values())


def _level_stem(level: str) -> str:
    base = level.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-4] if base.lower().endswith(".bin") else base


# 只读头这么多就够（settings 可能更长，见 read_header_file 的二次读取）
HEADER_PROBE = 8192
MAX_PARTS = 400                 # 一局回放不可能有这么多分片
MAX_TOTAL_MB = 3072             # 误选大目录的硬上限（3 GB）


def _natural_key(name: str):
    """文件名自然排序: 让 `part2` 排在 `part10` 前面(纯字符串排序会反过来)。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def read_header_file(path: str) -> ReplayInfo:
    """只读回放头的必要部分（不整份读进内存）。

    扫描服务器回放目录时要把每个文件都过一遍 —— 整份读会在用户**误选大目录**时
    瞬间吃满内存（2026-10-25 用户反馈：卡住 + 内存持续上升）。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(HEADER_PROBE)
        if len(head) >= OFF_SETTINGS_SIZE + 4:
            st = struct.unpack_from("<I", head, OFF_SETTINGS_SIZE)[0]
            need = HEADER_SIZE + HEADER_PAD + min(st, 4 << 20) + 4096
            if need > len(head) and need <= size:
                fh.seek(0)
                head = fh.read(need)
    info = parse_header(head, path)
    info.size = size            # parse_header 里的 size 是 len(buf)，这里换成真实大小
    return info


def _scan_parts(path: str, *, note=None,
                expect_server: bool | None = None
                ) -> tuple[list[str], list[ReplayInfo]]:
    """列出一个文件夹里的全部 `.wrpl`（= 一局回放的各分片）。

    **一个文件夹就是一局** —— 用户自己会把同一局的分片放在一起，所以这里不再
    猜“这个目录里是几局”（以前只是 `sorted(listdir())`，把目录里所有回放都当
    一局的分片**整份读进内存**，用户误选大目录时就卡死 + 内存爆满）。

    现在只做两件事：① 只读文件头（不整份加载）；② 数量/总大小超限就拦住，
    免得再把一个装满录像的大目录当成一局吞下去。
    """
    names = sorted((n for n in os.listdir(path) if n.lower().endswith(".wrpl")),
                   key=_natural_key)
    if not names:
        raise ValueError("这个文件夹里没有 .wrpl 文件")
    if len(names) > MAX_PARTS:
        raise ValueError(
            f"这个文件夹里有 {len(names)} 个 .wrpl（超过 {MAX_PARTS} 个）。\n\n"
            "这不像是一局的录像分片 —— 请把**同一局**的录像单独放一个文件夹再选。")

    files: list[str] = []
    infos: list[ReplayInfo] = []
    total = 0
    for i, n in enumerate(names, 1):
        fp = os.path.join(path, n)
        if not os.path.isfile(fp):
            continue
        if note is not None and (i == 1 or i % 10 == 0 or i == len(names)):
            note(f"检查分片 {i}/{len(names)} …", 0.0)
        try:
            info = read_header_file(fp)
        except Exception as exc:                 # noqa: BLE001 - 转成用户能懂的话
            raise ValueError(f"{n} 不是有效的回放文件：{exc}") from exc
        files.append(fp)
        infos.append(info)
        total += info.size
        if total > MAX_TOTAL_MB << 20:
            raise ValueError(
                f"这些文件加起来已经超过 {MAX_TOTAL_MB} MB —— 不像是一局的录像。\n\n"
                "请把**同一局**的录像单独放一个文件夹再选。")
    if not files:
        raise ValueError("这个文件夹里没有可读的 .wrpl 文件")

    # 服务器/本地标志必须与按钮相符（本地回放和服务器分片是两套读法）
    is_server = infos[0].server
    if expect_server is True and not is_server:
        raise ValueError(
            "这些是**本地**回放（游戏内「保存回放」得到的）。\n\n"
            "请改用「从录像中获取路线(本地)」按钮。")
    if expect_server is False and is_server:
        raise ValueError(
            "这些是**服务器**回放分片。\n\n"
            "请改用「从录像中获取路线(服务器)」并选整个文件夹。")
    return files, infos


class ImportCancelled(Exception):
    """用户主动取消（进度框点「取消」）。"""


def load_replay(path: str, *, progress=None, expect_server: bool | None = None
                ) -> ReplayData:
    """读一个本地回放文件，或一个服务器回放分片目录。

    * `progress(msg, frac)` —— `frac` 是 0~1 的总进度（None 表示不确定）；
    * `expect_server` —— True 要求服务器回放、False 要求本地回放、None 不限。
    """

    def note(msg: str, frac: float | None = None) -> None:
        if progress:
            progress(msg, frac)

    if os.path.isdir(path):
        files, infos = _scan_parts(path, note=note,
                                   expect_server=expect_server)
    else:
        info = read_header_file(path)
        if expect_server is True and not info.server:
            raise ValueError(
                "这是**本地**回放（游戏内「保存回放」得到的）。\n\n"
                "请改用「从录像中获取路线(本地)」按钮。")
        if expect_server is False and info.server:
            raise ValueError(
                "这是**服务器**回放的一个分片。\n\n"
                "请改用「从录像中获取路线(服务器)」，并选分片所在的**文件夹**"
                "（单个分片只含 1/N 的对局数据）。")
        files, infos = [path], [info]

    data = ReplayData(parts=len(files))
    data.source = files[0] if files else ""
    nfiles = len(files)
    hist: dict[int, DeltaHistory] = {}
    raw: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)

    for idx, fp in enumerate(files, 1):
        note(f"读取分片 {idx}/{nfiles} …", (idx - 1) / nfiles)
        with open(fp, "rb") as fh:
            buf = fh.read()
        info = infos[idx - 1]
        if idx == 1:
            data.level = _level_stem(info.level)
            data.battle_type = info.battle_type
            data.environment = info.environment
            data.visibility = info.visibility
            data.server = info.server
        payload = decompress_stream(buf, info)
        packets, _used = parse_packets(payload)
        data.packets += len(packets)
        total = len(packets)
        step = max(1, total // 100)          # 每个分片最多报 100 次进度
        for i, (ptype, ms, s, e) in enumerate(packets):
            if i % step == 0:
                note(f"解析分片 {idx}/{nfiles} · 包 {i:,}/{total:,}",
                     ((idx - 1) + i / max(1, total)) / nfiles)
            if ptype != PACKET_MPI or e - s < 12:
                continue
            mid = payload[s + 2] | (payload[s + 3] << 8)
            if mid not in (SIG_GROUND_CLIENT, SIG_GROUND_SERVER):
                continue
            try:
                rows = ground_parse_packet(payload, hist, s, e)
            except (ValueError, EOFError, IndexError):
                data.failed_packets += 1
                continue
            t = ms / 1000.0
            for uid, x, y, z in rows:
                raw[uid].append((t, x, y, z))
        # 逐个分片释放（单份服务器回放可能几百 MB，误选大目录时就是内存爆满）
        del buf, payload, packets

    for uid, rows in raw.items():
        rows.sort()
        data.units[uid] = rows
    if raw:
        all_t = [rows[0][0] for rows in raw.values()] + [rows[-1][0] for rows in raw.values()]
        data.t_first, data.t_last = min(all_t), max(all_t)
    return data
