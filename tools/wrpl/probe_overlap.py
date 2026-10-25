"""不变量检查: 同一位玩家在同一时刻**不可能**开着两台车。

这是"线路挂错人"的最硬判据 —— 只要他把某人的两条线挂到一起, 时间就会重叠。
逐玩家检查「同名线路的时间区间是否互斥」, 并列出违规。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.replay_import import import_replay                  # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


class FakeStore:
    def calib_of(self, key):
        return None

    def add_global(self, rec):
        pass


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    rec, st = import_replay(path, FakeStore())
    print(f"线路 {rec.count()} 条 (A {len(rec.lives('A'))} / B {len(rec.lives('B'))})"
          f"  残骸剔除 {st.ghosts} 点")

    per: dict[str, list] = collections.defaultdict(list)
    for side in ("A", "B"):
        for i, lf in enumerate(rec.lives(side), 1):
            if lf.player:
                per[lf.player].append((lf.points[0].t, lf.t1, f"{side}{i}", lf.vehicle_cls))

    bad = 0
    for who, items in sorted(per.items()):
        items.sort()
        for a, b in zip(items, items[1:]):
            if b[0] < a[1] - 1.0:          # 重叠超过 1 秒
                bad += 1
                print(f"  ⚠ {who}: {a[2]} {a[0]:.1f}~{a[1]:.1f} ({a[3]})"
                      f"  ↔  {b[2]} {b[0]:.1f}~{b[1]:.1f} ({b[3]})")
    print(f"\n玩家 {len(per)} 位, 时间重叠 {bad} 处")

    print("\n线路最多的 8 位玩家:")
    for who, items in sorted(per.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"  {who[:30]:32s} {len(items)} 条: "
              + ", ".join(f"{c[2]}{'' if c[3] else '?'}" for c in items))


if __name__ == "__main__":
    main()
