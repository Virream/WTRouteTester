"""量化: 用击杀时刻当轨迹终点, 能比 ECS 销毁时刻多砍掉多少「残骸尾巴」。

背景 —— 车被击毁后位置流会继续上报残骸(坐标冻结), 我们现在的终点取 ECS 的
`died_t`(`0x26` 销毁包), 而击杀包里 `VictimUid + 时间戳` 是**击毁那一刻**,
通常比销毁早约 10 秒。这个探针回答三件事:

1. 每台地面载具有没有「窗口内」的击杀事件(即它是不是被打死的);
2. `died_t - kill_t` 的分布(残骸尾巴有多长);
3. 若把终点收紧到 `kill_t`, 会砍掉多少采样点、多少条线的尾巴会变短。

用法: python tools/wrpl/probe_kill_window.py [回放目录]
"""
from __future__ import annotations

import collections
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import EXT_MASK_UID, load_replay   # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def _pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * p))))
    return xs[i]


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)

    kills: dict[int, list[float]] = collections.defaultdict(list)
    for ev in data.kills:
        if ev.victim_uid != EXT_MASK_UID and ev.t > 0:
            kills[ev.victim_uid].append(ev.t)
    for v in kills.values():
        v.sort()

    vans = [v for v in getattr(data, "vehicles", ())
            if v.uid and v.uid != EXT_MASK_UID]
    ground = [v for v in vans
              if v.spawn or v.uid in data.units]        # 有出生组名 / 有采样
    print(f"回放 {path}")
    print(f"地面载具档案 {len(vans)} 台 (有出生组名/有采样 {len(ground)}), "
          f"击杀事件 {sum(len(v) for v in kills.values())} 条 "
          f"(涉及 {len(kills)} 个 uid)")

    # ---- 1. 窗口内有没有击杀 ------------------------------------------------
    with_kill = 0
    no_kill = 0
    no_died = 0
    deltas: list[float] = []
    tail_pts = 0
    tail_pts_kept = 0          # died 之后仍有采样(现方案已裁) — 对照用
    per_van = []
    for v in ground:
        k_in = [t for t in kills.get(v.uid, ())
                if v.born_t - 1.0 <= t <= ((v.died_t + 1.0)
                                           if v.died_t is not None else 1e9)]
        rows = data.units.get(v.uid, ())
        if v.died_t is None:
            no_died += 1
        if k_in:
            with_kill += 1
            k_t = k_in[-1]                       # 取窗口内最后一次
            # 同一台车可能先被打伤再被打死; 只统计相对 ECS 销毁的那一次
            if v.died_t is not None:
                deltas.append(v.died_t - k_t)
                tail = [r for r in rows
                        if k_t < r[0] <= v.died_t + 2.0]
                tail_pts += len(tail)
                per_van.append((v.uid, v.born_t, k_t, v.died_t, len(tail),
                                len(k_in)))
        else:
            no_kill += 1

    print(f"\n== 窗口内有击杀(被打死) {with_kill} 台 / 无击杀 {no_kill} 台"
          f" (其中 died_t=None {no_died} 台) ==")
    if deltas:
        print(f"died_t - kill_t: p10 {_pct(deltas, .10):.1f}s  "
              f"p50 {_pct(deltas, .50):.1f}s  "
              f"p90 {_pct(deltas, .90):.1f}s  max {max(deltas):.1f}s  "
              f"(均值 {statistics.mean(deltas):.1f}s)")
        print(f"kill_t 之后、died_t +2s 之内的残骸采样: {tail_pts} 个点")
    if per_van:
        per_van.sort(key=lambda x: -x[4])
        print("\n尾巴最长的 12 台 (uid, born, kill, died, 尾巴点数, 窗口内击杀数):")
        for it in per_van[:12]:
            print("  uid%-4d %7.1f %7.1f %7.1f  %5d  %d" % it)

    # ---- 2. 无击杀但有采样: 它们怎么结束的 --------------------------------
    print("\n== 无窗口内击杀、但有采样的载具(弃车/坠毁/AI) ==")
    n = 0
    for v in ground:
        k_in = [t for t in kills.get(v.uid, ())
                if v.born_t - 1.0 <= t <= ((v.died_t + 1.0)
                                           if v.died_t is not None else 1e9)]
        if k_in or not data.units.get(v.uid):
            continue
        n += 1
        if n <= 12:
            rows = data.units[v.uid]
            print(f"  uid{v.uid:<4} born {v.born_t:7.1f} "
                  f"died {('%7.1f' % v.died_t) if v.died_t else '   None'} "
                  f"采样 {len(rows):5d} 尾 {rows[-1][0]:7.1f} "
                  f"{v.template or ''} {v.spawn or ''}")
    print(f"  共 {n} 台")


if __name__ == "__main__":
    main()
