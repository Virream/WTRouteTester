"""复现「路线播放器无法播放」: 直接用真实数据跑 RoutePlayer。

用法: python tools\\wrpl\\probe_playback.py [routes.json]
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication                       # noqa: E402

from wt81111g.route_playback import RoutePlayer                 # noqa: E402
from wt81111g.route_store import RouteStore                     # noqa: E402

DEFAULT = os.path.join(ROOT, "dist", "WTRouteTester", "data", "routes.json")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    app = QApplication.instance() or QApplication([])           # noqa: F841
    store = RouteStore(path)
    print(f"routes.json: {path}")
    print(f"单条路线 {len(store.routes)} 条, 全局条目 {len(store.globals)} 个")

    globs = [g for g in store.globals if g.count()]
    if not globs:
        print("没有可分组的全局条目")
        return
    g = globs[0]
    print(f"\n用条目 {g.label}  map_key={g.map_key}  is_replay={g.is_replay}")
    for side in ("A", "B"):
        lives = [lf for lf in g.lives(side) if len(lf.points) >= 2]
        if not lives:
            continue
        print(f"  {side} 队 {len(g.lives(side))} 条, 其中可播 {len(lives)} 条")
        p = RoutePlayer()
        p.load(lives[:5])
        print(f"    span={p.span}  end={p.end:.1f}  records={len(p.records)}")
        p.start()
        print(f"    start() 后 playing={p.playing}  t={p.time:.2f}")
        # 手动跑几帧, 看时刻有没有前进
        for _ in range(5):
            p._on_tick()
        print(f"    5 帧后 t={p.time:.2f} playing={p.playing}")
        p.pause()


if __name__ == "__main__":
    main()
