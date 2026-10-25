"""诊断: 用 8111 校准后, 有多少条线路真的落在底图(2048×2048 m)范围内。

关键疑问(2026-10-25): 观测到的世界坐标跨度(x -2448~8637)远大于底图覆盖的
2048 m —— 那到底是真的“战场比图大”, 还是少数远处 AI 拉大了范围?
按行统计**完全在图内 / 部分在图内 / 完全在图外** 的线路数, 才能判断
校准以后用户还能不能看见这些线。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.map_calib import load_from_capture                  # noqa: E402
from wt81111g.replay_import import import_replay                  # noqa: E402

CAP = os.path.join(ROOT, "回放文件", "8111抓取", "20261025_201545")
SERVER = os.path.join(ROOT, "回放文件", "服务器", "芬兰单点模式")


class FakeStore:
    def __init__(self, calib=None):
        self._calib = calib

    def calib_of(self, map_key):
        return self._calib

    def add_global(self, rec):
        pass


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    calib = load_from_capture(CAP)
    rec, _st = import_replay(SERVER, FakeStore(calib))

    for kind in ("ground_model", "air_model"):
        lives = [lf for lf in rec.lives("A") + rec.lives("B")
                 if lf.obj_type == kind and lf.points]
        print(f"\n===== {kind}: {len(lives)} 条 =====")
        if not lives:
            continue
        inside = part = outside = 0
        worst = []
        for lf in lives:
            xs = [p.x for p in lf.points]
            ys = [p.y for p in lf.points]
            allin = all(0 <= v <= 1 for v in xs) and all(0 <= v <= 1 for v in ys)
            allout = (max(xs) < 0 or min(xs) > 1 or max(ys) < 0 or min(ys) > 1)
            if allin:
                inside += 1
            elif allout:
                outside += 1
                worst.append(lf)
            else:
                part += 1
        print(f"  完全在图内 {inside}  部分 {part}  完全在图外 {outside}")
        if worst:
            for lf in worst[:8]:
                xs = [p.x for p in lf.points]
                ys = [p.y for p in lf.points]
                print(f"    {lf.icon[:40]:42s} x {min(xs):6.2f}~{max(xs):6.2f}"
                      f"  y {min(ys):6.2f}~{max(ys):6.2f}"
                      f"  ({len(lf.points)} 点, {lf.t1 - lf.points[0].t:.0f}s)")

    # 世界坐标分布(全部地面点, 通过归一化反算)
    rect = rec.norm_rect
    spanx, spanz = rect[1] - rect[0], rect[3] - rect[2]
    gx, gz = [], []
    for lf in rec.lives("A") + rec.lives("B"):
        if lf.obj_type != "ground_model":
            continue
        for p in lf.points:
            gx.append(rect[0] + p.x * spanx)
            gz.append(rect[3] - p.y * spanz)
    print("\n===== 地面样本世界坐标分位 =====")
    for p in (0.0, 0.001, 0.01, 0.5, 0.99, 0.999, 1.0):
        print(f"  p{p * 100:6.1f}%  x {pct(gx, p):9.1f}   z {pct(gz, p):9.1f}")


if __name__ == "__main__":
    main()
