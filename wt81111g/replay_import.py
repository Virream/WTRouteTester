"""把 `.wrpl` 回放导入成路线测试器的「全局条目」。

分工（2026-10-25 定）：**整局复盘读回放**（一次就有两队全部单位、不用 A/B 两次录制、
不用对轴、文件头直接给地图名），**单条路线测量继续读 8111**（25 Hz 实时）。

坐标：回放给的是**米**（x 东 / z 北 / 高度），本模块把它归一化到 0~1 存进
`UnitLife.points` —— 与 8111 录制的那套归一化坐标同构，所以绘制/回放/悬停
全部可以复用，不需要改渲染。

归一化的范围: **优先用 8111 的地图校准**(`map_min`/`map_max`, 见 `map_calib.py`),
这样回放坐标与底图像素**一一对应**(实测 finland: 1 像素 = 1 米)。
没有校准的图退回**所有样本的 0.1%~99.9% 分位**估算(回放里有远处机场 AI 防空,
坐标能到 ±20 km, 直接用 min/max 会把战场压成一小块)—— 那种情况比例与偏移都不准,
界面上会提示用户去「地图管理」导入校准。
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass

from .map_calib import MAP_KEY_PREFIX, MapCalib, level_of, load_bundled
from .route_store import GlobalRecord, TrackPoint, UnitLife
from .wrpl_reader import EXT_MASK_UID, ReplayData, load_replay
from .zones import mode_name as zone_mode_name

log = logging.getLogger("replay_import")

# 归一化后仍超出 [0,1] 的点按边界截断（远处 AI 会落在框外）
GAP_SPLIT_SEC = 45.0        # 相邻采样超过这么久 → 认为是两段（死了/长时间无上报）
JUMP_MIN_M = 200.0          # 一步至少跳这么远
JUMP_MAX_MPS = 60.0         # 且折算速度超过这个（216 km/h）→ 判定为换了实体
# 实测依据（2026-10-25，avg_finland 服务器回放）: uid=142 在 t=404.6s 一步跳了
# 370.9 m / 3.52 s = 105 m/s(380 km/h) —— 地面车最快也就 150 km/h(42 m/s)，
# 那就是“玩家在车里操作微型侦察无人机 / 死后重生”造成的 uid 复用。阈值取
# 60 m/s: 正常行驶要在 200 m 内平均 216 km/h 才会误判，实际不可能。


@dataclass
class ImportStats:
    """导入结果摘要（给状态栏/对话框显示）。"""
    level: str = ""
    battle_type: str = ""
    server: bool = False
    parts: int = 1
    packets: int = 0
    failed_packets: int = 0
    samples: int = 0
    units: int = 0
    lives: int = 0
    duration: float = 0.0
    dropped: int = 0           # 太短的线路（< 3 个点）丢弃数
    players: int = 0           # 解出的玩家数（Slot 包）
    kills: int = 0             # 击杀事件数（Kill 包）
    mapped: int = 0            # 能对应到玩家的载具数
    air_units: int = 0         # 飞机/直升机数（ptype=2）
    air_lives: int = 0         # 飞机生成的线路数
    damages: int = 0           # 严重伤害事件数（用来补 uid ↔ 玩家）
    deaths: int = 0            # 判定为“被击毁”的线路数
    ecs_vehicles: int = 0      # ECS 解出的玩家载具数
    ecs_bound: int = 0         # 其中成功绑定玩家/阵营的
    ecs_group: int = 0         # 出生组每组人数 K（16v16 时为 16）
    calibrated: bool = False   # 是否用了 8111 地图校准(世界米 → 底图像素)
    calib_note: str = ""       # 校准来源/尺寸描述(给导入报告用)


def _robust_range(values: list[float]) -> tuple[float, float]:
    """用 **0.1%~99.9% 分位**当范围。

    以前用 1%~99%：实测本地那局有 **1.88% 的点（2,848 个）落在框外**，而
    `build_record` 又把归一化结果 `min/max` 夹到 0~1 → 玩家自己绕着重生点后方
    走那一段就被夹成一条贴着地图边框的假直线（2026-10-25 用户反馈 A22/A49）。
    换到 0.1%/99.9% 后同样的数据框外 0 点，范围与全量 min/max 一致，而真正的
    离群值（远处机场 AI 防空，能到 ±20 km）仍然被挤掉。
    """
    if not values:
        return 0.0, 1.0
    s = sorted(values)
    n = len(s)
    lo, hi = s[int(n * 0.001)], s[min(n - 1, int(n * 0.999))]
    if hi - lo < 100.0:               # 分位太窄（几乎没动）→ 退回最小/最大
        lo, hi = s[0], s[-1]
    if hi - lo < 1e-6:
        hi = lo + 1.0
    return lo, hi


def replay_map_key(level: str) -> str:
    """关卡名 → 地图键。用 `wrpl_<level>` 而**不是** `wrpl:<level>` ——
    底图缓存以 `<地图键>.<ext>` 存盘, 冒号在 Windows 上是非法文件名字符。
    """
    return f"{MAP_KEY_PREFIX}{level or 'unknown'}"


def resolve_calib(store, level: str) -> tuple[MapCalib | None, str]:
    """找这张图的校准: 用户导入的(store) 优先, 其次随软件预置的。

    返回 `(校准, 来源说明)`; 都没有时返回 `(None, "")`。
    """
    key = replay_map_key(level)
    try:
        calib = store.calib_of(key) if store is not None else None
    except AttributeError:
        calib = None
    if calib is not None:
        return calib, "地图管理里导入的校准"
    calib = load_bundled(level_of(key))
    if calib is not None:
        return calib, "随软件预置的校准"
    return None, ""


def _time_from_name(path: str) -> str:
    """从 `#2026.10.24 22.45.13.wrpl` 这类文件名里取录制时刻；取不到用当前时间。"""
    m = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})[ _](\d{2})[.\-:](\d{2})[.\-:](\d{2})",
                  os.path.basename(path))
    if m:
        y, mo, d, h, mi, se = m.groups()
        return f"{y}-{mo}-{d}T{h}:{mi}:{se}"
    return time.strftime("%Y-%m-%dT%H:%M:%S")


AIRFIELD_MIN_UNITS = 3          # 少于这么多阵地就不认（不是机场防空）
RUNWAY_LEN_SCALE = 1.5          # 主轴跨度 → 跑道长度（阵地只占跑道中段）
RUNWAY_LEN_MIN = 1500.0
RUNWAY_LEN_MAX = 4500.0
RUNWAY_W_RATIO = 0.10           # 长宽比 10:1：真实跑道约 45 m，那样在图上只有 1px
HELIPAD_SIZE = 320.0            # 直升机坪画成小方块（真实约 100 m，画小就看不见）


def _airfields_from_ecs(data: ReplayData) -> list[dict]:
    """从 ECS 里「机场防空」AI 的阵地估出两队机场跑道（用户 2026-10-26 要求）。

    **为什么只能估**: 机场本体(`tN_airfield`)的坐标和跑道朝向在关卡对象包(`0xD039`)
    里 —— 要解 36B 旋转矩阵 + 12B 平移才拿得到。而 `tN_airfield_aaa_*`（机场防空）
    是普通 ECS 载具，位置现成。这些阵地是沿跑道两侧和两端摆的，于是：

    * 它们的**质心** ≈ 跑道中心（实测与关卡对象里的机场坐标只差约 100 m）；
    * 它们的**主轴**（协方差主特征向量）≈ 跑道朝向；
    * 主轴上的**跨度**比跑道短（阵地只覆盖跑道中段），乘 `RUNWAY_LEN_SCALE` 补回来。

    这些数字**都是估的**，只用来在图上示意"机场在那一侧、朝哪个方向"，不要当量具。

    ⚠ `tN_` 前缀与队伍编号是**反的**（`t1_` 属 team 2），与 `_team_by_takeoff` 一致。
    """
    runway: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
    helipad: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
    for veh in getattr(data, "vehicles", ()):
        cls = (getattr(veh, "cls", "") or "").lower()
        m = re.match(r"^t([12])_", cls)
        if not m or not cls:
            continue
        kind = "helipad" if "helipad" in cls else (
            "airfield" if "airfield" in cls else "")
        if not kind:
            continue
        rows = data.units.get(getattr(veh, "uid", 0))
        if not rows:
            continue
        _t, x, _y, z = rows[0]
        (helipad if kind == "helipad" else runway)[int(m.group(1))].append((x, z))

    out: list[dict] = []
    for prefix, pts in runway.items():
        if len(pts) < AIRFIELD_MIN_UNITS:
            continue
        side = "B" if prefix == 1 else "A"        # t1_ 属 team 2
        n = float(len(pts))
        cx = sum(p[0] for p in pts) / n
        cz = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - cx) ** 2 for p in pts) / n
        szz = sum((p[1] - cz) ** 2 for p in pts) / n
        sxz = sum((p[0] - cx) * (p[1] - cz) for p in pts) / n
        theta = 0.5 * math.atan2(2.0 * sxz, sxx - szz)
        dx, dz = math.cos(theta), math.sin(theta)
        proj = [(p[0] - cx) * dx + (p[1] - cz) * dz for p in pts]
        span = max(proj) - min(proj)
        length = min(RUNWAY_LEN_MAX, max(RUNWAY_LEN_MIN, span * RUNWAY_LEN_SCALE))
        mid = (max(proj) + min(proj)) / 2.0       # 阵地可能偏在一头，中心要跟着挪
        out.append({
            "side": side, "kind": "runway",
            "x": cx + mid * dx, "z": cz + mid * dz,
            "dx": dx, "dz": dz,
            "length": length, "width": max(120.0, length * RUNWAY_W_RATIO),
        })
    for prefix, pts in helipad.items():
        if not pts:
            continue
        side = "B" if prefix == 1 else "A"
        n = float(len(pts))
        out.append({
            "side": side, "kind": "helipad",
            "x": sum(p[0] for p in pts) / n, "z": sum(p[1] for p in pts) / n,
            "dx": 1.0, "dz": 0.0,
            "length": HELIPAD_SIZE, "width": HELIPAD_SIZE,
        })
    return out


def build_record(data: ReplayData, source_path: str = "",
                 calib=None) -> tuple[GlobalRecord, ImportStats]:
    """`ReplayData` → `GlobalRecord`。

    `calib` 是这张图的地图校准(`MapCalib`, 见 `map_calib.py`)。回放里的坐标是
    **米制世界坐标**, 而地图控件要的是 0~1 归一化: 只有拿到 8111 的
    `map_min`/`map_max` 才能准确落到 2048² 底图上。没有校准时退回老办法
    ——用本局观测到的 0.1%/99.9% 分位范围估算, 比例/偏移都不准。
    """
    src = source_path or data.source
    stats = ImportStats(
        level=data.level, battle_type=data.battle_type, server=data.server,
        parts=data.parts, packets=data.packets,
        failed_packets=data.failed_packets, samples=data.sample_count,
        units=len(data.units), players=len(data.players), kills=len(data.kills),
        damages=data.damages,
    )

    calibrated = bool(calib is not None and getattr(calib, "valid", False))
    if calibrated:
        # 用 8111 的地图外框: 直接就是底图覆盖的世界矩形, 不需要任何估算
        minx, maxx = float(calib.min_x), float(calib.max_x)
        minz, maxz = float(calib.min_z), float(calib.max_z)
        stats.calibrated = True
        stats.calib_note = calib.describe()
    else:
        xs, zs = [], []
        for rows in data.units.values():
            for _t, x, _y, z in rows:
                xs.append(x)
                zs.append(z)
        if not xs:
            raise ValueError("回放里没有解析到任何地面单位位置")
        minx, maxx = _robust_range(xs)
        minz, maxz = _robust_range(zs)
        # 两侧各留 5% 余量：分位范围之外的那一点点（玩家绕到重生点后方、贴边绕圈）
        # 不至于跑出画布 —— 地图控件是 x*w 直接换算，超出的部分会被裁掉、看起来像断线
        padx, padz = (maxx - minx) * 0.05, (maxz - minz) * 0.05
        minx, maxx = minx - padx, maxx + padx
        minz, maxz = minz - padz, maxz + padz
    spanx, spanz = maxx - minx, maxz - minz
    if spanx <= 0 or spanz <= 0:
        raise ValueError("地图范围无效(宽/高为 0) —— 回放里没有可用坐标")

    # `map_name` = **只放地图名**(侧栏最上层那一行就用它)。模式已经单独存在
    # `mode_name` 里(由战区数推出), 拼进来会变成“avg_finland (finland_Dom)”
    # 这种带内部串的名字。
    map_name = data.level or "未知地图"

    # 模式名: 8111 的规则是「战区数量 → 模式」(3=战区控制 / 2=战区攻陷 /
    # 1=战区争夺)。回放里战区坐标是占位值, 但 `cz_circle_0N` 的**个数**能数出来,
    # 所以照样推得出模式 —— 以前直接把回放头的 `finland_Dom` 当模式名塞进去,
    # 侧栏就显示成 `finland_Dom` 这种内部串(用户 2026-10-26 反馈树结构不对)。
    _zc = data.zone_count
    rec = GlobalRecord(
        route_id="",                       # 由 store.new_global 之外的地方补; 这里直接给
        map_key=replay_map_key(data.level),
        map_name=map_name,
        created_at=_time_from_name(src) if src else
                   time.strftime("%Y-%m-%dT%H:%M:%S"),
        zone_count=_zc,
        zone_signature="",
        mode_name=zone_mode_name(_zc) if _zc else (data.battle_type or ""),
        duration=max(0.0, data.t_last - data.t_first),
    )
    rec.route_id = f"wrpl{int(time.time() * 1000) % 10 ** 10:010d}"
    # 记住归一化用的世界矩形: 以后这图补上/换了校准, 可以**原地重算**坐标
    # (见 `renormalize_map_records`), 不用让用户重新导入回放。
    rec.norm_rect = [minx, maxx, minz, maxz]
    # 两队机场跑道(示意用, 见 `_airfields_from_ecs`)。存世界米, 绘制时才归一化 ——
    # 这样换校准时它跟着线路一起重算。
    rec.airfields = _airfields_from_ecs(data)

    t0 = data.t_first
    # ---- 玩家身份与真实分队 ------------------------------------------------
    # Slot 包给「玩家槽位 → 名字/队伍」，Kill 包给「载具 uid ↔ 玩家槽位」+
    # 「uid → 载具型号」。都不需要 ECS 哈希表（详见 wrpl_events.py 的模块注释）。
    # 覆盖不到的车（从没参与过击杀/被击毁）只能归到 A 队、名字就叫 uidN
    # —— 实测服务器回放能覆盖 73%（69/94）。
    teams = {p.slot: p.team for p in data.players.values()}
    names = {p.slot: p.display for p in data.players.values()}
    vehicle_of: dict[int, str] = dict(data.uid_vehicle)
    for ev in data.kills:
        if ev.killer_uid != EXT_MASK_UID and ev.vehicle:
            vehicle_of.setdefault(ev.killer_uid, ev.vehicle)

    # ---- 载具档案（ECS）--------------------------------------------------
    # 这是「这辆车是谁的」的**权威来源**：出生组名 `tN_playerMM_K` 在构造块里是
    # **明文**，而槽位公式（t1 -> NN-1, t2 -> NN-1+K）已用击杀事实 32/32 验证。
    # 以前只能从击杀事件反推，覆盖不到 3 成，而且 uid 复用时会张冠李戴。
    vans: dict[int, list] = {}
    for v in getattr(data, "vehicles", ()):
        vans.setdefault(v.uid, []).append(v)
    for _lst in vans.values():
        _lst.sort(key=lambda v: v.born_t)

    def _side(uid: int) -> str:
        pid = data.uid_pid.get(uid)
        return "B" if teams.get(pid) == 2 else "A"

    def _label(uid: int) -> str:
        pid = data.uid_pid.get(uid)
        who = names.get(pid) if pid is not None else None
        car = vehicle_of.get(uid)
        if who:
            return f"{who} · {car}" if car else who
        return car or f"uid{uid}"

    def _split_jumps(rows: list) -> list[list]:
        """把一段轨迹按「长时间无上报」或「一步瞬移」切开。"""
        out: list[list] = []
        cur: list = []
        for item in rows:
            if cur:
                dt = item[0] - cur[-1][0]
                d = ((item[1] - cur[-1][1]) ** 2
                     + (item[3] - cur[-1][3]) ** 2) ** 0.5
                if dt > GAP_SPLIT_SEC or (d > JUMP_MIN_M and dt > 0
                                          and d / dt > JUMP_MAX_MPS):
                    out.append(cur)
                    cur = []
            cur.append(item)
        if cur:
            out.append(cur)
        return out

    # ---- 载具死亡时刻（来自 Kill 包）--------------------------------------
    # 服务器回放 24 Hz 全量上报，所以“uid 不再出现”本身就能看出车没了；但 kill 包
    # 里的 `VictimUid + 时间戳` 是**精确到包**的击毁时刻，而且能区分“死了”与
    # “看不见”。两条信息一起用：
    #   * 死亡时刻是切段点（uid 是槽位，死后可能被新车复用）；
    #   * 落在段内的死亡时刻 → `dead=True` + `death_t`（地图上画红叉）。
    deaths: dict[int, list[float]] = {}
    for ev in data.kills:
        if ev.victim_uid != EXT_MASK_UID and ev.t > 0:
            deaths.setdefault(ev.victim_uid, []).append(ev.t)
    for _v in deaths.values():
        _v.sort()

    # ---- 每辆车的**有效终点** = min(ECS 销毁, 被击毁那一刻) -------------------
    # ECS 的 `0x26` 销毁包对**残骸常常根本不发**(残骸就留在场上), 此时
    # `_fill_end_times` 只能把 `died_t` 兜底成对局结束(`died_by == "end"`)。
    # 只看这个窗口就会把「冻结的残骸」整条留在线上 —— 实测 uid 30/41/40/53/34/32
    # 在 79~194s 被击毁, 之后 **1.9~2.2 万个采样全在同一个坐标**(跨度 0~5 m), 一直
    # 报到 1005s, 于是 6 条幽灵线拖了整局。
    #
    # 击杀包里的 `VictimUid + 时间戳` 正好补上这个缺口: **被打死的那一刻就是这辆
    # 车的终点**, 之后的位置流全是残骸。取窗口内**第一次**击杀(之后的属于 uid 复用)。
    def _eff_end(v) -> tuple[float, str]:
        """载具档案 -> (有效终点秒, 理由)。理由取 `kill` / `ecs` / `next` / `end`。"""
        end = v.died_t if v.died_t is not None else 1e9
        for t in deaths.get(v.uid, ()):
            if v.born_t - 1.0 <= t <= end:
                return t, "kill"
        return end, (v.died_by or "ecs")

    _end_of: dict[int, float] = {}        # id(Vehicle) -> 有效终点秒
    _end_by: dict[int, str] = {}          # id(Vehicle) -> 理由

    def _veh_ends(m: dict[int, list]) -> dict[int, list[tuple[float, float, str]]]:
        """uid -> [(born, 有效终点, 理由)]; 同时填好按 id 查的表。"""
        out: dict[int, list[tuple[float, float, str]]] = {}
        for _u, _lst in m.items():
            for _v in _lst:
                _e, _by = _eff_end(_v)
                _end_of[id(_v)] = _e
                _end_by[id(_v)] = _by
                out.setdefault(_u, []).append((_v.born_t, _e, _by))
            out[_u].sort()
        return out

    def _tol_of(by: str) -> float:
        """窗口容差: 击杀端点只给 0.5 s, 其余 2 s(见地面循环里的说明)。"""
        return 0.5 if by == "kill" else 2.0

    g_ends = _veh_ends(vans)

    for uid, rows in sorted(data.units.items()):
        # 车被击毁后位置流**还会继续上报残骸**(实测某 uid 击毁于 147.7s, 之后
        # 12,641 个点全在同一个坐标, 一直报到 1012s), 而残骸本身仍可以被撞得
        # 滑动几米 —— 所以**不能用“有没有位移”去猜**, 而是按存活窗口裁:
        # 窗口 = `[ECS 创建, min(ECS 销毁, 被击毁时刻)]`(见 `_eff_end`)。
        # 窗口之外的采样必然属于别的东西(残骸 / uid 复用)。
        # 实测三份回放里**每个有采样的 uid 都有 ECS 档案**(0 个孤儿), 所以这条够用。
        _win = g_ends.get(uid, ())
        if _win:
            # 击杀端点只给 0.5 s 容差: 残骸本来就从击杀那一刻开始, 给 2 s 会多出一
            # 截几十个点的碎屑段(击杀时刻也是切段点, 那截就成了独立的 `_2` 线)。
            rows = [r for r in rows
                    if any(a - _tol_of(_by) <= r[0] <= b + _tol_of(_by)
                           for a, b, _by in _win)]
        # 切段点: uid 被复用后, 前一辆车「真正结束」的那一刻就是分界(比“看不见了”
        # 精确, 也比速度跳变检测稳)。`died_by == "end"` 且没被击杀的 = 真的活到
        # 对局结束(或 AI), 不能切。
        #
        # ⚠ 击杀时刻在这里**也会**成为切段点, 但不会有“10 秒残骸被切成第二条线”
        #   的老问题: 采样已经被上面的窗口裁到 `min(died, kill)` 了(用户 2026-10-25
        #   反馈的 4 条 cn_ztz_96b 就是当年在击杀时刻切、却没裁采样造成的)。
        _wins = [(a, b) for a, b, _by in _win]

        def _covered(t: float) -> bool:
            return any(a - 1.0 <= t <= b + 1.0 for a, b in _wins)

        dl = [t for t in deaths.get(uid, ()) if not _covered(t)]
        for _a, _b, _by in _win:
            # "end" = 到对局结束还在, 不是死亡
            if _b < 1e8 and _by != "end":
                dl.append(_b)
        dl.sort()
        di = 0
        segs: list[tuple[list[tuple[float, float, float, float]], float]] = []
        cur: list[tuple[float, float, float, float]] = []
        for item in rows:
            t = item[0]
            # 越过了某个击毁时刻 → 当前段到此为止，后面属于新载具
            while di < len(dl) and dl[di] <= t:
                if cur:
                    segs.append((cur, dl[di]))
                    cur = []
                di += 1
            if cur:
                dt = item[0] - cur[-1][0]
                d = ((item[1] - cur[-1][1]) ** 2 + (item[3] - cur[-1][3]) ** 2) ** 0.5
                # ① 隔了很久没上报（阵亡/重生）
                # ② **uid 被复用**: 回放的 uid 是“场上槽位”而不是唯一实体 —— 玩家在车里
                #    操作微型侦察无人机、或者死后重生，同一个 uid 前后是两台不同的东西，
                #    时间上却是连续的 → 不切开就会在图上拉出一条瞬移直线
                #    (2026-10-25 用户反馈 A67)。地面车跑不到 720 km/h，所以
                #    “一步 > 200 m 且 > 200 m/s” 一定是换了实体。
                #
                # ⚠ **只在没有 ECS 档案时才用启发式**(2026-10-27): 档案已经给了精确的
                #   出生/结束时刻, 窗口内再按间隔/速度硬切, 只会把**一辆车**劈成两条线
                #   (`A12_1` / `A12_2`)。有档案就完全听档案的。
                if not _win and (dt > GAP_SPLIT_SEC
                                 or (d > JUMP_MIN_M and dt > 0
                                     and d / dt > JUMP_MAX_MPS)):
                    segs.append((cur, 0.0))
                    cur = []
            cur.append(item)
        if cur:
            segs.append((cur, 0.0))
        # 段的归属：用**段首时刻**在这段轨迹的 ECS 档案里查。同一个 uid 会被复用
        # （死后重生 / 操作微型无人机 / **同一玩家又出了同型号的车**），前后段可能
        # 属于不同玩家、也可能是同一玩家的两台不同载具。
        #
        # 必须先把所有段的归属解析出来，才能「**一辆车一个基号**」地编号：
        #   * 以前按 uid 分配 -> 同一 uid 先后两辆车(实测 uid=61 两辆 cn_ztz_96b)
        #     被合并成"1 台"，用户从界面上完全看不出是两辆；
        #   * 更不能按"段"分配 -> 一辆车被切成多段就变成 A25_1/A26_2/A27_3。
        seg_src: list = []
        _cands = vans.get(uid, ())
        for seg, _dth in segs:
            _ts = seg[0][0]
            _veh = None
            # ⚠ 用**有效终点** `_end_of`(已含击杀/下一台车兑底), 不是原始的 `died_t`。
            #   以前用 `died_t`: 它为 None 时条件是 `ts <= 1e9`, 于是**后面所有段都被
            #   第一台车吞掉** —— 同一 uid 的第二条命会挂到前一台车名下(型号/玩家都错),
            #   而且两台车只拿一个基号。
            #   同一时刻落进多个窗口时取**最晚出生**那台(越兑越近)。
            for _cand in _cands:
                _end = _end_of.get(id(_cand), 1e9)
                if _cand.born_t - 1.0 <= _ts <= _end + 1.0:
                    if _veh is None or _cand.born_t > _veh.born_t:
                        _veh = _cand
            seg_src.append(_veh)
        seg_total: dict[int, int] = {}
        for _v in seg_src:
            _k0 = id(_v) if _v is not None else -1
            seg_total[_k0] = seg_total.get(_k0, 0) + 1
        _base: dict[int, int] = {}
        _segno: dict[int, int] = {}
        for si, (seg, death_at) in enumerate(segs, 1):
            # 太短/几乎没动的段直接丢弃: 以前只验点数 >= 3，结果那辆“只被看见 1.4 秒”
            # 的车留在地图上就是一个孤立小点（用户 2026-10-25: 两个角上有 A1/A11）
            if len(seg) < 3:
                stats.dropped += 1
                continue
            _dur = seg[-1][0] - seg[0][0]
            _span = max(max(q[1] for q in seg) - min(q[1] for q in seg),
                        max(q[3] for q in seg) - min(q[3] for q in seg))
            if _dur < 3.0 and _span < 50.0:
                stats.dropped += 1
                continue
            # 死亡时刻没落在切段点上（例如它比最后一个采样点晚了 1~2 秒）
            # 也算击毁 —— 但时间差要短，否则可能只是“没被看见”。
            # 窗口放到 20 秒: 最后一个采样往往落在**击杀那一刻**, 而 ECS 的销毁时刻
            # 还要再晚 10 秒左右(残骸在被销毁前一直上报), 否则阵亡标记会全部消失。
            dead = bool(death_at)
            if not dead and dl:
                tail = seg[-1][0]
                for d0 in dl:
                    if tail <= d0 <= tail + 20.0:
                        dead, death_at = True, d0
                        break
            if dead:
                stats.deaths += 1
            pts: list[TrackPoint] = []
            prev = None
            last_speed = 0.0
            for t, x, y, z in seg:
                nx = (x - minx) / spanx
                ny = (maxz - z) / spanz          # 北 = 图上方（与 8111 的归一化同向）
                speed = last_speed
                if prev is not None:
                    dt = t - prev[0]
                    if dt > 1e-6:
                        d = ((x - prev[1]) ** 2 + (z - prev[2]) ** 2) ** 0.5
                        if d < 2000.0:           # 跳变（换了 uid/传送）不计速度
                            kmh = d / dt * 3.6
                            # 地面单位跑不到 150 km/h: 超过就是坐标抖动(高频通道的
                            # 亚米级噪声 / uid 复用), 沿用上一个有效值而不是造尖峰
                            if kmh <= 150.0:
                                speed = last_speed = kmh
                pts.append(TrackPoint(t=round(max(0.0, t - t0), 2),
                                      # **不夹到 0~1** —— 夹了会在边框上拉出一条假直线；
                                      # 真超出范围的点就让它画到画布外（自然被裁掉）
                                      # ⚠ 这里 round 一次就够: 保存时不再 round
                                      # (80 万点 × 4 次 round 要 1 秒, 见 route_store._pack_point)
                                      x=round(nx, 5),
                                      y=round(ny, 5),
                                      speed=round(speed, 1)))
                prev = (t, x, z)
            side = _side(uid)
            veh = seg_src[si - 1]           # 上面已按段首时刻解析好
            if veh is not None and veh.team:
                side = "B" if veh.team == 2 else "A"
            if veh is not None and veh.player:
                stats.mapped += 1
            elif data.uid_pid.get(uid) is not None:
                stats.mapped += 1
            # 基号按**这一辆车**（ECS 档案条目）分配，不是按 uid —— 这样同一 uid
            # 先后两辆车会拿到两个不同基号，侧栏台数才准。
            _k = id(veh) if veh is not None else -1
            if _k not in _base:
                _base[_k] = len(rec.sides[side]) + 1
            _segno[_k] = _segno.get(_k, 0) + 1
            # 归属兜底：ECS 档案没命中时，用 uid->pid 的名字让侧栏还能归组
            # （这条只用于显示分组，`bind` 仍然是 none，不算“已解析出玩家”）。
            _who = (veh.player if veh is not None and veh.player
                    else (names.get(data.uid_pid.get(uid)) or ""))
            life = UnitLife(
                life_id=f"{rec.route_id}:{side}{_base[_k]}"
                        + (f"_{_segno[_k]}" if seg_total.get(_k, 1) > 1 else ""),
                icon=veh.label if veh is not None else _label(uid),
                player=_who,
                vehicle_cls=(veh.short_cls if veh is not None
                             else (vehicle_of.get(uid) or "")),
                obj_type="ground_model",
                color="#174DFF" if side == "A" else "#FF4D4D",
                map_key=rec.map_key,
                points=pts,
                side=side,
                dead=dead,
                death_t=max(0.0, death_at - t0) if dead else 0.0,
            )
            rec.sides[side].append(life)
            stats.lives += 1
    stats.duration = rec.duration
    # ---- 飞机 / 直升机 ------------------------------------------------------
    # `ptype=2`（FlightModel，4 Hz）。uid 与地面**共用同一套 11 位编号**（同一时刻
    # 不会重复），所以可以拿 ECS 的 `aircraft+player_unit` 档案把空中航迹绑到玩家与
    # 阵营上（实测 14/16 命中；剩下的 `ucav_recon_micro_flir` 侦察无人机本来就没有
    # 出生组名，属正常）。以前这里硬编码 `side="A"`，于是所有飞机都被算到 A 队。
    #
    # 位置归一化沿用**地面**算出来的范围 —— 飞机常飞到 20 km 外的机场，若把它算进
    # 范围会把战场压成一小块；超出的点自然画到画布外。
    air_vans: dict[int, list] = {}
    for _v in getattr(data, "air_vehicles", ()):
        air_vans.setdefault(_v.uid, []).append(_v)
    for _lst in air_vans.values():
        _lst.sort(key=lambda v: v.born_t)
    a_ends = _veh_ends(air_vans)
    _air_base: dict[int, int] = {}

    # 机场群质心：AI 机场防空的任务对象名 `t1_`/`t2_` 与队伍编号**是反的**
    # (`t1_` 属 team 2)。靠这个给「没有出生组名、也没有击杀证据」的飞机兜底定边 ——
    # 典型就是**核弹机/killstreak 机**: 击杀包里它的 `victim_pid` 是 0(服务器不归属),
    # 但它的起飞点就在自家机场上(实测 468 m), 离对面机场 28 km。
    _afc: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
    for _v in getattr(data, "vehicles", ()):
        _m = re.match(r"^t([12])_", _v.cls or "")
        if not _m or "airfield" not in (_v.cls or "").lower():
            continue
        _rows = data.units.get(_v.uid)
        if _rows:
            _afc[int(_m.group(1))].append((_rows[0][1], _rows[0][3]))

    def _team_by_takeoff(x: float, z: float) -> int | None:
        """首点离哪个机场群近 -> 阵营。只信「真的很近」的情况(< 3 km 且明显更近),
        否则返回 None —— 巡逻/攻击机常 10 km 外进场, 瞎猜会把颜色弄反。"""
        if not _afc[1] or not _afc[2]:
            return None
        d = {p: min(((x - a) ** 2 + (z - b) ** 2) ** 0.5 for a, b in _afc[p])
             for p in (1, 2)}
        near = 1 if d[1] < d[2] else 2
        far = d[2] if near == 1 else d[1]
        if d[near] < 3000.0 and d[near] * 2.0 < far:
            return 3 - near
        return None

    for auid, rows in sorted(data.air_units.items()):
        # 与地面一样, 只靠存活窗口裁(飞机坠毁后的位置也会继续上报), 窗口同样收紧到
        # `min(ECS 销毁, 被击落时刻)`
        _win = air_vans.get(auid, ())
        if _win:
            rows = [r for r in rows
                    if any(a - _tol_of(_by) <= r[0] <= b + _tol_of(_by)
                           for a, b, _by in a_ends.get(auid, ()))]
        if not rows:
            continue
        # 切段点：长时间没上报 + 「真正结束」的时刻（不含“活到对局结束”）
        cuts = sorted(b for _a, b, _by in a_ends.get(auid, ())
                      if b < 1e8 and _by != "end")
        segs: list[list[tuple[float, float, float, float]]] = []
        cur: list[tuple[float, float, float, float]] = []
        di = 0
        for item in rows:
            while di < len(cuts) and cuts[di] <= item[0]:
                if cur:
                    segs.append(cur)
                    cur = []
                di += 1
            if cur and not _win and item[0] - cur[-1][0] > GAP_SPLIT_SEC:
                segs.append(cur)
                cur = []
            cur.append(item)
        if cur:
            segs.append(cur)
        seg_veh: list = []
        for seg in segs:
            _ts = seg[0][0]
            _veh = None
            for _cand in _win:
                if _cand.born_t - 2.0 <= _ts <= _end_of.get(id(_cand), 1e9) + 2.0:
                    _veh = _cand
                    break
            seg_veh.append(_veh)
        _segn: dict[int, int] = {}
        for si, seg in enumerate(segs, 1):
            if len(seg) < 3:
                stats.dropped += 1
                continue
            # 与地面同一道闸: 太短又几乎没动的段丢掉。坠机后残骸还会上报 1~2 秒
            # (被击落时刻成了切段点, 那截尾巴就单独成段), 留着就是 8 个点的碎屑。
            _dur = seg[-1][0] - seg[0][0]
            _mv = max(max(q[1] for q in seg) - min(q[1] for q in seg),
                      max(q[3] for q in seg) - min(q[3] for q in seg))
            if _dur < 3.0 and _mv < 50.0:
                stats.dropped += 1
                continue
            veh = seg_veh[si - 1]
            _team = veh.team if veh is not None else None
            if _team is None:
                # 兜底：用起飞位置定边（核弹机/侦察机这类没有出生组名的）
                _team = _team_by_takeoff(seg[0][1], seg[0][3])
            if veh is None or _team is None:
                # 阵营解不出来的空中单位（无人侦察机，既没有出生组名、没有交战记录、
                # 起飞点也不在机场）**直接忽略** —— 宁可不要这条线，
                # 也不要把它硬塞进某一队（用户口径）。
                stats.dropped += 1
                continue
            side = "B" if _team == 2 else "A"
            _k = id(veh) if veh is not None else -1
            if _k not in _air_base:
                _air_base[_k] = len(rec.sides[side]) + 1
            _segn[_k] = _segn.get(_k, 0) + 1
            pts = [TrackPoint(t=round(max(0.0, t - t0), 2),
                              x=round((x - minx) / spanx, 5),
                              y=round((maxz - z) / spanz, 5),
                              speed=0.0)
                   for t, x, _y, z in seg]
            # 速度：飞机按相邻点位移算（不设上限，超音速很正常）
            for a, b in zip(pts, pts[1:]):
                dt = b.t - a.t
                if dt > 1e-6:
                    a.speed = ((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5 / dt * 3.6
            _cls = veh.short_cls if veh is not None else ""
            _who = veh.player if veh is not None else ""
            _vend = _end_of.get(id(veh), 1e9) if veh is not None else 1e9
            _vby = _end_by.get(id(veh), "") if veh is not None else ""
            _dead = bool(_vby in ("ecs", "next", "kill")) and _vend < 1e8
            life = UnitLife(
                life_id=f"{rec.route_id}:{side}{_air_base[_k]}"
                        + (f"_{_segn[_k]}" if len(segs) > 1 else ""),
                icon=veh.label if (veh is not None and (veh.player or _cls))
                        else f"✈ uid{auid}",
                player=_who,
                vehicle_cls=_cls,
                obj_type="air_model",
                color="#174DFF" if side == "A" else "#FF4D4D",
                map_key=rec.map_key,
                points=pts,
                side=side,
                dead=_dead,
                death_t=(max(0.0, _vend - t0) if _dead else 0.0),
            )
            rec.sides[side].append(life)
            stats.lives += 1
            stats.air_lives += 1
            if _who:
                stats.mapped += 1

    stats.air_units = len(data.air_units)
    stats.ecs_vehicles = len(getattr(data, "vehicles", ()))
    stats.ecs_bound = sum(1 for v in getattr(data, "vehicles", ())
                          if v.team is not None)
    stats.ecs_group = getattr(data, "ecs_group_size", 0)
    return rec, stats


def renormalize_map_records(store, map_key: str, calib) -> int:
    """把已经导入的记录**重新归一化**到新校准上。返回改了几条记录。

    为什么需要: `points` 里存的是 0~1, 归一化用的范围在导入那一刻就定死了。
    用户在「地图管理」里补上/换掉 8111 校准以后, 旧记录的位置还是按当初的估算
    范围算的 —— 不重算就永远歪着。有了 `norm_rect`(导入时用的世界矩形)就能
    反推出世界米, 再用新校准换算一遍, 不用重新导入回放。

    只处理 `norm_rect` 完整、且属于这张图的记录; 8111 实时录制的不受影响
    (它们本来就是 8111 的归一化坐标)。
    """
    if calib is None or not getattr(calib, "valid", False):
        return 0
    # 地图键要按**图片哈希**归一: 回放记录存的是 `wrpl_<关卡代号>`, 而用户在
    # 「地图管理」里把那张图关联到某个图片哈希之后, 两边就该算同一张图。
    canon = getattr(store, "canon", None) or (lambda k: k)
    target = canon(map_key)
    new_rect = [float(calib.min_x), float(calib.max_x),
                float(calib.min_z), float(calib.max_z)]
    nspanx, nspanz = new_rect[1] - new_rect[0], new_rect[3] - new_rect[2]
    changed = 0
    for rec in getattr(store, "globals", ()):
        if canon(getattr(rec, "map_key", "")) != target:
            continue
        old = getattr(rec, "norm_rect", None) or []
        if len(old) != 4:
            continue
        if [round(v, 3) for v in old] == [round(v, 3) for v in new_rect]:
            continue
        spanx, spanz = old[1] - old[0], old[3] - old[2]
        if spanx <= 0 or spanz <= 0:
            continue
        # 80 万点时要跑几十万次内层循环: 把属性/索引都提到循环外(实测快约 30%)
        ox, otop, sx, sz = old[0], old[3], spanx, spanz
        nx0, ntop, nsx, nsz = new_rect[0], new_rect[3], nspanx, nspanz
        for side in ("A", "B"):
            for life in rec.lives(side):
                for pt in life.points:
                    pt.x = (ox + pt.x * sx - nx0) / nsx
                    pt.y = (ntop - (otop - pt.y * sz)) / nsz
        rec.norm_rect = new_rect
        changed += 1
    if changed:
        store.save()
        log.info("按新校准重算了 %d 条回放记录的位置: %s", changed, map_key)
    return changed


def import_replay(path: str, store, *, progress=None,
                  expect_server: bool | None = None
                  ) -> tuple[GlobalRecord, ImportStats]:
    """解析一个回放（本地文件 / 服务器分片目录）并**落盘**到 `store`。

    一次调用 = 一局:
    * **本地回放**：传单个 `.wrpl` 文件（一个文件就是完整一局）；
    * **服务器回放**：传一个文件夹（里面的 `.wrpl` 全是这一局的分片）。

    `progress(msg, frac)` / `expect_server` 含义见 `wrpl_reader.load_replay`。
    """
    data = load_replay(path, progress=progress, expect_server=expect_server)
    calib, _src = resolve_calib(store, data.level)
    rec, stats = build_record(data, path if os.path.isfile(path) else "", calib=calib)
    store.add_global(rec)
    # 地图名用回放头里的关卡名(`avg_finland`)。不写的话侧栏只能显示
    # “地图 1 / 地图 2”这种自动编号, 回放导入的图根本对不上号。
    if rec.map_name and not store.name_of(rec.map_key):
        try:
            store.rename_map(rec.map_key, rec.map_name)
        except Exception:                       # noqa: BLE001 - 取名失败不影响导入
            log.warning("写入地图名失败: %s", rec.map_key, exc_info=True)
    return rec, stats
