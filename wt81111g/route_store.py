"""跑图线路记录持久化。

8111 **不提供地图名称**(只有几何数据与地图图片), 因此用地图图片内容的
指纹(sha1 前 12 位)作为地图唯一键; 显示名默认自动编号, 支持用户重命名。

模式: 8111 也不给模式名, 但 `/map_obj.json` 里的 `capture_zone`(战区)能决定它 ——
详见 `zones.py`。同一张图会有多种模式, 所以:

* 每条轨迹记录下当时的 **战区数量 + 战区坐标串(指纹)+ 模式名**;
* `map_zones` 按地图保存见过的战区布局, 战局结束后依旧能在地图上画出战区;
* 同一模式还会有多个「版本」(实测: 战区控制/攻陷分大地图与小地图、战区争夺的
  单战区会从 3 个位置里随机), 但它们**不再分开归类** —— 侧栏只按战区数量分成
  战区争夺(1)/战区攻陷(2)/战区控制(3)。

数据文件(2026-10-26 起, 为了支持 500~600 局):

* `<data_dir>/routes.json`(**普通 JSON, 只有几十 KB**)—— 地图名 / 隐藏标记 /
  已知地图 / 战区布局 / 地图校准 / 关卡代号关联这些**小数据**;
* `<data_dir>/routes.sessions/<route_id>.wts` —— **一局一个二进制会话文件**
  (格式见 `session_store.py`: 小 JSON 头部 + 每条线路一个 zstd 压缩的点块)。

为什么拆: 一局回放实测约 80 万点。五百局 = 4 亿点 —— 单文件会到 5~6 GB, 而且
**改个地图名都要重写整个文件**; 全量常驻内存则要几十 GB。

现在:
* 启动只读每个会话文件开头几 KB 的头部(500 局几十毫秒, 且**与总点数无关**);
* 保存只重写**变过的那一局**(3~5 MB, 一百多毫秒), 改名字只写几 KB 的 meta;
* 点数据**用到哪局才读哪局**(见 `LazyPoints`), 侧栏/悬停要的是元数据
  (点数 / 首末时刻 / 最高速), 头部里已经带着, 不用为此读点。

`map_calib` 是「回放里的世界米 → 底图像素」的变换(见 `map_calib.py`)。
**只有回放导入的线路需要它** —— 战局内录的轨迹本来就是 8111 的归一化坐标,
而回放给的是米制世界坐标, 没有这个矩形就只能拿“本局观测范围”去凑比例。

`globals` 是「全局录制」条目(整局所有玩家的线路, 分 A/B 两队), 与 `routes`
是两类记录: 前者挂在侧栏**存放路线的那一级**, 但不可当单条轨迹回放。
"""
from __future__ import annotations

import hashlib
import gzip
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from . import session_store
from .config import data_dir
from .garage import GARAGE_MAP_SHA1
from .map_calib import MAP_KEY_PREFIX, MapCalib
from .zones import mode_name as zone_mode_name
from .zones import signature as zones_signature
from .zones import spawns_to_json, zones_from_signature, zones_to_json

log = logging.getLogger("route_store")

# 后台写盘: 收到保存请求后先等这么久再动手, 把连续请求(比如"标记 + 结束录制"
# 连着两下)合并成一次写。80 ms 对人眼是瞬时的, 但不至于每次点儿都写一遍硬盘。
SAVE_DEBOUNCE_S = 0.08

# **测试/探针用**: 置 True 时 `save()` 改回同步写盘。
# 它们有大量“写完立刻重读”的断言, 不置的话每处都得自己叫 `save_now()`。
SYNC_SAVE = False


class _Cols:
    """一条线的点, 以**列数组**常驻(只读); 访问单个点时才现造对象。

    `make(t, x, y, speed, dx, dy, hold)` 决定造 `RoutePoint` 还是 `TrackPoint`。
    为什么要 `float()` 包一层: numpy 的 `float32` **不能**被 `json.dumps` 序列化,
    万一有人把点拿去写 JSON(旧格式导出/调试)会直接抛错。
    """
    __slots__ = ("fmt", "arr", "n", "make")

    def __init__(self, fmt: int, arr, n: int, make):
        self.fmt, self.arr, self.n, self.make = fmt, arr, n, make

    def at(self, i: int):
        a = self.arr
        if i < 0:
            i += self.n
        if not 0 <= i < self.n:
            raise IndexError(f"点下标越界: {i} (共 {self.n} 点)")
        dx = dy = hold = 0.0
        if self.fmt & session_store.FMT_DIR:
            dx, dy = float(a[4][i]), float(a[5][i])
        if self.fmt & session_store.FMT_HOLD:
            hold = float(a[-1][i])          # 最后一列一定是 hold
        return self.make(float(a[0][i]), float(a[1][i]), float(a[2][i]),
                         float(a[3][i]), dx, dy, hold)

    def to_list(self) -> list:
        """展开成真的点对象(**要改点之前**才调; 44 万点约 0.2 s)。"""
        cols = [c.tolist() if hasattr(c, "tolist") else list(c) for c in self.arr]
        ts, xs, ys, sp = cols[0], cols[1], cols[2], cols[3]
        if self.fmt & session_store.FMT_DIR:
            dxs, dys = cols[4], cols[5]
        else:
            dxs = dys = None
        hos = cols[-1] if self.fmt & session_store.FMT_HOLD else None
        make = self.make
        if dxs is None and hos is None:
            return [make(t, x, y, s, 0.0, 0.0, 0.0)
                    for t, x, y, s in zip(ts, xs, ys, sp)]
        if hos is None:
            return [make(t, x, y, s, dx, dy, 0.0)
                    for t, x, y, s, dx, dy in zip(ts, xs, ys, sp, dxs, dys)]
        if dxs is None:
            return [make(t, x, y, s, 0.0, 0.0, h)
                    for t, x, y, s, h in zip(ts, xs, ys, sp, hos)]
        return [make(t, x, y, s, dx, dy, h)
                for t, x, y, s, dx, dy, h in zip(ts, xs, ys, sp, dxs, dys, hos)]


class LazyPoints(list):
    """**占位点列表**: 第一次被访问时才真的从会话文件读出来。

    为什么要继承 `list` 而不是“去掉字段改成属性”: 全项目有 100 多处
    `rec.points[...]` / `len(...)` / `for p in ...`, 继承 `list` 之后这些**一行都不用改**,
    也就不会出现“用到点却忘了先加载”(那类 bug 表现为线路画不出来, 极难查)。

    **两种存储模式**(2026-10-26, P3):

    * **列模式**(从会话文件读出来): 数据是 `(列, n)` 的 `float32` 数组, 常驻内存,
      **不造对象**。实测 44 万点一局: 逐点造 `TrackPoint` 要 223 ms, 列视图 0.2 ms。
      单个点用 `points[i]` 现造; 要**整批**遍历/修改先 `_flat()` 物化(见 `__iter__`)。
    * **对象模式**(录制中/新建): 就是普通 list 行为。

    快路径给热点消费者用: `columns()` 拿到列数组后, 绘制分桶、回放时间表、
    包围盒、最高速、保存打包都不用物化(见 `route_map_view.build_speed_segments`、
    `session_store.pack_points`)。

    `loaded` 供界面判断: 侧栏/悬停要的是“点数、首末时刻、最高速”这几项元数据,
    提前存在记录里, 不用为了显示它们把几十万点读进来。
    """

    __slots__ = ("_loader", "_col", "loaded")

    def __init__(self, loader):
        super().__init__()
        self._loader = loader
        self._col: _Cols | None = None
        self.loaded = False

    def _ensure(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        loader, self._loader = self._loader, None
        try:
            data = loader() if loader is not None else None
        except Exception:            # noqa: BLE001 - 读坏文件不能把界面炸掉
            log.exception("延迟加载线路点失败")
            data = None
        if isinstance(data, _Cols):
            self._col = data         # 列存储: **不搬对象**
        elif data:
            list.extend(self, data)

    # ---- 列快路径 / 物化
    def columns(self):
        """`(fmt, 列数组)`; 不是列存储(或没加载出东西)返回 None。

        调用方**不要缓存**这个返回值 —— 点一旦被改就会物化成对象列表。
        """
        self._ensure()
        col = self._col
        return (col.fmt, col.arr) if col is not None else None

    def point_count(self) -> int:
        """点数(列存储时**不物化**)。"""
        self._ensure()
        return self._n()

    def _n(self) -> int:
        col = self._col
        return col.n if col is not None else list.__len__(self)

    def _flat(self) -> None:
        """列 → 真点对象。**要改点/排序/比较之前必须调**(否则改的是临时对象)。"""
        col = self._col
        if col is not None:
            self._col = None
            list.extend(self, col.to_list())

    # ---- 读
    def __len__(self):
        self._ensure()
        return self._n()

    def __iter__(self):
        self._ensure()
        # 遍历一律物化: 全项目有大量 `for p in points: p.x = ...`(换校准重算),
        # 若给的是临时对象, 改了不生效且**没有任何报错** —— 那是最难查的一类 bug。
        self._flat()
        return list.__iter__(self)

    def __getitem__(self, key):
        self._ensure()
        col = self._col
        if col is None:
            return list.__getitem__(self, key)
        if isinstance(key, slice):
            return [col.at(i) for i in range(*key.indices(col.n))]
        return col.at(key)

    def __contains__(self, item):
        self._ensure()
        self._flat()
        return list.__contains__(self, item)

    def __reversed__(self):
        self._ensure()
        self._flat()
        return list.__reversed__(self)

    def __eq__(self, other):
        self._ensure()
        self._flat()
        return list.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        self._ensure()
        self._flat()
        return list.__repr__(self)

    def __bool__(self):
        self._ensure()
        return self._n() > 0

    def __add__(self, other):
        self._ensure()
        self._flat()
        return list.__add__(self, other)

    def __radd__(self, other):
        self._ensure()
        self._flat()
        return list.__radd__(self, other)

    def __mul__(self, n):
        self._ensure()
        self._flat()
        return list.__mul__(self, n)

    def __rmul__(self, n):
        self._ensure()
        self._flat()
        return list.__rmul__(self, n)

    def copy(self):
        self._ensure()
        self._flat()
        return list.copy(self)

    def count(self, item):
        self._ensure()
        self._flat()
        return list.count(self, item)

    def index(self, *args):
        self._ensure()
        self._flat()
        return list.index(self, *args)

    # ---- 写(先物化, 否则会把还没展开的列数据冲掉)
    def __setitem__(self, key, value):
        self._ensure()
        self._flat()
        list.__setitem__(self, key, value)

    def __delitem__(self, key):
        self._ensure()
        self._flat()
        list.__delitem__(self, key)

    def append(self, item):
        self._ensure()
        self._flat()
        list.append(self, item)

    def extend(self, items):
        self._ensure()
        self._flat()
        list.extend(self, items)

    def insert(self, index, item):
        self._ensure()
        self._flat()
        list.insert(self, index, item)

    def pop(self, *args):
        self._ensure()
        self._flat()
        return list.pop(self, *args)

    def remove(self, item):
        self._ensure()
        self._flat()
        list.remove(self, item)

    def clear(self):
        # 清空不需要物化: 列数据直接丢掉, 结果一样
        self._ensure()
        self._col = None
        list.clear(self)

    def sort(self, *args, **kwargs):
        self._ensure()
        self._flat()
        list.sort(self, *args, **kwargs)

    def reverse(self):
        self._ensure()
        self._flat()
        list.reverse(self)

    def __iadd__(self, other):
        self.extend(other)
        return self

    def __imul__(self, n):
        self._ensure()
        self._flat()
        return list.__imul__(self, n)


def _vmax_of(pts) -> float:
    """一条线里的最高速。列存储时直接取第 4 行最大值(**不物化**)。"""
    cols_of = getattr(pts, "columns", None)
    if callable(cols_of):
        got = cols_of()
        if got is not None:
            arr = got[1]
            try:
                return float(arr[3].max()) if len(arr[3]) else 0.0
            except (AttributeError, TypeError, ValueError):
                pass
    return max((p.speed for p in pts), default=0.0)


def _has_hold(pts) -> bool:
    """这条线要不要 `hold` 列。列存储时看它自带不带(不物化)。"""
    cols_of = getattr(pts, "columns", None)
    if callable(cols_of):
        got = cols_of()
        if got is not None:
            return bool(int(got[0]) & session_store.FMT_HOLD)
    return any(getattr(p, "hold", 0.0) for p in pts)



def _read_payload(path: str) -> dict | None:
    """读数据文件: **gzip 压缩的 JSON**。

    不做旧格式兼容(用户 2026-10-22 明确要求): 本软件自用、未发布, 数据随时
    可以重录, 没必要为早期的明文/BOM 文件留分支。读取失败就当没有数据
    (宁可从零开始, 也不要把半个文件当成完整数据用)。
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("读取线路数据失败: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def map_fingerprint(image_bytes: bytes | None) -> str:
    """由地图图片字节生成稳定的地图指纹(图片不可用时返回 unknown)。"""
    if not image_bytes:
        return "unknown"
    return hashlib.sha1(image_bytes).hexdigest()[:12]


# 8111 在**战局外**(/map_info.json 的 valid=false)返回的不是地图,
# 而是一张固定的 128x128 灰色 “?” 占位图(PNG, 1519 字节)。
# 实测 2026-10-06:
#   Content-Type: image/png, 1519 字节, SHA1 = ab516d02191d001d809253c346e8e3f93e13f136
# 采样得到 6 种颜色, 均为灰度(64/68/72/109/115/128), 中心像素 (128,128,128)。
# 用途: 把它当作“当前没有真实地图”的信号, 不要当地图绘制, 也不要当成一次换图。
# 常量统一放在 `garage.py`(黑名单助手判“在不在车库”也用同一份实测值)。
PLACEHOLDER_MAP_SHA1 = GARAGE_MAP_SHA1
PLACEHOLDER_MAP_KEY = PLACEHOLDER_MAP_SHA1[:12]


@dataclass
class RoutePoint:
    """轨迹采样点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    speed: float      # 当时速度 km/h
    # 车头朝向(归一化单位方向向量, 与 /map.img 同坐标系); 0,0 = 没记录
    # (旧版本录的轨迹没有这两个字段, 回放时不画朝向箭头)
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class RouteMark:
    """标记点 / 起点 / 终点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    kind: str = "mark"   # start | mark | stop


@dataclass
class RouteRecord:
    """一次完整的跑图录制。

    `zone_count` / `zone_signature` / `mode_name` 是录制开始时采集的战区信息:
    8111 不给模式名, 由战区数量推出(见 zones.MODE_NAMES), 坐标串则是稳定的
    “模式指纹”。旧记录没有这三个字段 → 归为“未知模式”。
    """
    route_id: str
    map_key: str
    map_name: str
    vehicle: str
    created_at: str
    duration: float
    start_x: float
    start_y: float
    points: list[RoutePoint] = field(default_factory=list)
    marks: list[RouteMark] = field(default_factory=list)
    zone_count: int = 0
    zone_signature: str = ""
    mode_name: str = ""
    # ---- **磁盘元数据**(从会话文件加载时填, 见 `LazyPoints`)--------------
    n_pts: int = 0
    vmax_hint: float = 0.0

    @property
    def point_count(self) -> int:
        """点数 —— 没加载时用头部里的数, 不会因此读点。"""
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.n_pts
        return len(pts)

    @property
    def vmax(self) -> float:
        """这条线的最高速度(km/h)。"""
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.vmax_hint
        if not self.vmax_hint:
            self.vmax_hint = _vmax_of(pts)      # 列存储: 取第 4 行最大值, 不物化
        return self.vmax_hint

    @property
    def label(self) -> str:
        """侧栏二级条目显示的文本。

        不重复带上用时 —— 侧栏已经有专门的“用时”列了, 重复前缀会挤掉车型名。
        """
        ts = self.created_at[11:16] if len(self.created_at) >= 16 else ""
        return f"{ts}  {self.vehicle}".strip()

    @property
    def mode_label(self) -> str:
        """模式显示名 —— **优先按战区数量推**。

        用户 2026-10-26: 「只按战区数量简单分类即可」, 所以 `zone_count` 有值时
        一律按它算(3=控制/2=攻陷/1=争夺), 不看存过的 `mode_name` —— 否则旧记录里
        那些五花八门的写法(带“大图/小图”之类后缀)会把同一模式拆成两组。
        只有 zone_count 缺失的旧数据才回退到 `mode_name`。
        """
        by_count = zone_mode_name(self.zone_count)
        if by_count and by_count != "未知模式":
            return by_count
        return self.mode_name or by_count


# ====================================================================== 全局录制
# 「全局录制」= 一局回放里**所有玩家**的线路, 分 A / B 两方各录一次
# (回放的 `/map_obj.json` 只给观战那一方的单位, 所以要录两遍才拼出整局)。
#
# 与单条线路录制的区别:
# * **不记朝向**(用户要求), 只记 x/y/t —— 点用 TrackPoint, 比 RoutePoint 省一半内存;
# * 一条全局记录下面是**很多条**线路: 每个单位"从出现(重生)到消失(死亡/换车)"算一条,
#   按 A/B 两队分组, 组内按开始时间**正序**(越靠前越早);
# * 8111 的 `/map_obj.json` **没有 id、没有玩家名**, 所以"同一个单位"是录制器按
#   "上一帧位置最近的那个同类对象"推测出来的(见 route_recorder 里的匹配逻辑),
#   同一个人中途上飞机/换车会被拆成两条线 —— 这点与用户确认过, 可以接受。
GLOBAL_SIDES = ("A", "B")
GLOBAL_SIDE_LABELS = {"A": "A 队", "B": "B 队"}


@dataclass(slots=True)
class TrackPoint:
    """全局录制的轨迹点: 坐标 + 时间 + **速度**(km/h)。

    `dx`/`dy` 恒为 0(全局录制不记朝向), 但**字段必须有**: `point_at()` 在
    “早于首点/晚于末点”时会把原始点直接返回, 回放光标随即读 `.dx/.dy` ——
    缺了就会每帧抛 AttributeError(2026-10-21 实测: 整张地图画不出来)。
    字段名与 `RoutePoint` 对齐, 速度配色/playhead 切分/光标那套可以直接复用。
    """
    x: float
    y: float
    t: float
    speed: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    # **静止持续时长**(秒, 只对全局录制有效): 从这个点开始, 单位一直停在 (x, y)
    # 直到 `t + hold`。
    #
    # 这么做的原因(用户 2026-10-23): 8111 每帧都在报同一个坐标, 静止的车如果
    # 逐帧存点, 一棵不动的防空车蹲 20 分钟就是 3 万个点 —— 而它们**坐标完全一样,
    # 只差时间**。所以静止段折叠成“一个点 + hold”, 移动段仍然逐帧全存(不抽稀),
    # 等于“拉多少数据就存多少信息”, 但体积不是一回事。
    # 旧数据没有这个字段 → 0, 行为与以前完全一致。
    hold: float = 0.0


def _pack_point(p) -> list:
    """一个全局轨迹点 → 紧凑数组 `[t, x, y, speed(, hold)]`。

    `hold` 只在非 0 时写第五个数 —— 移动点和旧数据都还是 4 个数的样子,
    读回来完全兼容。

    ⚠ **只 round `t`**: 它是逐帧累加出来的, 会带出 `0.30000000000000004` 这种长尾;
    而 `x`/`y`/`speed` 在**采样时就 round 过**(录制器与回放导入都做了),
    这里再 round 一遍要 80 万 × 3 次调用 = **0.8 秒**(实测)。
    """
    row = [round(p.t, 2), p.x, p.y, p.speed]
    hold = p.hold or 0.0
    if hold > 0.0:
        row.append(round(hold, 2))
    return row


def _unpack_point(row) -> TrackPoint | None:
    """紧凑数组 → `TrackPoint`(坏行返回 None, 由调用方过滤)。

    位置参数 + 长度快路径: 80 万点时要跑 80 万次, 关键字参数与多余的 `float()`
    在这里能差 0.3 秒(实测 0.82 → 0.52 s)。
    """
    try:
        if len(row) == 4:
            return TrackPoint(row[1], row[2], row[0], row[3])
        return TrackPoint(row[1], row[2], row[0], row[3],
                          hold=max(0.0, row[4]))
    except (IndexError, TypeError, ValueError):
        return None


# rels/回放导入的单条轨迹点 —— 同样打成数组, 理由见 `UnitLife.to_json` 的注释:
# 一个点写成一个 dict 时, `{"t":..,"x":..,"y":..,"speed":..,"dx":..,"dy":..}`
# 光键名就占 40 个字符。80 万点时这是 **3300 万 → 1300 万字符** 的差别,
# `json.dumps` 也跟着快一倍(2026-10-26 用户报“保存要卡好几秒”)。
def _pack_route_point(p) -> list:
    """`RoutePoint` → `[t, x, y, speed, dx, dy]`(与 `TrackPoint` 的数组同序)。

    同样只 round `t`, 理由见 `_pack_point`。
    """
    return [round(p.t, 2), p.x, p.y, p.speed, p.dx, p.dy]


def _unpack_route_point(row) -> RoutePoint | None:
    """数组或**旧版 dict** → `RoutePoint`(坏行返回 None)。

    ⚠ 两种格式都要认: 1.3.1 之前的文件里点是 `{"x":..,"y":..,...}` 的 dict。
    第一次 `save()` 之后就会全部写成新数组格式, 所以这只是“读一次旧文件”的过渡。
    """
    if isinstance(row, dict):
        try:
            return RoutePoint(row["x"], row["y"], row["t"],
                              row.get("speed") or 0.0,
                              row.get("dx") or 0.0, row.get("dy") or 0.0)
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return RoutePoint(row[1], row[2], row[0], row[3], row[4], row[5])
    except (IndexError, TypeError, ValueError):
        return None


def _pack_mark(m) -> list:
    """`RouteMark` → `[x, y, t, kind]`。"""
    return [m.x, m.y, round(m.t, 2), m.kind or "mark"]


def _unpack_mark(row) -> RouteMark | None:
    if isinstance(row, dict):
        try:
            return RouteMark(x=float(row["x"]), y=float(row["y"]),
                             t=float(row.get("t") or 0.0),
                             kind=str(row.get("kind") or "mark"))
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return RouteMark(x=float(row[0]), y=float(row[1]),
                         t=float(row[2]) if len(row) > 2 else 0.0,
                         kind=str(row[3]) if len(row) > 3 else "mark")
    except (IndexError, TypeError, ValueError):
        return None


@dataclass
class UnitLife:
    """全局录制里的一条线路 = 某个单位从出现(重生)到消失(死亡/换车)的一段轨迹。

    `life_id` 形如 `<全局条目 id>:<序号>` —— 侧栏的眼睛图标/地图缓存都拿它当键;
    为了能被回放与高亮那套代码直接使用, 额外暴露了 `route_id` / `marks` /
    `map_key` / `duration` 这几个与 `RouteRecord` 同名的属性。
    `icon` / `obj_type` 是 8111 原样的字段(如 `MediumTank` / `ground_model`),
    没有玩家名可用(8111 不给), 所以显示时就用它们当“名字”。
    """
    life_id: str
    icon: str = ""
    obj_type: str = ""
    color: str = ""             # 8111 的颜色(仅参考, 不参与分组)
    map_key: str = ""           # 所属地图(回放时用来判断底图对不对)
    points: list[TrackPoint] = field(default_factory=list)
    # 这条线是在哪一队录到的(`A` / `B`): 回放时阵亡标记的颜色靠它区分
    # (A 队 = 蓝, B 队 = 红)。
    side: str = ""
    # 这条线的单位是**被击毁**结束的(而不是录制结束时还活着)。
    # 用户口径(2026-10-21): “在 8111 中玩家对应的 icon 消失就是死了” ——
    # 一次只能开一台载具, 想换固定翼必先阵亡; 退局后载具停在原地、
    # 8111 过一会儿不再上报, 也算击毁。实现上只看“线断那一刻场上还有没有别人”。
    dead: bool = False
    # **阵亡时刻**(录制/回放时间轴的秒数; 0 = 没有阵亡信息)。
    # 回放导入时由 ECS 销毁时刻或击杀时刻给出, 播过这一刻就在图上画红叉。
    death_t: float = 0.0
    # 这条线**曾经是摄像机挂着的那辆车**(至少有1帧 icon == Player 或颜色是自己黄)。
    # 为什么要记这个布尔值而不是直接看 `color`: `color` 每帧都被覆盖, 摄像机离开后
    # 就变回普通蓝色了; 而“观战对象”这个事实必须留下来(2026-10-22 实测: 他第三辆
    # 车的线 8111 给的是 `icon=SAM`、只有颜色是自己黄, 而 B 队整队一条 `icon=Player`
    # 都没有 —— 只按 icon 认会全漏)。
    watched: bool = False
    # ---- 回放导入（ECS）专有：8111 录制时这两个字段为空 ----
    # 玩家名。侧栏按它分组，所以要跟 `icon`（"玩家 · 型号"）分开存。
    player: str = ""
    # 内部型号名（如 `tankModels/cn_ztz_96b`），画图标/查类别可以用。
    vehicle_cls: str = ""
    # ---- **磁盘元数据**(只有从会话文件加载时才有值)-----------------------
    # 侧栏/悬停要的是点数、首末时刻、最高速, 但**不该为此把几十万点读进来**:
    # 这几项就存在会话头部里, 加载时直接填进来(见 `LazyPoints`)。
    n_pts: int = 0
    t0_hint: float = 0.0
    t1_hint: float = 0.0
    vmax_hint: float = 0.0

    @property
    def route_id(self) -> str:
        """与 `RouteRecord.route_id` 同名 —— 地图缓存/回放/高亮都按它当键。"""
        return self.life_id

    @property
    def marks(self) -> list:
        """全局线路没有标记点 ⇒ 回放时“到下一标记暂停”会直接播到底。"""
        return []

    # ---- 下面两个只为了让绘制/悬停那套代码把它当成普通轨迹(鸭子类型)
    @property
    def vehicle(self) -> str:
        return self.icon or self.obj_type or "未知单位"

    @property
    def created_at(self) -> str:
        return ""                  # 全局线路没有独立的录制时刻(它属于整局)

    @property
    def t0(self) -> float:
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.t0_hint      # 还没读点 → 用头部里的元数据(侧栏只靠它)
        return pts[0].t if pts else 0.0

    @property
    def t1(self) -> float:
        """线尾时刻 = 最后一个已知时刻 + 该点的**静止持续时长**(`hold`)。

        静止段的采样被折叠成“一个点 + hold”(见 `TrackPoint.hold`), 所以末点的
        有效结束时刻是 `t + hold`; 旧数据没 hold → 与以前完全一致。
        """
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.t1_hint
        if not pts:
            return 0.0
        last = pts[-1]
        return last.t + float(getattr(last, "hold", 0.0) or 0.0)

    @property
    def point_count(self) -> int:
        """点数 —— 没加载时用头部里的数, 不会因此读点。"""
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.n_pts
        return len(pts)

    @property
    def vmax(self) -> float:
        """这条线的最高速度(km/h) —— 没加载时用头部里的值。"""
        pts = self.points
        if isinstance(pts, LazyPoints) and not pts.loaded:
            return self.vmax_hint
        if not self.vmax_hint:
            self.vmax_hint = _vmax_of(pts)      # 列存储: 取第 4 行最大值, 不物化
        return self.vmax_hint

    @property
    def duration(self) -> float:
        return max(0.0, self.t1 - self.t0)

    @property
    def short_id(self) -> str:
        """侧栏/报 bug 时用的**短编号**: `<条目 id>:A19` → `A19`。

        用户 2026-10-23: “没有这个，某条路线有 bug 我要报告时太麻烦”。
        """
        return self.life_id.rsplit(":", 1)[-1] or self.life_id

    @property
    def label(self) -> str:
        """侧栏显示名: `A19  01:47  SAM` —— 编号放最前面，方便直接报出来。"""
        name = self.icon or self.obj_type or "未知单位"
        return f"{self.short_id}  {fmt_time(self.t0)}  {name}"

    def to_json(self) -> dict:
        # 点写成 [t, x, y, speed(, hold)] 的紧凑数组: 整局几十辆车 → 几十万个点,
        # 用 {"x":..,"y":..,"t":..} 的字典形式会让 routes.json 膨胀 3 倍以上。
        # `hold` 只在非 0 时写第五个数(静止段), 移动点仍然是 4 个数的老格式。
        out = {
            "id": self.life_id,
            "icon": self.icon,
            "type": self.obj_type,
            "color": self.color,
            "map_key": self.map_key,
            "side": self.side,
            "dead": 1 if self.dead else 0,
            "pts": [_pack_point(p) for p in self.points],
        }
        if self.death_t:
            out["death_t"] = round(self.death_t, 2)
        if self.watched:
            out["w"] = 1
        # 回放导入才能拿到的玩家身份 —— 不写就重启后分组全没了
        if self.player:
            out["player"] = self.player
        if self.vehicle_cls:
            out["cls"] = self.vehicle_cls
        return out

    @classmethod
    def from_json(cls, data: dict) -> UnitLife | None:
        try:
            life_id = str(data["id"])
        except (KeyError, TypeError):
            return None

        def read_points(rows) -> list[TrackPoint]:
            return [p for p in (_unpack_point(row) for row in rows or [])
                    if p is not None]

        try:
            death_t = float(data.get("death_t") or 0.0)
        except (TypeError, ValueError):
            death_t = 0.0
        return cls(
            life_id=life_id,
            icon=str(data.get("icon") or ""),
            obj_type=str(data.get("type") or ""),
            color=str(data.get("color") or ""),
            map_key=str(data.get("map_key") or ""),
            points=read_points(data.get("pts")),
            side=str(data.get("side") or ""),
            dead=bool(data.get("dead")),
            death_t=death_t,
            watched=bool(data.get("w")),
            player=str(data.get("player") or ""),
            vehicle_cls=str(data.get("cls") or ""),
        )


@dataclass
class GlobalRecord:
    """一次“全局录制”条目(侧栏里挂在**存放路线的那一级**, 与单条路线同级)。

    名称直接用录制时间(`label`); 下面分 A / B 两队, 每队若干条 `UnitLife`;
    组内按开始时间正序(用户要求: 越靠前时间越早)。
    `marks` 是右键打上的“下次录到哪一队”(可同时有 A 和 B) ——
    **没有任何条目带标记**时, 点「A/B 队录制」会新建一个全局条目。
    """
    route_id: str
    map_key: str
    map_name: str
    created_at: str
    zone_count: int = 0
    zone_signature: str = ""
    mode_name: str = ""
    duration: float = 0.0
    marks: list[str] = field(default_factory=list)
    sides: dict[str, list[UnitLife]] = field(default_factory=lambda: {"A": [], "B": []})
    # 战区状态**时间轴**: [{"t": 秒, "zones": [{label,x,y,side,color,blink}]}]
    # 只在“归属变了或开始/结束被占领”时记一条(见 `RouteRecorder._record_zone_state`),
    # 回放时按时间取最近一条 → 战区能重现当时的蓝/红/占领中, 而不是一张静态快照。
    # 旧数据没有这个字段 → 回放退回静态布局(取 `layout_for`)。
    zone_states: list[dict] = field(default_factory=list)
    # 本局 `/hudmsg` 的**击毁消息**(录制前后各抓一次取差集, 按 id 去重):
    # [{"id": 1234, "t": 457.0, "msg": "A (豹 2A4) 击毁了 B (T-64A)"}]
    # 用途: `t` 是**比赛秒数**(从服务器端开始录像计时, 与我们 t=0 差一个未知偏移),
    # 所以只能拿“击毁事件之间的**时间差模式**”去和线路结束时刻对齐 ——
    # 摄像机挂着的那个玩家 icon 不会消失(实测), 他的死亡时刻只能靠这个反推。
    kills: list[dict] = field(default_factory=list)
    # 本条目**归一化时用的世界矩形** `[min_x, max_x, min_z, max_z]`(米), 只对回放
    # 导入的记录有意义: `points` 里存的是 0~1, 有了这个矩形就能反推出世界米 ——
    # 于是“地图管理里换了/补了校准”以后可以把旧记录**重新对齐**, 不用重新导入。
    norm_rect: list = field(default_factory=list)
    # 两队的机场跑道(回放导入时从 ECS 的机场防空阵地估出来的, 见
    # `replay_import._airfields_from_ecs`)。存的是**世界米**, 绘制时才用 `norm_rect`
    # 归一化 —— 这样换校准时它跟着线路一起重算, 不用单独维护。
    #   {"side": "A"/"B", "kind": "runway"/"helipad",
    #    "x": 东, "z": 北, "dx": 朝向东分量, "dz": 朝向北分量,
    #    "length": 米, "width": 米, "est": True}
    airfields: list[dict] = field(default_factory=list)
    # 结算表摘要(回放导入时从文件尾部的结果 BLK 解出, 见 `wrpl_results.py`):
    #   {"status": "success", "time_played": 429.8,
    #    "players": [{"name", "clanTag", "team", "kills", "deaths", "score"…}]}
    # 用途: 侧栏的“结算表”折叠面板 —— 这一局到底有谁、哪队、战绩多少。
    # 本地录制的条目没有这个字段(空 dict), 面板就不显示那一块。
    results: dict = field(default_factory=dict)
    # ---- 磁盘元数据(从会话文件加载时才有)--------------------------------
    # 线路数存在**会话头部**里: 侧栏“N 条”不用为了计数去读线路清单。
    n_lives: int = 0
    # 线路清单(玩家/型号/点数/点块位置)存在文件的**第二段**; 展开那一局时才读。
    # 它是 `_fill_lives(rec)` 这种回调 —— 读完自己清空, 不会重复读。
    _dir_loader: object = None

    def _ensure_dir(self) -> None:
        """首次需要 `sides` 时把线路清单读进来(见 `session_store.read_tracks`)。"""
        loader, self._dir_loader = self._dir_loader, None
        if loader is not None:
            try:
                loader(self)
            except Exception:      # noqa: BLE001 - 读坏文件不能把界面炸掉
                log.exception("读取线路清单失败: %s", self.route_id)

    def sides_loaded(self) -> bool:
        """线路清单读过没(没读过不代表没有线路 —— 见 `n_lives`)。"""
        return self._dir_loader is None

    def zone_state_entry(self, t: float, side: str = "") -> dict | None:
        """取回放时刻 `t` 的战区快照**整条**(`{"t","side","zones"}`); 没有返回 None。

        用时间轴里 `<= t` 的最后一条 —— 战区状态是“阶毯式”变化(只在不连续时刻变),
        用“最近的前一条”才是当时真实的样子; 取最近的(不管是前是后)会在
        占领发生**之前**就先把颜色改了。

        `side` 用来限“只看某一次观战(A/B)录到的那批”: 8111 的颜色站在观战者视角,
        A/B 两次观战视角相反, 混着取会让颜色一会儿蓝一会儿红(2026-10-23 修)。
        旧数据没有 `side` 字段 → 一律算命中; 指定了 side 但一条都没有 → 退回全部。

        ⚠ 旧数据(没有 side)是 A、B 两段拼在一个列表里的, **时间会重新从 0 开始** ——
        所以这里不能“遇到第一条 t 更大的就停”, 必须整表扫一遍取最后一条。
        """
        states = self.zone_states
        if not states:
            return None

        def pick(pool: list[dict]) -> dict | None:
            if not pool:
                return None
            best = -1
            for i, st in enumerate(pool):
                if float(st.get("t") or 0.0) <= t:
                    best = i
            if best < 0:
                best = 0                 # 早于第一条 → 用第一条(录制开始那一刻)
            return pool[best]

        if side:
            mine = [st for st in states
                    if not st.get("side") or str(st.get("side")) == side]
            got = pick(mine)
            if got is not None:
                return got
        return pick(list(states))

    def zone_state_at(self, t: float, side: str = "") -> list[dict] | None:
        """只要快照里的战区列表(见 `zone_state_entry`)。"""
        entry = self.zone_state_entry(t, side)
        if entry is None:
            return None
        zones = entry.get("zones")
        return list(zones) if isinstance(zones, list) else None

    @property
    def label(self) -> str:
        """侧栏显示名 = 录制时间(如 `10-21 21:03`)。"""
        ts = self.created_at or ""
        if len(ts) >= 16:
            return f"{ts[5:10]} {ts[11:16]}"
        return ts or "全局录制"

    @property
    def mode_label(self) -> str:
        """模式显示名 —— 优先按战区数量推(同 `RouteRecord.mode_label`)。"""
        by_count = zone_mode_name(self.zone_count)
        if by_count and by_count != "未知模式":
            return by_count
        return self.mode_name or by_count

    @property
    def is_replay(self) -> bool:
        """是不是「从录像导入」的条目(地图键 `wrpl_<level>`)。

        录像导入的坐标是米制世界坐标归一化来的, 与 8111 实时录制不同 ——
        只有它们受地图校准(`map_calib`)影响。
        """
        return (self.map_key or "").startswith("wrpl_")

    @property
    def mark_label(self) -> str:
        """标记的显示文字(没有标记返回空串)。"""
        return " ".join(s for s in GLOBAL_SIDES if s in self.marks)

    def lives(self, side: str) -> list[UnitLife]:
        self._ensure_dir()
        return self.sides.get(side) or []

    def count(self) -> int:
        """线路总数 —— 清单未加载时用会话头部里的数(不为了报数去读清单)。"""
        if self._dir_loader is not None:
            return max(int(self.n_lives or 0),
                       sum(len(v) for v in self.sides.values()))
        return sum(len(v) for v in self.sides.values())

    def timeline_note(self) -> str:
        """A/B 两队的**时间轴对比**(写日志/诊断用)。

        两队都是“抓到第一辆载具的那一刻 = 0 点”, 所以两边都从 0 开始 ——
        能拿来比的只有**结尾**:

        * 结尾相同(±0.5s) → 两边的可录单位消失于同一时刻(通常就是战斗结束),
          说明两次录制的 0 点也对得上;
        * 结尾差得多 → 两种可能, 8111 的数据**分不出来**:
          ① 某一队先被打光/退场(跟对齐无关),
          ② 两次录制的 0 点真的错开了(两侧“第一辆车”不是同一瞬间重生)。

        没有更好锚点的原因: 8111 不提供战斗时钟(`/hudmsg` 的 `time` 实测不可靠),
        而 `/map_obj.json` 只给**观战那一方**的单位 —— 两侧看到的第一个重生时刻
        本来就可能差 1~2 秒。
        """
        spans = []
        for side in GLOBAL_SIDES:
            lives = [lf for lf in self.lives(side) if lf.point_count]
            if lives:
                # 用 `t1`(末点 + hold)而不是 `points[-1].t`: 静止车的线尾被折叠成
                # 一个带持续时长的点, 直接取 `t` 会把“结尾”算早
                spans.append((side, len(lives), max(lf.t1 for lf in lives)))
        if not spans:
            return "无线路"
        parts = [f"{side} 队 {n} 条 0~{last:.1f}s" for side, n, last in spans]
        if len(spans) == 2:
            diff = spans[0][2] - spans[1][2]
            parts.append("结尾对齐" if abs(diff) <= 0.5 else f"结尾差 {diff:+.1f}s")
        return " | ".join(parts)

    def to_json(self) -> dict:
        return {
            "route_id": self.route_id,
            "map_key": self.map_key,
            "map_name": self.map_name,
            "created_at": self.created_at,
            "zone_count": self.zone_count,
            "zone_signature": self.zone_signature,
            "mode_name": self.mode_name,
            "duration": self.duration,
            # `marks`(A/B 队录制目标)**故意不落盘**(用户 2026-10-23 要求):
            # 它只是个“下次点录制录到哪一条”的临时指向, 关掉软件就该忘掉;
            # 写进 routes.json 会让人第二天莫名其妙地录到旧的条目上。
            "zone_states": self.zone_states,
            "kills": self.kills,
            "norm_rect": list(self.norm_rect or []),
            "airfields": list(self.airfields or []),
            "results": dict(self.results or {}),
            "sides": {side: [life.to_json() for life in self.lives(side)]
                      for side in GLOBAL_SIDES},
        }

    @classmethod
    def from_json(cls, data: dict) -> GlobalRecord | None:
        try:
            rec = cls(
                route_id=str(data["route_id"]),
                map_key=str(data.get("map_key", "unknown")),
                map_name=str(data.get("map_name", "未知地图")),
                created_at=str(data.get("created_at", "")),
                zone_count=int(data.get("zone_count") or 0),
                zone_signature=str(data.get("zone_signature") or ""),
                mode_name=str(data.get("mode_name") or ""),
                duration=float(data.get("duration") or 0.0),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("跳过损坏的全局录制记录: %s", exc)
            return None
        _fill_global_meta(rec, data)
        for side, rows in (data.get("sides") or {}).items():
            if side not in GLOBAL_SIDES or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                life = UnitLife.from_json(row)
                if life is not None:
                    if life.side not in GLOBAL_SIDES:
                        life.side = side          # 用所在组的队号
                    rec.sides[side].append(life)
        return rec


def _fill_global_meta(rec: GlobalRecord, data: dict) -> None:
    """解析全局条目的“小数据”: 归一化矩形 / 机场 / 战区时间轴 / 击毁消息。

    旧格式(`routes.json` 里的 dict)与新的会话头部字段名完全一致, 所以两边共用。
    """
    try:
        rect = [float(v) for v in (data.get("norm_rect") or [])]
        if len(rect) == 4 and rect[1] > rect[0] and rect[3] > rect[2]:
            rec.norm_rect = rect
    except (TypeError, ValueError):
        rec.norm_rect = []
    for row in data.get("airfields") or []:
        if not isinstance(row, dict):
            continue
        try:
            rec.airfields.append({
                "side": str(row.get("side") or "A"),
                "kind": str(row.get("kind") or "runway"),
                "x": float(row["x"]), "z": float(row["z"]),
                "dx": float(row.get("dx") or 1.0),
                "dz": float(row.get("dz") or 0.0),
                "length": float(row.get("length") or 3000.0),
                "width": float(row.get("width") or 300.0),
            })
        except (KeyError, TypeError, ValueError):
            continue
    res = data.get("results")
    if isinstance(res, dict):
        rows = [p for p in (res.get("players") or []) if isinstance(p, dict)]
        rec.results = {"status": str(res.get("status") or ""),
                       "time_played": float(res.get("time_played") or 0.0),
                       "players": rows}
    for row in data.get("zone_states") or []:
        if not isinstance(row, dict):
            continue
        zones = row.get("zones")
        if not isinstance(zones, list) or not zones:
            continue
        try:
            t = float(row.get("t") or 0.0)
        except (TypeError, ValueError):
            continue
        rec.zone_states.append({"t": t,
                                "zones": [z for z in zones if isinstance(z, dict)]})
    for row in data.get("kills") or []:
        if not isinstance(row, dict):
            continue
        try:
            rec.kills.append({"id": int(row.get("id") or 0),
                              "t": float(row.get("t") or 0.0),
                              "msg": str(row.get("msg") or "")})
        except (TypeError, ValueError):
            continue


def fmt_time(seconds: float) -> str:
    """秒 -> MM:SS。"""
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_time_ms(seconds: float) -> str:
    """秒 -> MM:SS.mmm(毫秒精度, 用于轨迹悬停预览)。"""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    m, rem = divmod(total_ms, 60_000)
    sec, ms = divmod(rem, 1000)
    return f"{m:02d}:{sec:02d}.{ms:03d}"


def fmt_speed(value: float) -> str:
    """速度(km/h)显示: 陆战 8111 返回整数(无小数), 空战有小数。

    实测: 陆战 4000+ 样本无一小数; 空战最多 6 位小数。
    这里自适应 —— 整数不显示小数点, 有小数才保留 1 位。
    """
    v = float(value)
    return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:.1f}"


def point_at(points: list[RoutePoint], t: float) -> RoutePoint | None:
    """取 t 时刻的轨迹位置(线性插值)。

    回放光标需要平滑移动, 不能在采样点之间跳 —— 因此按时间插值。
    t 超出范围时返回首/末点; 空列表返回 None。
    """
    if not points:
        return None
    if t <= points[0].t:
        return points[0]
    if t >= points[-1].t:
        return points[-1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:                      # 二分找到区间
        mid = (lo + hi) // 2
        if points[mid].t <= t:
            lo = mid
        else:
            hi = mid
    a, b = points[lo], points[hi]
    # 静止段折叠(`hold`): a 点在 [a.t, a.t + a.hold] 期间坐标不变, 真正“开始移动”
    # 是 a.t + hold, 所以插值要从那里开始算 —— 否则静止越久, 起步那一下就越像瞬移。
    # RoutePoint 没有 hold 字段 → getattr 取到 0, 行为与以前完全一致。
    start = a.t + float(getattr(a, "hold", 0.0) or 0.0)
    span = b.t - start
    k = 0.0 if span <= 0 else (t - start) / span
    k = min(1.0, max(0.0, k))
    # 朝向取**更近那一端**的采样值: 单位向量做线性插值在掉头时会退化成零向量
    # (方向丢失), 而采样间隔只有几十 ms, 取端点肉眼看不出跳变。
    src = a if k < 0.5 else b
    return RoutePoint(x=a.x + (b.x - a.x) * k,
                      y=a.y + (b.y - a.y) * k,
                      t=t,
                      speed=a.speed + (b.speed - a.speed) * k,
                      dx=src.dx, dy=src.dy)


def split_points(points: list[RoutePoint],
                 t: float) -> tuple[list[RoutePoint], list[RoutePoint]]:
    """按时刻 t 把轨迹切成 (已播放, 未播放), 边界插入插值点以保证连续。"""
    if not points:
        return ([], [])
    head = [p for p in points if p.t <= t]
    tail = [p for p in points if p.t > t]
    mid = point_at(points, t)
    if mid is not None:
        if not head or head[-1].t < t:
            head.append(mid)
        if tail and tail[0].t > t:
            tail.insert(0, mid)
    return (head, tail)


# 8111 载具内部名的常见国家前缀(显示时去掉, 更接近游戏内名称)
_NATION_PREFIXES = (
    "germ_", "ussr_", "us_", "uk_", "jp_", "cn_", "fr_", "it_",
    "sw_", "il_", "rus_", "usa_",
)

# 载具类别前缀(实测 indicators.type 形如 tankModels/germ_leopard_2a4_pzbtl_123)
_MODELS_PREFIX = re.compile(r"^[A-Za-z]+Models/")


def pretty_vehicle(key: str) -> str:
    """把 8111 的载具内部名转为较友好的显示名。

    例: tankModels/germ_leopard_2a4_pzbtl_123 -> Leopard 2A4 Pzbtl 123;
        germ_leopard_2pl -> Leopard 2PL; us_m1_abrams -> M1 Abrams。
    无法解析时原样返回, 避免信息丢失。
    """
    if not key:
        return "未知载具"
    k = _MODELS_PREFIX.sub("", key.strip())
    for p in _NATION_PREFIXES:
        if k.startswith(p):
            k = k[len(p):]
            break
    words = [w for w in k.split("_") if w]
    if not words:
        return key

    def _fmt(w: str) -> str:
        if w.isdigit():
            return w
        if any(ch.isdigit() for ch in w):   # 数字+字母混合段(如 72b)整体大写
            return w.upper()
        return w.capitalize()

    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        # 数字段 + 紧邻的短后缀(如 2 + pl)合并: "2PL"
        if (w.isdigit() and i + 1 < len(words)
                and words[i + 1].isalpha() and len(words[i + 1]) <= 3):
            out.append(w + words[i + 1].upper())
            i += 2
            continue
        out.append(_fmt(w))
        i += 1
    return " ".join(out)


def _points_changed(holder) -> bool:
    """这条记录的点有没有被改过(靠“点数变了”判断, 不触发惰性加载)。"""
    pts = holder.points
    if isinstance(pts, LazyPoints) and not pts.loaded:
        return False
    return len(pts) != int(getattr(holder, "n_pts", 0) or 0)


def _pack_route(rec: RouteRecord) -> tuple[dict, list[bytes], list[dict] | None]:
    """`RouteRecord` → (会话头部, 点块, 无第二段)。单条录制有朝向, 没有 hold。

    单条录制只有**一个**点块, 偏移恒为 0 —— 没必要为它开第二段, 元数据直接放头部。
    """
    pts = rec.points
    fmt = session_store.FMT_BASE | session_store.FMT_DIR
    raw = session_store.pack_points(pts, fmt=fmt)
    blob = session_store.compress(raw) if raw else b""
    entry = {
        "n": len(pts),
        "fmt": fmt,
        "raw": len(raw),
        "vmax": round(_vmax_of(pts), 1),
        "off": 0,
        "clen": len(blob),
    }
    header = {
        "v": session_store.FORMAT_VERSION,
        "kind": "route",
        "id": rec.route_id,
        "map_key": rec.map_key,
        "map_name": rec.map_name,
        "vehicle": rec.vehicle,
        "created_at": rec.created_at,
        "duration": round(rec.duration, 3),
        "start_x": rec.start_x,
        "start_y": rec.start_y,
        "zone_count": rec.zone_count,
        "zone_signature": rec.zone_signature,
        "mode_name": rec.mode_name,
        "marks": [_pack_mark(m) for m in rec.marks],
        "pts": entry,
    }
    return header, [blob], None


def _pack_global(rec: GlobalRecord) -> tuple[dict, list[bytes], list[dict]]:
    """`GlobalRecord` → (会话头部, 每条线的点块, 线路清单)。

    线路清单单独成一段: 500 局的话头部能小两个数量级, 启动就不用读它了。
    """
    rec._ensure_dir()                  # 要写就得先把清单读全(否则会丢线路)
    header = {
        "v": session_store.FORMAT_VERSION,
        "kind": "global",
        "id": rec.route_id,
        "map_key": rec.map_key,
        "map_name": rec.map_name,
        "created_at": rec.created_at,
        "zone_count": rec.zone_count,
        "zone_signature": rec.zone_signature,
        "mode_name": rec.mode_name,
        "duration": round(rec.duration, 3),
        "nl": rec.count(),             # 线路数(侧栏报数用, 不必读清单)
        "norm_rect": list(rec.norm_rect or []),
        "airfields": list(rec.airfields or []),
        "results": dict(rec.results or {}),
        "zone_states": rec.zone_states,
        "kills": rec.kills,
    }
    tracks: list[dict] = []
    blocks: list[bytes] = []
    for side in GLOBAL_SIDES:
        for life in rec.sides.get(side) or []:
            pts = life.points
            fmt = session_store.FMT_BASE
            if _has_hold(pts):                  # 只有静止段折叠过的点才多一列
                fmt |= session_store.FMT_HOLD        # 列存储时看它自带不带, 不物化
            raw = session_store.pack_points(pts, fmt=fmt)
            entry = {
                "id": life.life_id,
                "side": side,
                "icon": life.icon,
                "type": life.obj_type,
                "color": life.color,
                "dead": 1 if life.dead else 0,
                "player": life.player,
                "cls": life.vehicle_cls,
                "n": len(pts),
                "fmt": fmt,
                "raw": len(raw),
                # 侧栏/悬停要用的元数据(有它就不用为了显示去读点)
                "t0": round(life.t0, 3),
                "t1": round(life.t1, 3),
                "vmax": round(_vmax_of(pts), 1),
            }
            if life.death_t:
                entry["death_t"] = round(life.death_t, 2)
            if life.watched:
                entry["w"] = 1
            tracks.append(entry)
            blocks.append(session_store.compress(raw) if raw else b"")
    return header, blocks, tracks


class RouteStore:
    """线路记录读写(原子写, 线程安全)。

    存储布局见模块 docstring: **小数据**在 `routes.json`, **一局一个文件**在
    `routes.sessions/<id>.wts`。于是:

    * 启动只读每个文件的头部(500 局几十毫秒, 与总点数无关);
    * 保存只重写变过的那一局;
    * 点数据用到哪局才读哪局(见 `LazyPoints`)。
    """

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "routes.json")
        # 一局一个文件的目录 —— 用 meta 文件名 + `.sessions` 后缀, 这样同一个进程里
        # 开两个不同路径的 store(测试/探针)不会互相串数据。
        self.sessions_dir = os.path.splitext(self.path)[0] + ".sessions"
        self._lock = threading.RLock()
        # 后台写盘(见 `request_save`): 界面只置个标志, 真正的序列化/压缩在另一个
        # 线程里做 —— 一局回放就几十万点, 同步写要几百毫秒。
        self._save_pending = False
        self._closing = False
        # 有没有“自上次成功写盘之后又改过东西”—— 退出时据此决定要不要真写一次
        self._dirty = False
        self._save_wake = threading.Event()
        self._save_thread: threading.Thread | None = None
        # 需要重写/删除的会话(按 route_id) —— 拆文件的意义就在这里:
        # 改一个地图名不该重写那几百 MB 的轨迹
        self._dirty_ids: set[str] = set()
        self._deleted_ids: set[str] = set()
        self._dirty_meta = False
        self.map_names: dict[str, str] = {}
        # 用户在「地图管理」里勾了“隐藏”的地图: 只是**不在左侧列表里显示**,
        # 轨迹数据/名称/缓存图片都不动(空战地图永远用不到, 留着只碍事)
        self.hidden_maps: list[str] = []
        # 见过的地图键(含**只缓存了图片、还没录过轨迹**的地图):
        # 侧栏与「地图管理」的默认编号都基于它, 保证两边一致且稳定
        self.known_maps: list[str] = []
        # 见过的战区布局: {地图键: [布局, ...]}(新的在前, 每种模式一条),
        # 存下来才能在战局结束后依旧把 A/B/C 画在地图上
        self.map_zones: dict[str, list[dict]] = {}
        # 地图校准(世界米 → 底图像素): {地图键: MapCalib.to_dict()}
        # 回放导入的线路是**米制世界坐标**, 必须有这个矩形才能准确落到 2048² 底图上
        self.map_calib: dict[str, dict] = {}
        # ---- 地图身份的「别名」: {图片哈希键: 回放里的关卡代号} ------------------
        # 为什么要有它(用户 2026-10-26): 同一张地图会以**两种身份**进来 ——
        #   * 8111 实况/单条录制 → 图片哈希(如 `c59f768c0fde`), 有图有校准;
        #   * 回放文件解析     → 关卡代号(`wrpl_avg_finland`), 有轨迹但没图。
        # 两者其实是同一张图, 软件自己判不出来(8111 不给地图名), 只能靠用户指认。
        # 指认**只改这张表**, 绝不改任何记录的 `map_key` —— 所以随时能改、改错能改回来。
        self.map_levels: dict[str, str] = {}
        # `map_levels` 的反向索引(关卡代号 → 认领它的哈希键), 由 `_rebuild_levels()` 维护
        self._level_owner: dict[str, str] = {}
        self.routes: list[RouteRecord] = []
        # 全局录制条目(整局所有玩家的线路, 分 A/B 两队)
        self.globals: list[GlobalRecord] = []
        self._load()
        # 写盘线程**惰性启动**(见 `request_save`): 只读的 store(探针/测试)不该占线程

    # ------------------------------------------------------------------ 加载
    def _load(self) -> None:
        """读 meta + 扫会话目录。旧的单文件 `routes.json` 会**自动迁移一次**。"""
        meta = self._read_meta()
        legacy = None
        if isinstance(meta, dict) and isinstance(meta.get("__legacy__"), dict):
            legacy = meta["__legacy__"]
            meta = None
        if isinstance(meta, dict):
            self._apply_meta(meta)
            self._load_sessions()
        elif isinstance(legacy, dict):
            log.info("检测到旧格式的单文件数据, 迁移成「一局一文件」…")
            self._apply_meta(legacy)
            self._migrate_legacy(legacy)      # 迁移自己就填好了 routes/globals
        else:
            self._load_sessions()             # 只有会话没有 meta(或全新)也能起来
        self._rebuild_levels()
        self._recover_layouts()

    def _read_meta(self) -> dict | None:
        """读 meta。

        返回三种: 新格式 dict / `{"__legacy__": 旧整包}` / None(没有或读不出来)。
        """
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                if data.get("layout") == "sessions-v1":
                    return data
                # 旧格式的**明文** JSON(1.2 时代的 routes.json 就是这样)
                return {"__legacy__": data}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        legacy = _read_payload(self.path)      # 旧格式的 gzip 整包
        return {"__legacy__": legacy} if isinstance(legacy, dict) else None

    def _apply_meta(self, data: dict) -> None:
        """把 meta(地图名/隐藏/已知地图/战区布局/校准/关卡关联)灌进来。"""
        try:
            names = data.get("map_names")
            if isinstance(names, dict):
                self.map_names = {str(k): str(v) for k, v in names.items()}
            hidden = data.get("hidden_maps")
            if isinstance(hidden, list):
                self.hidden_maps = [str(k) for k in hidden if str(k)]
            known = data.get("known_maps")
            if isinstance(known, list):
                self.known_maps = [str(k) for k in known if str(k)]
            mz = data.get("map_zones")
            if isinstance(mz, dict):
                for key, layouts in mz.items():
                    if isinstance(layouts, list):
                        self.map_zones[str(key)] = [x for x in layouts if isinstance(x, dict)]
            mc = data.get("map_calib")
            if isinstance(mc, dict):
                for key, calib in mc.items():
                    if isinstance(calib, dict):
                        self.map_calib[str(key)] = calib
            ml = data.get("map_levels")
            if isinstance(ml, dict):
                self.map_levels = {str(k): str(v).strip()
                                   for k, v in ml.items() if str(k) and str(v).strip()}
        except (TypeError, ValueError) as exc:
            log.warning("读取地图元数据失败: %s", exc)

    def _load_sessions(self) -> None:
        """扫会话目录建索引(只读头部, **不读点**)。"""
        for head, path in session_store.scan_dir(self.sessions_dir):
            try:
                kind = str(head.get("kind") or "route")
                rec = (self._global_from_header(head, path) if kind == "global"
                       else self._route_from_header(head, path))
            except (TypeError, ValueError, KeyError) as exc:
                log.warning("跳过损坏的会话 %s: %s", os.path.basename(path), exc)
                continue
            if rec is None:
                continue
            if isinstance(rec, GlobalRecord):
                self.globals.append(rec)
            else:
                self.routes.append(rec)
        # 目录里的顺序不可靠 → 按录制时间排(组内再按 id 保证稳定)
        self.routes.sort(key=lambda r: (r.created_at, r.route_id))
        self.globals.sort(key=lambda g: (g.created_at, g.route_id))

    def _route_from_header(self, head: dict, path: str) -> RouteRecord | None:
        rid = str(head.get("id") or "")
        if not rid:
            return None
        # 单条录制只有一条轨迹, 它的点元数据在第一段里(没有第二段)
        entry = head.get("pts") if isinstance(head.get("pts"), dict) else {}
        n = int(entry.get("n") or 0)
        marks = [m for m in (_unpack_mark(row) for row in head.get("marks") or [])
                 if m is not None]
        points: list = LazyPoints(lambda: self._load_points(path, entry, True)) if n else []
        return RouteRecord(
            route_id=rid,
            map_key=str(head.get("map_key") or "unknown"),
            map_name=str(head.get("map_name") or "未知地图"),
            vehicle=str(head.get("vehicle") or ""),
            created_at=str(head.get("created_at") or ""),
            duration=float(head.get("duration") or 0.0),
            start_x=float(head.get("start_x") or 0.0),
            start_y=float(head.get("start_y") or 0.0),
            points=points,
            marks=marks,
            zone_count=int(head.get("zone_count") or 0),
            zone_signature=str(head.get("zone_signature") or ""),
            mode_name=str(head.get("mode_name") or ""),
            n_pts=n,
            vmax_hint=float(entry.get("vmax") or 0.0),
        )

    def _global_from_header(self, head: dict, path: str) -> GlobalRecord | None:
        rid = str(head.get("id") or "")
        if not rid:
            return None
        rec = GlobalRecord(
            route_id=rid,
            map_key=str(head.get("map_key") or "unknown"),
            map_name=str(head.get("map_name") or "未知地图"),
            created_at=str(head.get("created_at") or ""),
            zone_count=int(head.get("zone_count") or 0),
            zone_signature=str(head.get("zone_signature") or ""),
            mode_name=str(head.get("mode_name") or ""),
            duration=float(head.get("duration") or 0.0),
            n_lives=int(head.get("nl") or 0),
        )
        _fill_global_meta(rec, head)
        # 线路清单(200 多条 × 每条 20 个字段)比会话元数据大两个数量级 → 展开时才读
        dir_len = int(head.get("_dir_len") or 0)
        rec._dir_loader = lambda r: self._fill_lives(r, path, dir_len)
        return rec

    def _fill_lives(self, rec: GlobalRecord, path: str, dir_len: int) -> None:
        """把某局的线路清单读进来(`GlobalRecord._ensure_dir` 触发)。"""
        for entry in session_store.read_tracks(path, {"_dir_len": dir_len}):
            side = str(entry.get("side") or "")
            if side not in GLOBAL_SIDES:
                continue
            life_id = str(entry.get("id") or "")
            if not life_id:
                continue
            n = int(entry.get("n") or 0)
            rec.sides[side].append(UnitLife(
                life_id=life_id,
                icon=str(entry.get("icon") or ""),
                obj_type=str(entry.get("type") or ""),
                color=str(entry.get("color") or ""),
                map_key=rec.map_key,
                points=LazyPoints(
                    lambda e=entry: self._load_points(path, e, False, dir_len)) if n else [],
                side=side,
                dead=bool(entry.get("dead")),
                death_t=float(entry.get("death_t") or 0.0),
                watched=bool(entry.get("w")),
                player=str(entry.get("player") or ""),
                vehicle_cls=str(entry.get("cls") or ""),
                n_pts=n,
                t0_hint=float(entry.get("t0") or 0.0),
                t1_hint=float(entry.get("t1") or 0.0),
                vmax_hint=float(entry.get("vmax") or 0.0),
            ))

    @staticmethod
    def _load_points(path: str, entry: dict, is_route: bool, dir_len: int = 0):
        """把一条线路的点块读出来并还原成**列视图**(`LazyPoints` 第一次访问时调)。

        为什么不再直接造点对象(2026-10-26): 44 万点一局, `unpack_points` 逐点
        `TrackPoint(...)` 要 **223 ms**, 而 `frombuffer` + reshape 只要 **0.2 ms**。
        点对象改为**用到才造**(`_Cols.at`), 遍历/修改时再整批物化(`LazyPoints._flat`)。
        """
        raw = session_store.read_block(path,
                                       int(entry.get("off") or 0),
                                       int(entry.get("clen") or 0),
                                       int(entry.get("raw") or 0),
                                       dir_len)
        if not raw:
            return None
        fmt = int(entry.get("fmt") or session_store.FMT_BASE)
        n, arr = session_store.unpack_columns(raw, fmt=fmt)
        if n <= 0 or arr is None:
            return None
        if is_route:
            def make(t, x, y, speed, dx, dy, _hold):
                return RoutePoint(x, y, t, speed, dx, dy)
        else:
            def make(t, x, y, speed, _dx, _dy, hold):
                return TrackPoint(x, y, t, speed, 0.0, 0.0, hold)
        return _Cols(fmt, arr, n, make)

    # ------------------------------------------------------------------ 迁移
    def _migrate_legacy(self, data: dict) -> None:
        """旧的单文件整包(`routes.json` = gzip 的 JSON)→ 一局一个会话文件。

        顺序很重要: **先把所有会话写出来, 最后才落 meta**(meta 里的 `layout`
        标记就是“迁移已完成”的凭据)。中途崩了下次启动会重来一遍, 不会丢记录。
        原文件改名成 `.v1.bak` 留档, 不删。
        """
        routes, globals_ = self._parse_legacy(data)
        with self._lock:
            self.routes = routes
            self.globals = globals_
            self._dirty_ids = {r.route_id for r in [*routes, *globals_]}
            self._dirty_meta = True
        for rec in [*routes, *globals_]:
            try:
                self._write_session(rec)
            except Exception:      # noqa: BLE001 - 单局失败不该中断整次迁移
                log.exception("迁移时写会话失败: %s", rec.route_id)
        self._write_meta(self.path + ".new")
        try:
            os.replace(self.path, self.path + ".v1.bak")
        except OSError as exc:
            log.warning("旧数据文件改名失败: %s", exc)
        try:
            os.replace(self.path + ".new", self.path)
        except OSError as exc:
            log.warning("迁移后的 meta 落盘失败: %s", exc)
        with self._lock:
            self._dirty_ids.clear()
            self._dirty_meta = False
        self._dirty = False
        log.info("旧数据迁移完成: %d 条单条录制 + %d 个全局条目",
                 len(routes), len(globals_))

    @staticmethod
    def _parse_legacy(data: dict) -> tuple[list[RouteRecord], list[GlobalRecord]]:
        """解析旧整包里的 `routes` / `globals`(只在迁移时用一次)。"""
        routes: list[RouteRecord] = []
        globals_: list[GlobalRecord] = []
        for item in data.get("routes") or []:
            try:
                pts = [_unpack_route_point(p) for p in item.get("points") or []]
                mks = [_unpack_mark(m) for m in item.get("marks") or []]
                routes.append(RouteRecord(
                    route_id=str(item["route_id"]),
                    map_key=str(item.get("map_key", "unknown")),
                    map_name=str(item.get("map_name", "未知地图")),
                    vehicle=str(item.get("vehicle", "")),
                    created_at=str(item.get("created_at", "")),
                    duration=float(item.get("duration", 0.0)),
                    start_x=float(item.get("start_x", 0.0)),
                    start_y=float(item.get("start_y", 0.0)),
                    points=[p for p in pts if p is not None],
                    marks=[m for m in mks if m is not None],
                    zone_count=int(item.get("zone_count") or 0),
                    zone_signature=str(item.get("zone_signature") or ""),
                    mode_name=str(item.get("mode_name") or ""),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("跳过损坏的线路记录: %s", exc)
        for item in data.get("globals") or []:
            if not isinstance(item, dict):
                continue
            rec = GlobalRecord.from_json(item)
            if rec is not None:
                globals_.append(rec)
        return routes, globals_

        self._rebuild_levels()
        self._recover_layouts()

    # ------------------------------------------------- 地图身份(哈希 ↔ 关卡代号)
    def _rebuild_levels(self) -> None:
        """按 `map_levels` 重建反向索引(关卡代号 → 认领它的哈希键)。

        同一个关卡代号被两个哈希同时认领时**先到先得**(字典顺序)—— 语义上不该发生
        (一张图不可能既是芬兰又是荷兰), 但用户误操作时也不该炸, 取第一个即可。
        """
        self._level_owner = {}
        for hk, level in self.map_levels.items():
            level = (level or "").strip()
            if hk and level:
                self._level_owner.setdefault(level, hk)

    def level_of_hash(self, map_key: str) -> str:
        """这个图片哈希关联的回放关卡代号(没关联返回空串)。"""
        return (self.map_levels.get(map_key) or "").strip()

    def hash_of_level(self, level: str) -> str:
        """这个回放关卡代号被哪个图片哈希认领了(没人认领返回空串)。"""
        return self._level_owner.get((level or "").strip(), "")

    def canon(self, map_key: str) -> str:
        """把地图键**归一化到实际的那张图**。

        回放导入的 `wrpl_<关卡代号>` 一旦被某个图片哈希认领, 它的数据就该和那张图
        算作同一张 —— 树状结构、校准查找、底图查找全部走这里。

        ⚠ 只做映射, **不搬数据**: 记录里的 `map_key` 永远是原值, 所以解除关联
        (把「可选」改回「未选择」或改指别的关卡代号) 立刻恢复原状, 不会丢东西。
        """
        if not map_key or not self._level_owner:
            return map_key
        if not map_key.startswith(MAP_KEY_PREFIX):
            return map_key
        return self._level_owner.get(map_key[len(MAP_KEY_PREFIX):], map_key)

    def set_map_level(self, map_key: str, level: str) -> bool:
        """把某个图片哈希关联到回放关卡代号(传空串 = 解除关联)。返回是否真的变了。"""
        map_key = (map_key or "").strip()
        level = (level or "").strip()
        if not map_key:
            return False
        if level.startswith(MAP_KEY_PREFIX):        # 允许用户传 `wrpl_avg_finland`
            level = level[len(MAP_KEY_PREFIX):]
        with self._lock:
            old = (self.map_levels.get(map_key) or "").strip()
            if old == level:
                return False
            if level:
                self.map_levels[map_key] = level
            else:
                self.map_levels.pop(map_key, None)
            self._rebuild_levels()
            self._dirty_meta = True
        self.save()
        log.info("地图身份: %s 的关卡代号 %r → %r", map_key, old, level)
        return True

    def level_choices(self) -> list[str]:
        """可选的回放关卡代号(软件里出现过的全部, 按字母序)。

        「地图管理」那一列下拉框就用它 —— 只能选软件真见过的代号, 天然防止手打错字。
        """
        out: list[str] = []
        for key in [*self.known_maps,
                    *(r.map_key for r in self.routes),
                    *(g.map_key for g in self.globals)]:
            key = key or ""
            if not key.startswith(MAP_KEY_PREFIX):
                continue
            level = key[len(MAP_KEY_PREFIX):]
            if level and level != "unknown" and level not in out:
                out.append(level)
        return sorted(out)


    def _recover_layouts(self) -> bool:
        """轨迹里有战区指纹、但 `map_zones` 没存过对应布局 → 从指纹还原出来。

        指纹本身就是 `字母@x,y;...`, 所以能反推战区位置; 这样四层树(地图→模式→版本)
        与地图上画战区对**旧数据**也成立(旧版本的 exe 没存过布局)。
        不在这里写盘 —— 下次正常保存时自然写回去。
        """
        known: dict[str, set[str]] = {k: {str(x.get("sig") or "") for x in v}
                                      for k, v in self.map_zones.items()}
        changed = False
        for rec in [*self.routes, *self.globals]:
            sig = rec.zone_signature
            if not sig or not rec.map_key or sig in known.get(rec.map_key, set()):
                continue
            zones = zones_from_signature(sig)
            if not zones:
                continue
            self.map_zones.setdefault(rec.map_key, []).append({
                "sig": sig,
                "count": len(zones),
                "mode": zone_mode_name(len(zones)),
                "zones": zones_to_json(zones),
                "seen_at": "",
            })
            known.setdefault(rec.map_key, set()).add(sig)
            changed = True
        if changed:
            self._dirty_meta = True
            log.info("从轨迹指纹补回了缺失的战区布局")
        return changed

    def _snapshot_meta(self) -> dict:
        """地图级的**小数据**(几十 KB) —— 它才是 `routes.json` 里剩下的全部内容。"""
        return {
            "version": 2,
            "layout": "sessions-v1",
            "map_names": self.map_names,
            "hidden_maps": self.hidden_maps,
            # 存**原始** known_maps(不按 canon 去重): 回放键 `wrpl_<关卡>` 即使
            # 当前被某个哈希认领掉了, 也要留在盘上 —— 用户撤销关联后它得立刻回来。
            "known_maps": [k for k in self.known_maps if k],
            "map_zones": self.map_zones,
            "map_calib": self.map_calib,
            "map_levels": self.map_levels,
        }

    # ------------------------------------------------------------------ 写盘
    def mark_dirty(self, *objs) -> None:
        """标记这些记录需要重写(接受 `route_id` 或记录对象)。"""
        with self._lock:
            for obj in objs:
                rid = obj if isinstance(obj, str) else getattr(obj, "route_id", "")
                if rid:
                    self._dirty_ids.add(rid)

    def save(self) -> None:
        """**请求**落盘(异步, 立即返回) —— 真正的写盘在后台线程里做。

        为什么异步(用户 2026-10-26): 一局回放就是几十万点, 写一局要几百毫秒 ——
        点 F6 保存、开始录制、选关卡代号、关窗口都不该在那儿等。

        ⚠ 调用后**不保证**文件已经写好; 要确定落盘用 `save_now()`, 退出前用 `flush()`。
        """
        if SYNC_SAVE:                  # 测试/探针: 保持同步语义
            self.save_now()
            return
        self.request_save()

    def save_now(self) -> None:
        """同步落盘: **只写变过的那一局 + meta**; 没有任何改动时是空操作。

        这就是拆成一局一文件的全部意义(用户 2026-10-26 的 500~600 局目标):
        改地图名只写几 KB 的 `routes.json`, 导入一局只写那 3~5 MB,
        其余几百局**一个字节都不动**。
        """
        with self._lock:
            meta_dirty = self._dirty_meta
            ids = set(self._dirty_ids)
            gone = set(self._deleted_ids)
            self._dirty_ids.clear()
            self._deleted_ids.clear()
            self._dirty_meta = False
            todo = [rec for rec in [*self.routes, *self.globals]
                    if rec.route_id in ids or self._needs_write(rec)]
        for rid in gone:
            session_store.remove_session(self.sessions_dir, rid)
        if meta_dirty or not os.path.exists(self.path):
            self._write_meta()
        failed: list[str] = []
        for rec in todo:
            try:
                self._write_session(rec)
            except Exception:      # noqa: BLE001 - 单局失败不能拖垮整次保存
                log.exception("写会话失败: %s", rec.route_id)
                failed.append(rec.route_id)
        if failed:
            with self._lock:
                self._dirty_ids.update(failed)     # 下次再试
            return
        self._dirty = False

    def _needs_write(self, rec) -> bool:
        """这条记录要不要重写: 文件不在 / 点被改过(点数变了)。

        `LazyPoints` 没加载时**只看元数据** —— 不会为了判断把几十万点读进来。
        """
        path = session_store.session_path(self.sessions_dir, rec.route_id)
        if not os.path.exists(path):
            return True
        if isinstance(rec, GlobalRecord):
            if not rec.sides_loaded():
                return False          # 清单都没读 → 点不可能被改过
            for side in GLOBAL_SIDES:
                for life in rec.sides.get(side) or []:
                    if _points_changed(life):
                        return True
            return False
        return _points_changed(rec)

    def _write_meta(self, dst: str | None = None) -> None:
        """写地图级小数据(普通 JSON, 可直接打开看)。"""
        target = dst or self.path
        tmp = target + ".tmp"
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with self._lock:
                text = json.dumps(self._snapshot_meta(),
                                  ensure_ascii=False, separators=(",", ":"))
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, target)
        except (OSError, TypeError, ValueError) as exc:
            log.error("保存地图元数据失败: %s", exc)

    def _write_session(self, rec) -> None:
        """把一局压成 `.wts`(内部是临时文件 + 原子替换)。"""
        if isinstance(rec, GlobalRecord):
            header, blocks, tracks = _pack_global(rec)
        else:
            header, blocks, tracks = _pack_route(rec)
        path = session_store.session_path(self.sessions_dir, rec.route_id)
        session_store.write_session(path, header, blocks, tracks)
        with self._lock:
            if isinstance(rec, GlobalRecord):
                rec.n_lives = 0
                for side in GLOBAL_SIDES:
                    for life in rec.sides.get(side) or []:
                        life.n_pts = len(life.points)
                        rec.n_lives += 1
            else:
                rec.n_pts = len(rec.points)

    # ------------------------------------------------------ 后台写盘(界面不等)
    def request_save(self) -> None:
        """请求落盘, **立即返回**。多次请求会合并成一次写。

        为什么要这个(用户 2026-10-26): 1.3.0 起一局回放就是几十万点, 一次全量
        写盘要 1~4 秒 —— 点 F6 保存、开始录制、选关卡代号、关窗口全在那儿等,
        叠加层都要等它写完才消失。
        """
        self._save_pending = True
        self._dirty = True
        if self._save_thread is None:
            self._save_thread = threading.Thread(
                target=self._save_worker, name="route-store-save", daemon=True)
            self._save_thread.start()
        self._save_wake.set()

    def _save_worker(self) -> None:
        """后台线程: 攒 `SAVE_DEBOUNCE_S` 把连续请求合并成一次写。"""
        while True:
            self._save_wake.wait()
            self._save_wake.clear()
            if not self._closing:
                time.sleep(SAVE_DEBOUNCE_S)      # 合并: 这期间来的请求只算一次
                self._save_wake.clear()
            if not self._save_pending:
                if self._closing:
                    return
                continue
            self._save_pending = False
            try:
                self.save_now()
            except Exception:  # noqa: BLE001 - 后台线程不能把异常抛出去
                log.exception("后台保存线路数据失败")
            if self._closing:
                return

    def flush(self, timeout: float = 20.0) -> None:
        """关窗口前调用: 把还在排队的写盘做完(**同步等它**)。

        不等的话, 后台线程会被进程退出直接干掉 —— 用户刚录的那条就没了。
        已经写完(没有新改动)时**直接返回** —— 大多数时候关窗口是瞬时的。
        """
        if not self._dirty and not self._save_pending:
            self._closing = True
            th = self._save_thread
            if th is not None and th.is_alive():
                self._save_wake.set()       # 叫醒它让它自己退出
                th.join(timeout)
            return
        self._save_pending = True
        self._closing = True
        th = self._save_thread
        if th is None:                 # 一次都没请求过 → 直接同步写一次
            try:
                self.save_now()
            except Exception:  # noqa: BLE001
                log.exception("退出时保存线路数据失败")
            return
        self._save_wake.set()
        if th.is_alive():
            th.join(timeout)

    def __getstate__(self):    # pragma: no cover - 只为避免意外 pickle 出线程
        raise TypeError("RouteStore 不可序列化")

    # ------------------------------------------------------------------
    def new_route(self, map_key: str, map_name: str, vehicle: str,
                  start_x: float, start_y: float) -> RouteRecord:
        """创建(尚未落盘)一条新线路记录。"""
        with self._lock:
            if map_key and map_key not in self.known_maps:
                self.known_maps.append(map_key)   # 新地图登记, 保证默认编号稳定
        rec = RouteRecord(
            route_id=uuid.uuid4().hex[:12],
            map_key=map_key,
            map_name=map_name,
            vehicle=vehicle,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            duration=0.0,
            start_x=start_x,
            start_y=start_y,
        )
        rec.marks.append(RouteMark(x=start_x, y=start_y, t=0.0, kind="start"))
        return rec

    def add(self, rec: RouteRecord) -> None:
        with self._lock:
            self.routes.append(rec)
            self._dirty_ids.add(rec.route_id)      # 只写这一局
        if rec.zone_signature:
            # 这条记录带着战区指纹, 但可能没存过对应布局(旧版 exe/手工数据)
            # → 立刻从指纹补上, 保证侧栏四层树与地图画战区都能正常工作
            self._recover_layouts()
        self.save()

    def remove(self, route_id: str) -> bool:
        with self._lock:
            before = len(self.routes)
            self.routes = [r for r in self.routes if r.route_id != route_id]
            changed = len(self.routes) != before
            if changed:
                self._dirty_ids.discard(route_id)
                self._deleted_ids.add(route_id)    # 文件也删
        if changed:
            self.save()
        return changed

    def get(self, route_id: str) -> RouteRecord | None:
        for r in self.routes:
            if r.route_id == route_id:
                return r
        return None

    # ------------------------------------------------------------ 全局录制条目
    def new_global(self, map_key: str, map_name: str, zone_count: int = 0,
                   zone_signature: str = "", mode_name: str = "") -> GlobalRecord:
        """创建一个(尚未落盘的)全局录制条目 —— 名字就是录制时间。"""
        with self._lock:
            if map_key and map_key not in self.known_maps:
                self.known_maps.append(map_key)
        return GlobalRecord(
            route_id=uuid.uuid4().hex[:12],
            map_key=map_key,
            map_name=map_name,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            zone_count=zone_count,
            zone_signature=zone_signature,
            mode_name=mode_name,
        )

    def add_global(self, rec: GlobalRecord) -> None:
        with self._lock:
            self.globals.append(rec)
            self._dirty_ids.add(rec.route_id)      # 只写这一局
            # 回放导入带了自己的地图名(`avg_finland`) → 直接登记成这张图的显示名,
            # 否则侧栏最上层只能显示“地图 1 / 地图 2”这种自动编号, 对不上号。
            # 用户已经起过名的**不覆盖**(`name_of` 非空就跳过)。
            if (rec.map_key and rec.map_name
                    and not (self.map_names.get(rec.map_key) or "").strip()):
                self.map_names[rec.map_key] = rec.map_name.strip()
            if rec.map_key and rec.map_key not in self.known_maps:
                self.known_maps.append(rec.map_key)
        if rec.zone_signature:
            self._recover_layouts()
        self.save()

    def remove_global(self, gid: str) -> bool:
        with self._lock:
            before = len(self.globals)
            self.globals = [g for g in self.globals if g.route_id != gid]
            changed = len(self.globals) != before
            if changed:
                self._dirty_ids.discard(gid)
                self._deleted_ids.add(gid)
        if changed:
            self.save()
        return changed

    def adopt_session(self, tmp_path: str) -> GlobalRecord | None:
        """接管一个**已经写好**的会话文件(并行解析的成果) —— **不再重新打包**。

        与 `add_global()` 的区别: 点已经在文件里了, 所以这里不碰点、不标脏 id,
        只做三件事 —— 把文件挪进 `routes.sessions/`、按头部建出内存里的元数据、
        把地图名 / 已知地图登记上。

        这是「一局一文件」结构的红利: 子进程(见 `import_pool`)自己就能把一局压成
        完整会话, 主进程接手时**连打包都省了**, 只读几百字节的头部。
        文件坏了返回 None(调用方当这一局失败)。
        """
        head = session_store.read_header(tmp_path)
        if not head:
            log.warning("接管的会话文件读不出头部: %s", tmp_path)
            return None
        rid = str(head.get("id") or "")
        if not rid:
            return None
        dst = session_store.session_path(self.sessions_dir, rid)
        try:
            os.makedirs(self.sessions_dir, exist_ok=True)
            os.replace(tmp_path, dst)          # 同目录 → 原子替换
        except OSError as exc:
            # ⚠ **跨盘时 `os.replace` 一定失败**(实测 WinError 17: 临时目录在 C:,
            # 数据在 D:)。并行解析现在写在 `sessions\` 里不会走到这里, 但保不齐
            # 以后有别的调用方传别处的路径 —— 退化成“复制 + 删源”。
            log.warning("接管会话: os.replace 失败(%s), 改用复制", exc)
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.copyfile(tmp_path, dst)
                os.remove(tmp_path)
            except OSError as exc2:
                log.error("接管会话失败(复制): %s", exc2)
                return None
        kind = str(head.get("kind") or "route")
        try:
            rec = (self._global_from_header(head, dst) if kind == "global"
                   else self._route_from_header(head, dst))
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("接管的会话头部有问题 %s: %s", os.path.basename(dst), exc)
            return None
        if rec is None:
            return None
        with self._lock:
            if isinstance(rec, GlobalRecord):
                self.globals.append(rec)
            else:
                self.routes.append(rec)
            if (rec.map_key and rec.map_name
                    and not (self.map_names.get(rec.map_key) or "").strip()):
                self.map_names[rec.map_key] = rec.map_name.strip()
            if rec.map_key and rec.map_key not in self.known_maps:
                self.known_maps.append(rec.map_key)
            self._dirty_meta = True            # 地图名/已知地图变了, meta 要落盘
            # ⚠ **不**把 id 加进 `_dirty_ids`: 文件已经就位, 别再打包一遍
        if rec.zone_signature:
            self._recover_layouts()
        self.save()
        return rec

    def get_global(self, gid: str) -> GlobalRecord | None:
        for g in self.globals:
            if g.route_id == gid:
                return g
        return None

    def marked_global(self, side: str) -> GlobalRecord | None:
        """找出被标记为“下次录到 `side` 队”的条目(同一队最多一条, 见 set_mark)。"""
        for g in self.globals:
            if side in g.marks:
                return g
        return None

    def set_mark(self, gid: str, side: str, on: bool) -> None:
        """给条目打/清标记。**同一队只会有一条**记录带标记 —— 打新标记时清掉旧条目上的。

        标记**只存在内存里**(不落盘, 见 `to_json`): 它是“下次点录制录到哪一条”的
        临时指向, 关掉软件就该忘掉。
        """
        if side not in GLOBAL_SIDES:
            return
        with self._lock:
            for g in self.globals:
                if g.route_id == gid:
                    continue
                if side in g.marks:
                    g.marks.remove(side)     # 一队只能指向一个条目, 否则不知道该录到哪
            target = self.get_global(gid)
            if target is not None:
                if on and side not in target.marks:
                    target.marks.append(side)
                elif not on and side in target.marks:
                    target.marks.remove(side)
                target.marks.sort(key=GLOBAL_SIDES.index)

    def clear_marks(self, gid: str) -> None:
        """清掉某个条目的全部标记(右键「清除标记」)。"""
        with self._lock:
            target = self.get_global(gid)
            if target is not None:
                target.marks.clear()

    def globals_by_mode(self) -> dict[tuple[str, str], list[GlobalRecord]]:
        """{(地图键, 模式名): [全局录制条目...]} —— 新的在前(按录制时间倒序)。

        侧栏只按**模式**分组(用户 2026-10-26: 不再细分"区域1/区域2"),
        所以这里按 `mode_label`(由战区数量推出)归并, 不按战区指纹拆。
        """
        out: dict[tuple[str, str], list[GlobalRecord]] = {}
        for g in self.globals:
            out.setdefault((self.canon(g.map_key or ""),
                            g.mode_label or ""), []).append(g)
        for rows in out.values():
            rows.sort(key=lambda g: g.created_at, reverse=True)
        return out

    # ------------------------------------------------------------------
    def display_name(self, map_key: str) -> str:
        """地图显示名: 用户命名优先, 否则按“首次见到”的顺序自动编号。"""
        map_key = self.canon(map_key)
        if map_key in self.map_names and self.map_names[map_key].strip():
            return self.map_names[map_key]
        keys = self.ordered_map_keys()
        if map_key in keys:
            return f"地图 {keys.index(map_key) + 1}"
        return f"地图 {len(keys) + 1}"

    def auto_name(self, map_key: str) -> str:
        """纯自动编号(`地图 N`), **不看用户起的名字** —— 地图管理里当“键”的前缀用。"""
        map_key = self.canon(map_key)
        keys = self.ordered_map_keys()
        if map_key in keys:
            return f"地图 {keys.index(map_key) + 1}"
        return f"地图 {len(keys) + 1}"

    def name_of(self, map_key: str) -> str:
        """用户给这张地图起的名字(没起过则为空串)。"""
        return (self.map_names.get(self.canon(map_key)) or "").strip()

    def remember_map(self, map_key: str) -> None:
        """登记一张见过(缓存过图片)的地图, 使它也有稳定的默认编号。"""
        if not map_key or map_key in ("unknown", PLACEHOLDER_MAP_KEY):
            return
        with self._lock:
            if map_key in self.known_maps:
                return
            self.known_maps.append(map_key)
            self._dirty_meta = True
        self.save()

    def rename_map(self, map_key: str, name: str) -> None:
        """给地图命名(空串 = 恢复自动编号)。"""
        map_key = self.canon(map_key)
        with self._lock:
            if name.strip():
                self.map_names[map_key] = name.strip()
            else:
                self.map_names.pop(map_key, None)
            for r in self.routes:
                if self.canon(r.map_key) == map_key and r.map_name != name.strip():
                    r.map_name = name.strip()
                    # 名字存在每条记录的会话头部里 → 那几条要重写(不碰点)
                    self._dirty_ids.add(r.route_id)
            self._dirty_meta = True
        self.save()

    def is_map_hidden(self, map_key: str) -> bool:
        """这张地图是否被用户在「地图管理」里勾了“隐藏”。"""
        return self.canon(map_key) in self.hidden_maps

    def set_map_hidden(self, map_key: str, hidden: bool) -> None:
        """勾选/取消“隐藏”。**只影响左侧列表**, 轨迹与缓存图片都不动。"""
        if not map_key:
            return
        map_key = self.canon(map_key)
        with self._lock:
            if hidden:
                if map_key not in self.hidden_maps:
                    self.hidden_maps.append(map_key)
            else:
                while map_key in self.hidden_maps:
                    self.hidden_maps.remove(map_key)
            self._dirty_meta = True
        self.save()
        log.info("地图管理: %s %s", "隐藏" if hidden else "取消隐藏", map_key)

    def merge_map_keys(self, keep: str, drop: str) -> bool:
        """把 `drop` 这张地图的全部数据并到 `keep` 上, 并删掉 `drop` 条目。

        用在“同一张地图被 8111 以不同分辨率发过来”时(见 MapCache.save):
        两个键其实是同一张图, 侧栏/地图管理里就应该只有一个地图。
        会先把 routes.json 备份成 .bak —— 这是会改用户数据的操作。
        """
        if not keep or not drop or keep == drop:
            return False
        moved = [r for r in self.routes if r.map_key == drop]
        moved_g = [g for g in self.globals if g.map_key == drop]
        has_data = bool(moved) or bool(moved_g) or drop in self.known_maps \
            or bool(self.name_of(drop)) or bool(self.map_zones.get(drop))
        if not has_data:
            return False
        try:
            if os.path.exists(self.path):
                shutil.copyfile(self.path, self.path + ".bak")
        except OSError as exc:  # noqa: BLE001
            log.warning("备份线路数据失败: %s", exc)
        with self._lock:
            keep_name = self.display_name(keep)      # drop 的轨迹改用 keep 的显示名
            for rec in moved:
                rec.map_key = keep
                rec.map_name = keep_name
            for g in moved_g:
                g.map_key = keep
                g.map_name = keep_name
            # 见过的地图: drop 的位置换成 keep(保持“首次见到”的顺序), 去掉重复
            self.known_maps = [keep if k == drop else k for k in self.known_maps]
            seen: set[str] = set()
            self.known_maps = [k for k in self.known_maps
                               if not (k in seen or seen.add(k))]
            if keep not in self.known_maps:
                self.known_maps.append(keep)
            # 名字: keep 没名字就用 drop 的
            if not self.name_of(keep) and self.name_of(drop):
                self.map_names[keep] = self.map_names[drop]
            self.map_names.pop(drop, None)
            # 隐藏标记也是“同一张图”的属性: 任一被隐藏则合并后仍隐藏
            was_hidden = (drop in self.hidden_maps) or (keep in self.hidden_maps)
            while drop in self.hidden_maps:
                self.hidden_maps.remove(drop)
            if was_hidden and keep not in self.hidden_maps:
                self.hidden_maps.append(keep)
            # 战区布局: 按指纹合并(同一布局只留一份, 取较新的 seen_at)
            merged: dict[str, dict] = {}
            for layout in (self.map_zones.get(keep) or []) + (self.map_zones.get(drop) or []):
                sig = str(layout.get("sig") or "")
                old = merged.get(sig)
                if old is None or str(layout.get("seen_at") or "") > str(old.get("seen_at") or ""):
                    merged[sig] = layout
            if merged:
                self.map_zones[keep] = sorted(
                    merged.values(), key=lambda x: str(x.get("seen_at") or ""),
                    reverse=True)[:12]
            self.map_zones.pop(drop, None)
            # 地图校准: keep 没有就用 drop 的(同一张图, 矩形是一样的)
            drop_calib = self.map_calib.pop(drop, None)
            if drop_calib and not self.map_calib.get(keep):
                self.map_calib[keep] = drop_calib
            # 关卡代号关联: 别把用户指认的别名弄丢(drop 认领的关卡归到 keep)
            drop_level = self.map_levels.pop(drop, None)
            if drop_level and not self.level_of_hash(keep):
                self.map_levels[keep] = drop_level
            self._rebuild_levels()
            # 记录的 `map_key` / `map_name` 变了 → 那几条会话要重写(不碰点数据)
            for rec in [*moved, *moved_g]:
                self._dirty_ids.add(rec.route_id)
            self._dirty_meta = True
        self.save()
        log.info("已合并地图条目: %s ← %s (%d 条轨迹)", keep, drop, len(moved))
        return True

    def ordered_map_keys(self) -> list[str]:
        """按首次见到顺序返回全部地图键 —— **已按图片哈希归并**。

        回放键 `wrpl_<关卡代号>` 若已被某个图片哈希认领, 就不单独占一行(它的数据
        已经归到那张图下了); 解除关联后自动恢复 Ù。
        """
        keys: list[str] = []
        for k in [*self.known_maps,
                  *(r.map_key for r in [*self.routes, *self.globals])]:
            k = self.canon(k)
            if k and k not in keys:
                keys.append(k)
        return keys

    def grouped(self) -> dict[str, list[RouteRecord]]:
        """{地图键: [线路...]}(组内按时间倒序, 最新的在前)。

        只包含**有记录**的地图 —— 只缓存了图片、还没录过轨迹的地图不占侧栏一行
        (那类地图在菜单「地图管理」里看/命名)。
        """
        out: dict[str, list[RouteRecord]] = {}
        for key in self.ordered_map_keys():
            rows = sorted(
                (r for r in self.routes if self.canon(r.map_key) == key),
                key=lambda r: r.created_at, reverse=True,
            )
            if rows:
                out[key] = rows
        return out

    # ------------------------------------------------------------------ 战区/模式
    # ------------------------------------------------------------------ 地图校准
    def calib_of(self, map_key: str) -> MapCalib | None:
        """取这张图的校准(没有/无效返回 None)。

        入口先归一到图片哈希 —— 这样回放导入时拿 `wrpl_<关卡代号>` 也查得到
        用户在「地图管理」里给那张图装的校准。
        """
        if not map_key:
            return None
        key = self.canon(map_key)
        with self._lock:
            raw = self.map_calib.get(key)
        if not isinstance(raw, dict):
            return None
        calib = MapCalib.from_dict(raw)
        return calib if calib.valid else None

    def set_calib(self, map_key: str, calib: MapCalib) -> bool:
        """记下这张图的校准并写盘。返回是否真的变了。"""
        if not map_key or calib is None or not calib.valid:
            return False
        map_key = self.canon(map_key)
        data = calib.to_dict()
        with self._lock:
            if self.map_calib.get(map_key) == data:
                return False
            self.map_calib[map_key] = data
            if map_key not in self.known_maps:      # 让它出现在侧栏/地图管理里
                self.known_maps.append(map_key)
            self._dirty_meta = True
        self.save()
        log.info("地图校准: %s <- %s (%s)", map_key, calib.source or "?", calib.describe())
        return True

    def clear_calib(self, map_key: str) -> bool:
        """删掉这张图的校准(底图/轨迹都不动)。"""
        map_key = self.canon(map_key)
        with self._lock:
            if map_key not in self.map_calib:
                return False
            self.map_calib.pop(map_key, None)
            self._dirty_meta = True
        self.save()
        log.info("已清除地图校准: %s", map_key)
        return True

    def remember_zones(self, map_key: str, zones, spawns=None) -> bool:
        """记下某地图的战区布局 + **重生位置**(按坐标串去重)。返回是否是新布局。

        必须按指纹去重: 同一张图的**不同模式**战区数量/位置不同, 它们是不同的布局,
        而同一模式的多次对局指纹相同(只刷新 seen_at)。

        重生位置(`spawns`)跟着布局一起存 —— 它同样随模式/版本变(同一张图的
        “区域1/区域2”双方重生位置不同), 所以同一个布局的多次对局只更新一份,
        内容真的变了才写盘(战局内每帧都会调到这里)。
        """
        if not map_key or not zones:
            return False
        sig = zones_signature(zones)
        if not sig:
            return False
        spawn_json = spawns_to_json(spawns or [])
        with self._lock:
            layouts = self.map_zones.setdefault(map_key, [])
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            is_new = True
            for item in layouts:
                if item.get("sig") == sig:
                    is_new = False
                    item["seen_at"] = now
                    # 重生位置没变就不写盘(否则每帧一次存盘太浪费)
                    if spawn_json and item.get("spawns") != spawn_json:
                        item["spawns"] = spawn_json
                    break
            if is_new:
                layouts.insert(0, {
                    "sig": sig,
                    "count": len(zones),
                    "mode": zone_mode_name(len(zones)),
                    # 存位置 + 当次归属色(供以后录像解析参考); **不存 blink** —— 那只是
                    # “当时正在被占”的瞬时状态, 存下来会让旧布局一直闪
                    "zones": [{k: v for k, v in z.items() if k != "blink"}
                              for z in zones_to_json(zones)],
                    "spawns": spawn_json,      # 重生位置(地图上画空心方块的就是它们)
                    "seen_at": now,
                })
                del layouts[12:]                 # 单张地图最多留 12 种布局
            self._dirty_meta = True
        self.save()
        return is_new

    # ------------------------------------------------------------------ 战区布局
    def zone_layouts(self, map_key: str) -> list[dict]:
        """某地图已见过的战区布局(新的在前)。"""
        return list(self.map_zones.get(self.canon(map_key)) or [])

    def layout_for(self, map_key: str, signature: str = "") -> dict | None:
        """取某地图的战区布局: 优先精确匹配指纹, 否则用**最近见过**的那张。

        2026-10-22 修正: 回退原先取 `layouts[0]`, 但那只是“最后**新出现**的布局” ——
        `remember_zones` 只在新布局插入队首, 老布局被再次看到时仅刷新 `seen_at`,
        于是用户先录一张单路的“区域 1”再录 AB 队的“区域 2”(区域 2 历史上见过),
        回放 AB 队时却拿到“区域 1”的战区位置。改为按 `seen_at` 取最新的。
        """
        layouts = self.map_zones.get(self.canon(map_key)) or []
        if signature:
            for item in layouts:
                if item.get("sig") == signature:
                    return item
        if not layouts:
            return None
        # seen_at 是 `%Y-%m-%dT%H:%M:%S` 字串, 字典序即时间序
        return max(layouts, key=lambda it: str(it.get("seen_at") or ""))

    def grouped_by_mode(self) -> dict[str, list[tuple[str, list[RouteRecord]]]]:
        """{地图键: [(模式名, [轨迹...]), ...]} —— 侧栏三层树用。

        同一张图的不同模式分开成组(模式按组内最新一条的时间倒序),
        这样“同一张图跑不同模式的线路”不会混在一起。
        """
        out: dict[str, list[tuple[str, list[RouteRecord]]]] = {}
        for key, rows in self.grouped().items():
            buckets: dict[str, list[RouteRecord]] = {}
            order: list[str] = []
            for rec in rows:            # rows 已按时间倒序 → 最近跑的模式排前面
                name = rec.mode_label
                if name not in buckets:
                    buckets[name] = []
                    order.append(name)
                buckets[name].append(rec)
            out[key] = [(name, buckets[name]) for name in order]
        return out
