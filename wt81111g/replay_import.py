"""把 `.wrpl` 回放导入成路线测试器的「全局条目」。

分工（2026-10-25 定）：**整局复盘读回放**（一次就有两队全部单位、不用 A/B 两次录制、
不用对轴、文件头直接给地图名），**单条路线测量继续读 8111**（25 Hz 实时）。

坐标：回放给的是**米**（x 东 / z 北 / 高度），本模块把它归一化到 0~1 存进
`UnitLife.points` —— 与 8111 录制的那套归一化坐标同构，所以绘制/回放/悬停
全部可以复用，不需要改渲染。

归一化的范围用**所有样本的 1%~99% 分位**（回放里有远处机场 AI 防空，坐标能到
±20 km，直接用 min/max 会把战场压成一小块）。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

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


def _time_from_name(path: str) -> str:
    """从 `#2026.10.24 22.45.13.wrpl` 这类文件名里取录制时刻；取不到用当前时间。"""
    m = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})[ _](\d{2})[.\-:](\d{2})[.\-:](\d{2})",
                  os.path.basename(path))
    if m:
        y, mo, d, h, mi, se = m.groups()
        return f"{y}-{mo}-{d}T{h}:{mi}:{se}"
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def build_record(data: ReplayData, source_path: str = "") -> tuple[GlobalRecord, ImportStats]:
    """`ReplayData` → `GlobalRecord`（全部单位先放 A 队；阵营待接 ECS）。"""
    src = source_path or data.source
    stats = ImportStats(
        level=data.level, battle_type=data.battle_type, server=data.server,
        parts=data.parts, packets=data.packets,
        failed_packets=data.failed_packets, samples=data.sample_count,
        units=len(data.units), players=len(data.players), kills=len(data.kills),
    )

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

    map_name = data.level or "未知地图"
    if data.battle_type:
        map_name = f"{map_name}  ({data.battle_type})"

    rec = GlobalRecord(
        route_id="",                       # 由 store.new_global 之外的地方补; 这里直接给
        map_key=f"wrpl:{data.level or 'unknown'}",
        map_name=map_name,
        created_at=_time_from_name(src) if src else
                   time.strftime("%Y-%m-%dT%H:%M:%S"),
        zone_count=0,
        zone_signature="",
        mode_name=data.battle_type,
        duration=max(0.0, data.t_last - data.t_first),
    )
    rec.route_id = f"wrpl{int(time.time() * 1000) % 10 ** 10:010d}"

    t0 = data.t_first
    # ---- 玩家身份与真实分队 ------------------------------------------------
    # Slot 包给「玩家槽位 → 名字/队伍」，Kill 包给「载具 uid ↔ 玩家槽位」+
    # 「uid → 载具型号」。都不需要 ECS 哈希表（详见 wrpl_events.py 的模块注释）。
    # 覆盖不到的车（从没参与过击杀/被击毁）只能归到 A 队、名字就叫 uidN
    # —— 实测服务器回放能覆盖 73%（69/94）。
    teams = {p.slot: p.team for p in data.players.values()}
    names = {p.slot: p.display for p in data.players.values()}
    vehicle_of: dict[int, str] = {}
    for ev in data.kills:
        if ev.killer_uid != EXT_MASK_UID and ev.vehicle:
            vehicle_of.setdefault(ev.killer_uid, ev.vehicle)

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

    for uid, rows in sorted(data.units.items()):
        segs: list[list[tuple[float, float, float, float]]] = []
        cur: list[tuple[float, float, float, float]] = []
        for item in rows:
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
                    segs.append(cur)
                    cur = []
            cur.append(item)
        if cur:
            segs.append(cur)
        for si, seg in enumerate(segs, 1):
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
            if data.uid_pid.get(uid) is not None:
                stats.mapped += 1
            life = UnitLife(
                life_id=f"{rec.route_id}:{side}{len(rec.sides[side]) + 1}"
                        + (f"_{si}" if len(segs) > 1 else ""),
                icon=_label(uid),
                obj_type="ground_model",
                color="#174DFF" if side == "A" else "#FF4D4D",
                map_key=rec.map_key,
                points=pts,
                side=side,
                dead=False,
            )
            rec.sides[side].append(life)
            stats.lives += 1
    stats.duration = rec.duration
    return rec, stats


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
    rec, stats = build_record(data, path if os.path.isfile(path) else "")
    store.add_global(rec)
    return rec, stats
