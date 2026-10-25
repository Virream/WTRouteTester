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

import os
import re
import time
from dataclasses import dataclass

from .map_calib import MAP_KEY_PREFIX, MapCalib, level_of, load_bundled
from .route_store import GlobalRecord, TrackPoint, UnitLife
from .wrpl_reader import EXT_MASK_UID, ReplayData, load_replay

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

    map_name = data.level or "未知地图"
    if data.battle_type:
        map_name = f"{map_name}  ({data.battle_type})"

    rec = GlobalRecord(
        route_id="",                       # 由 store.new_global 之外的地方补; 这里直接给
        map_key=replay_map_key(data.level),
        map_name=map_name,
        created_at=_time_from_name(src) if src else
                   time.strftime("%Y-%m-%dT%H:%M:%S"),
        zone_count=0,
        zone_signature="",
        mode_name=data.battle_type,
        duration=max(0.0, data.t_last - data.t_first),
    )
    rec.route_id = f"wrpl{int(time.time() * 1000) % 10 ** 10:010d}"
    # 记住归一化用的世界矩形: 以后这图补上/换了校准, 可以**原地重算**坐标
    # (见 `renormalize_map_records`), 不用让用户重新导入回放。
    rec.norm_rect = [minx, maxx, minz, maxz]

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

    for uid, rows in sorted(data.units.items()):
        # ECS 给出了这辆车的**存活窗口** [born, died]。窗口之外的采样点一定属于
        # 别的东西(uid 被复用给微型侦察无人机 / 别的实体), 留着就会让同一玩家在
        # 同一时刻"开着两台车"(2026-10-25 实测: A25_3 从 01:39 一直画到 16:30,
        # 而这位玩家 01:42 已经换车了)。这里先按窗口硬裁, 再走下面的切段逻辑。
        _win = vans.get(uid, ())
        if _win:
            _tol = 2.0
            rows = [r for r in rows
                    if any(v.born_t - _tol <= r[0]
                           <= ((v.died_t + _tol) if v.died_t is not None else 1e9)
                           for v in _win)]
        # ECS 的「销毁时刻」也是切段点：uid 被复用后，前一辆车销毁的那一刻
        # 就是分界（比“看不见了”精确，也比速度跳变检测稳）
        #
        # ⚠ 被 ECS 窗口覆盖的**击杀时刻不再当切段点**：车被击毁后残骸还会被上报
        #   约 10 秒（实测 uid=61 击杀 363.3s、ECS 销毁 373.7s），在击杀时刻切一刀
        #   就把这 10 秒残骸切成了**独立线路**（用户 2026-10-25 反馈：一位玩家名下
        #   出现 4 条 cn_ztz_96b，而游戏里一局只能出击 2 次）。ECS 的 died_t 才是
        #   权威边界；没有 ECS 档案的（AI 载具）仍用击杀时刻切。
        _wins = [(v.born_t, v.died_t) for v in vans.get(uid, ())
                 if v.died_t is not None]

        def _covered(t: float) -> bool:
            return any(a - 1.0 <= t <= b + 1.0 for a, b in _wins)

        dl = [t for t in deaths.get(uid, ()) if not _covered(t)]
        for _v0 in vans.get(uid, ()):
            # "end" = 到对局结束还在, 不是死亡
            if _v0.died_t and _v0.died_by != "end":
                dl.append(_v0.died_t)
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
                if dt > GAP_SPLIT_SEC or (d > JUMP_MIN_M and dt > 0 and d / dt > JUMP_MAX_MPS):
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
        for seg, _dth in segs:
            _ts = seg[0][0]
            _veh = None
            for _cand in vans.get(uid, ()):
                if _cand.born_t - 1.0 <= _ts <= (
                        (_cand.died_t + 1.0) if _cand.died_t is not None else 1e9):
                    _veh = _cand
                    break
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
            dead = bool(death_at)
            if not dead and dl:
                tail = seg[-1][0]
                for d0 in dl:
                    if tail <= d0 <= tail + 5.0:
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
                pts.append(TrackPoint(t=max(0.0, t - t0),
                                      # **不夹到 0~1** —— 夹了会在边框上拉出一条假直线；
                                      # 真超出范围的点就让它画到画布外（自然被裁掉）
                                      x=nx,
                                      y=ny,
                                      speed=speed))
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
        _win = air_vans.get(auid, ())
        if _win:
            # 与地面同样的「越窗裁剪」：窗口外的采样属于别的实体
            _tol = 2.0
            rows = [r for r in rows
                    if any(v.born_t - _tol <= r[0]
                           <= ((v.died_t + _tol) if v.died_t is not None else 1e9)
                           for v in _win)]
        if not rows:
            continue
        # 切段点：长时间没上报 + 每架飞机的销毁时刻（击杀时刻不参与，理由同地面：
        # 残骸还会被上报几秒，切了会多出一条假线路）
        cuts = sorted(v.died_t for v in _win
                      if v.died_t is not None and v.died_by != "end")
        segs: list[list[tuple[float, float, float, float]]] = []
        cur: list[tuple[float, float, float, float]] = []
        di = 0
        for item in rows:
            while di < len(cuts) and cuts[di] <= item[0]:
                if cur:
                    segs.append(cur)
                    cur = []
                di += 1
            if cur and item[0] - cur[-1][0] > GAP_SPLIT_SEC:
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
                if _cand.born_t - 2.0 <= _ts <= (
                        (_cand.died_t + 2.0) if _cand.died_t is not None else 1e9):
                    _veh = _cand
                    break
            seg_veh.append(_veh)
        _segn: dict[int, int] = {}
        for si, seg in enumerate(segs, 1):
            if len(seg) < 3:
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
            pts = [TrackPoint(t=max(0.0, t - t0),
                              x=(x - minx) / spanx,
                              y=(maxz - z) / spanz,
                              speed=0.0)
                   for t, x, _y, z in seg]
            # 速度：飞机按相邻点位移算（不设上限，超音速很正常）
            for a, b in zip(pts, pts[1:]):
                dt = b.t - a.t
                if dt > 1e-6:
                    a.speed = ((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5 / dt * 3.6
            _cls = veh.short_cls if veh is not None else ""
            _who = veh.player if veh is not None else ""
            _dead = bool(veh is not None and veh.died_by in ("ecs", "next"))
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
                death_t=(max(0.0, veh.died_t - t0)
                         if (_dead and veh is not None and veh.died_t is not None)
                         else 0.0),
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
    new_rect = [float(calib.min_x), float(calib.max_x),
                float(calib.min_z), float(calib.max_z)]
    nspanx, nspanz = new_rect[1] - new_rect[0], new_rect[3] - new_rect[2]
    changed = 0
    for rec in getattr(store, "globals", ()):
        if getattr(rec, "map_key", "") != map_key:
            continue
        old = getattr(rec, "norm_rect", None) or []
        if len(old) != 4:
            continue
        if [round(v, 3) for v in old] == [round(v, 3) for v in new_rect]:
            continue
        spanx, spanz = old[1] - old[0], old[3] - old[2]
        if spanx <= 0 or spanz <= 0:
            continue
        for side in ("A", "B"):
            for life in rec.lives(side):
                for pt in life.points:
                    wx = old[0] + pt.x * spanx
                    wz = old[3] - pt.y * spanz
                    pt.x = (wx - new_rect[0]) / nspanx
                    pt.y = (new_rect[3] - wz) / nspanz
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
    return rec, stats
