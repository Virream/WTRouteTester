"""终检: 每条线「在最终位置原地不动」持续了多久(残骸尾巴的长度)。

残骸的特征就是**坐标冻结**。正常的车不会在同一米内停几个小时, 机场 AI 防空除外。
所以这个探针只看一件事: 每条线从「最后一次离开末点 10 m 范围」到线尾, 隔了多久。

用法: python tools/wrpl/probe_frozen_tail.py [回放目录]
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.replay_import import import_replay                  # noqa: E402
from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
FREEZE_M = 10.0          # 容许的“抖动”半径(米)
ALERT_SEC = 120.0        # 冻结超过这么久就值得看一眼


class FakeStore:
    def calib_of(self, key):
        return None

    def add_global(self, rec):
        pass


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    rec, st = import_replay(path, FakeStore())
    rect = rec.norm_rect
    spanx = max(rect[1] - rect[0], 1.0)
    spanz = max(rect[3] - rect[2], 1.0)

    print(f"回放 {path}  线路 {rec.count()} 条 ")

    rows = []
    for side in ("A", "B"):
        for lf in rec.lives(side):
            pts = lf.points
            if len(pts) < 50:
                continue
            ex, ey = pts[-1].x, pts[-1].y
            tol_x = FREEZE_M / spanx
            tol_y = FREEZE_M / spanz
            last_move = pts[0].t
            for p in pts:
                if abs(p.x - ex) > tol_x or abs(p.y - ey) > tol_y:
                    last_move = p.t
            freeze = lf.t1 - last_move
            if freeze >= ALERT_SEC:
                rows.append((freeze, side, lf))
    rows.sort(key=lambda r: -r[0])
    print(f"末尾「原地不动 ≥ {ALERT_SEC:.0f} s」的线路: {len(rows)} 条")
    for freeze, side, lf in rows:
        print(f"  {lf.life_id:<26} {len(lf.points):6d}点  "
              f"t {lf.points[0].t:7.1f}~{lf.t1:7.1f}  "
              f"尾段冻结 {freeze:6.1f}s  dead={lf.dead}  "
              f"{(lf.player or '')[:16]} · {(lf.vehicle_cls or '')[:22]}")


if __name__ == "__main__":
    main()
