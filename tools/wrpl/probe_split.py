"""探针: 为什么一条线被切成两段(针对指定玩家名)。

用户反馈(2026-10-25): 有位玩家名下出现 **4 条 cn_ztz_96b** 线路, 但游戏里除以色列 M51
外一局只能出击 2 次 —— 说明有车被错误地切成了两条。

这里把该玩家名下每个 uid 的:
  * ECS 存活窗口 [born, died]（含来源 ecs / next / end）
  * MPI 采样点的首末时刻、**所有 >2 秒的空洞**（含空洞两侧的坐标距离）
  * 击杀包给出的 victim 时刻
全部打出来, 看切段点到底是哪一个。

用法:
    python tools/wrpl/probe_split.py "回放目录" 做帝江号
"""
from __future__ import annotations

import collections
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402
from wt81111g.replay_import import GAP_SPLIT_SEC, JUMP_MIN_M, JUMP_MAX_MPS  # noqa: E402


def main(path: str, who: str) -> None:
    data = W.load_replay(path, expect_server=None)

    slots = {p.slot: (getattr(p, "display", "") or getattr(p, "name", ""))
             for p in data.players.values()}
    mine = {s for s, n in slots.items() if who in (n or "")}
    print(f"匹配玩家槽位: {mine} -> {[slots[s] for s in mine]}")

    vans: dict[int, list] = collections.defaultdict(list)
    for v in data.vehicles:
        vans[v.uid].append(v)
    for lst in vans.values():
        lst.sort(key=lambda v: v.born_t)

    deaths: dict[int, list[float]] = collections.defaultdict(list)
    for ev in data.kills:
        if ev.victim_uid != W.EXT_MASK_UID and ev.t > 0:
            deaths[ev.victim_uid].append(ev.t)

    for uid in sorted(data.units):
        vs = [v for v in vans.get(uid, ()) if v.slot in mine]
        if not vs:
            continue
        rows = data.units[uid]
        print(f"\n=== uid={uid}  ({len(rows)} 个采样, "
              f"{rows[0][0]:.1f}s .. {rows[-1][0]:.1f}s) ===")
        for v in vs:
            d = f"{v.died_t:7.1f}" if v.died_t is not None else "   未定"
            print(f"  ECS 窗口 born {v.born_t:7.1f} -> {d}  {v.short_cls:<28s} "
                  f"eid=0x{v.eid:x} by={v.died_by}")
        if deaths.get(uid):
            print(f"  击杀包 victim 时刻: "
                  f"{[round(t, 1) for t in sorted(deaths[uid])]}")
        # 空洞
        prev = None
        for t, x, y, z in rows:
            if prev is not None:
                dt = t - prev[0]
                d = math.hypot(x - prev[1], z - prev[2])
                if dt > 2.0:
                    why = "空洞"
                    if dt > GAP_SPLIT_SEC:
                        why += f" > {GAP_SPLIT_SEC}s -> 切段"
                    elif d > JUMP_MIN_M and dt > 0 and d / dt > JUMP_MAX_MPS:
                        why += f" 且 {d:.0f}m/{dt:.1f}s={d / dt * 3.6:.0f}km/h -> 切段"
                    print(f"    空洞 @{prev[0]:7.1f} -> {t:7.1f}  "
                          f"dt={dt:5.1f}s  d={d:7.0f}m  ({d / dt * 3.6:6.0f}km/h)  {why}")
            prev = (t, x, z)
        # 窗口边界附近的采样
        for v in vs:
            if v.died_t is None:
                continue
            near = [r for r in rows if abs(r[0] - v.died_t) <= 3.0]
            if near:
                print(f"    died={v.died_t:.1f} 附近 {len(near)} 个采样: "
                      f"{[round(r[0], 1) for r in near[:10]]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器",
         sys.argv[2] if len(sys.argv) > 2 else "做帝江号")
