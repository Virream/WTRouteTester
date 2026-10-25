"""打印导入结果里某几条线路(A26 之类)的详情, 用来判断"多出来的一段"是什么。

用法: python tools\\wrpl\\probe_a26.py <回放路径> [A26]
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.replay_import import import_replay                  # noqa: E402
from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器", "芬兰单点模式")


class FakeStore:
    def calib_of(self, key):
        return None

    def add_global(self, rec):
        pass


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    want = sys.argv[2] if len(sys.argv) > 2 else "A26"
    rec, st = import_replay(path, FakeStore())
    print(f"level={st.level} 线路 {rec.count()} 条 "
          f"(A {len(rec.lives('A'))} / B {len(rec.lives('B'))})\n")

    # 先看目标基号的全部段
    for side in ("A", "B"):
        lives = rec.lives(side)
        for i, lf in enumerate(lives, 1):
            base = f"{side}{i}"
            if base != want:
                continue
            print(f"{base}: {lf.life_id}")
            print(f"   icon={lf.icon}  player={lf.player}  cls={lf.vehicle_cls}")
            print(f"   {len(lf.points)} 点  t {lf.points[0].t:.1f}~{lf.t1:.1f}s"
                  f"  dead={lf.dead} death_t={lf.death_t:.1f}")
            print(f"   首点 ({lf.points[0].x:.3f}, {lf.points[0].y:.3f})"
                  f"  末点 ({lf.points[-1].x:.3f}, {lf.points[-1].y:.3f})")

    # 再把这个玩家的所有线路列出来
    keys = {lf.player for side in ("A", "B") for lf in rec.lives(side)
            if lf.life_id.startswith(want + ":") or lf.life_id.startswith(want + "_")}
    if keys:
        who = keys.pop()
        print(f"\n玩家「{who}」的全部线路:")
        for side in ("A", "B"):
            for lf in rec.lives(side):
                if lf.player != who:
                    continue
                print(f"  {lf.life_id:14s} {lf.icon[:38]:40s} {len(lf.points):5d} 点"
                      f"  t {lf.points[0].t:7.1f}~{lf.t1:7.1f}s  dead={lf.dead}"
                      f" death_t={lf.death_t:6.1f}")

    # 统计: 有多少条线路的"最近一次死亡"之后还在动(疑似残骸/幽灵)
    print("\n== 所有线段的时长分布(找那些像残骸的超短线) ==")
    short = []
    for side in ("A", "B"):
        for i, lf in enumerate(rec.lives(side), 1):
            if len(lf.points) < 2:
                continue
            dur = lf.t1 - lf.points[0].t
            if dur < 40:
                short.append((dur, side, i, lf))
    short.sort()
    for dur, side, i, lf in short[:25]:
        print(f"  {side}{i}_{lf.life_id.rsplit('_', 1)[-1]:>4s}  {dur:6.1f}s"
              f"  {len(lf.points):4d} 点  {lf.icon[:40]:42s} dead={lf.dead}")


if __name__ == "__main__":
    main()
