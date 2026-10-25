"""按玩家名列出导入结果里的全部线路(查"残骸/多出来的一条线")。

用法: python tools\\wrpl\\probe_player_lives.py <回放路径> <玩家名片段>
"""
from __future__ import annotations

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
    who = sys.argv[2] if len(sys.argv) > 2 else ""
    rec, st = import_replay(path, FakeStore())
    print(f"线路 {rec.count()} 条 (A {len(rec.lives('A'))} / B {len(rec.lives('B'))})")

    total = 0
    for side in ("A", "B"):
        for i, lf in enumerate(rec.lives(side), 1):
            if who and who not in (lf.player or ""):
                continue
            total += 1
            d = lf.t1 - lf.points[0].t
            moving = max(max(p.x for p in lf.points) - min(p.x for p in lf.points),
                         max(p.y for p in lf.points) - min(p.y for p in lf.points))
            print(f"  {side}{i:<3d} {lf.life_id:22s} {len(lf.points):5d} 点"
                  f"  t {lf.points[0].t:7.1f}~{lf.t1:7.1f}s ({d:6.1f}s)"
                  f"  跨度 {moving:.4f}  dead={lf.dead} death_t={lf.death_t:6.1f}"
                  f"  {lf.icon[:30]}")
    print(f"\n匹配「{who}」{total} 条")

    # 全局: 找出"几乎不动"的线路(残骸特征)
    print("\n== 几乎不动的线路(跨度 < 0.005 ≈ 10 m), 应为 0 条 ==")
    bad = 0
    for side in ("A", "B"):
        for i, lf in enumerate(rec.lives(side), 1):
            if len(lf.points) < 2:
                continue
            span = max(max(p.x for p in lf.points) - min(p.x for p in lf.points),
                       max(p.y for p in lf.points) - min(p.y for p in lf.points))
            if span < 0.005 and (lf.t1 - lf.points[0].t) > 30:
                bad += 1
                print(f"  {side}{i} {lf.icon[:40]:42s} 跨度 {span:.5f}"
                      f"  {len(lf.points)} 点  t {lf.points[0].t:.1f}~{lf.t1:.1f}"
                      f"  dead={lf.dead}")
    print(f"  共 {bad} 条")


if __name__ == "__main__":
    main()
