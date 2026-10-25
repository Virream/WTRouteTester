"""找出「采样点超出了该 uid 所有 ECS 存活窗口」的 uid —— 这些点就是残骸/幽灵。

对这样的 uid 逐段打出来, 看它们会不会被切成"多出来的第二条线路"。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    who = sys.argv[2] if len(sys.argv) > 2 else ""
    data = load_replay(path)
    by_uid: dict[int, list] = {}
    for v in data.vehicles:
        by_uid.setdefault(v.uid, []).append(v)
    name_of = {p.slot: p.display for p in data.players.values()}

    for uid, rows in sorted(data.units.items()):
        ves = by_uid.get(uid, [])
        if not ves or not rows:
            continue
        owners = " ".join(f"{v.player or '-'}[{v.born_t:.0f}~"
                          f"{('%.0f' % v.died_t) if v.died_t is not None else 'end'}]"
                          for v in ves)
        if who and who not in owners:
            continue
        last_end = max((v.died_t if v.died_t is not None else 1e9) for v in ves)
        if rows[-1][0] <= last_end + 2.0:
            continue                       # 采样没超出窗口 → 正常
        # 超出窗口的点
        tail = [r for r in rows if r[0] > last_end + 2.0]
        print(f"\nuid {uid}  采样 t {rows[0][0]:.1f}~{rows[-1][0]:.1f} ({len(rows)} 点)"
              f"   ECS 窗口最晚结束 {last_end:.1f}")
        print(f"   档案: {owners}")
        print(f"   窗口外 {len(tail)} 点: t {tail[0][0]:.1f}~{tail[-1][0]:.1f}"
              f"  位置 ({tail[0][1]:.0f}, {tail[0][3]:.0f})"
              f" → ({tail[-1][1]:.0f}, {tail[-1][3]:.0f})")
        kills = [ev.t for ev in data.kills if ev.victim_uid == uid]
        if kills:
            print(f"   击杀时刻: {[round(k, 1) for k in kills]}")
        print(f"   该 uid 对应玩家: {name_of.get(data.uid_pid.get(uid), '-')}")


if __name__ == "__main__":
    main()
