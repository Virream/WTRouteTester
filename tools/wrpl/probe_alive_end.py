"""列出「对局结束时仍然存活」的线路, 与玩家报的战果对照。

判据: `dead == False` 且线尾落在全场最后一段上报时间窗内(位置流停止上报前的
最后若干秒)。另外报出它末尾 60 s 的位移跨度 —— 用户口径是"结束前都在移动"。

用法: python tools/wrpl/probe_alive_end.py [回放目录]
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.replay_import import import_replay                  # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
TAIL_SEC = 60.0          # 末尾取样窗口(秒)


class FakeStore:
    def calib_of(self, key):
        return None

    def add_global(self, rec):
        pass


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    rec, st = import_replay(path, FakeStore())
    rect = rec.norm_rect
    sx = max(rect[1] - rect[0], 1.0)
    sz = max(rect[3] - rect[2], 1.0)

    allpts = [p.t for side in ("A", "B") for lf in rec.lives(side)
              for p in (lf.points[-1],)]
    t_last = max(allpts) if allpts else 0.0
    print(f"回放 {path}")
    print(f"线路 {rec.count()} 条, 最后一条线的末点 {t_last:.1f}s")

    alive = []
    for side in ("A", "B"):
        for lf in rec.lives(side):
            if lf.dead:
                continue
            if lf.t1 < t_last - 30.0:
                continue
            tail = [p for p in lf.points if p.t >= lf.t1 - TAIL_SEC]
            mv = 0.0
            if tail:
                mv = max((max(p.x for p in tail) - min(p.x for p in tail)) * sx,
                         (max(p.y for p in tail) - min(p.y for p in tail)) * sz)
            alive.append((side, lf, mv))
    alive.sort(key=lambda r: (r[0], r[1].points[0].t))
    print(f"\n对局结束时仍存活(dead=False 且线尾接近末尾): {len(alive)} 条")
    for side, lf, mv in alive:
        who = lf.player or "(AI)"
        print(f"  {lf.life_id:<26} {len(lf.points):6d}点  "
              f"t {lf.points[0].t:7.1f}~{lf.t1:7.1f}  "
              f"末尾 {TAIL_SEC:.0f}s 位移 {mv:8.0f} m   "
              f"{who[:18]:<18} · {lf.vehicle_cls or ''}")


if __name__ == "__main__":
    main()
