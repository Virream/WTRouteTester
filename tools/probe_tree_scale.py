# -*- coding: utf-8 -*-
"""侧栏规模实测: 500 局 × 217 条线时, 启动(重建侧栏)要多久。

只测“建树”, 所以每局只放 5 个点 —— 树的开销取决于**线路条数**, 与点数无关。
用法: python tools/probe_tree_scale.py [局数]
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from PyQt6.QtWidgets import QApplication                     # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

import route_tester_test as T                                # noqa: E402
from wt81111g.route_store import RouteStore, TrackPoint, UnitLife   # noqa: E402


def build(tmp: str, n_sessions: int, lives_per_side: int) -> str:
    path = os.path.join(tmp, "tree.json")
    store = RouteStore(path)
    pts = [TrackPoint(0.1 + i * 0.001, 0.2 + i * 0.001, i * 0.04, 30.0) for i in range(5)]
    for i in range(n_sessions):
        g = store.new_global(f"MAP{i % 10}", f"地图 {i % 10}", zone_count=3)
        for side in ("A", "B"):
            for k in range(lives_per_side):
                g.sides[side].append(UnitLife(
                    life_id=f"{g.route_id}:{side}{k}", icon="T",
                    obj_type="ground_model", side=side,
                    player=f"玩家{k % 16}", vehicle_cls=f"cls{k % 9}", points=pts))
        store.globals.append(g)
        store._dirty_ids.add(g.route_id)
    store.save_now()
    return path


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    tmp = tempfile.mkdtemp(prefix="probe_tree_")
    try:
        path = build(tmp, n, 108)          # 108 + 108 + AI ≈ 217 条/局
        t0 = time.perf_counter()
        store = RouteStore(path)
        t_store = time.perf_counter() - t0
        n_lives = sum(g.count() for g in store.globals)
        print(f"{n} 局 / {n_lives:,} 条线路")
        print(f"  存储层启动(只扫头部) {t_store * 1000:.0f} ms")

        win = T.make_window(tmp, "tree.json")
        win.recorder.stop()
        win.hotkeys.stop()
        win.show()
        app.processEvents()
        t0 = time.perf_counter()
        win._reload_tree()
        app.processEvents()
        dt = time.perf_counter() - t0
        print(f"  重建侧栏 {dt * 1000:.0f} ms")
        # 展开一局(建它那 217 条线的行)
        top = win.tree.topLevelItem(0)
        mid = top.child(0) if top is not None and top.childCount() else None
        gitem = None
        if mid is not None:
            for i in range(mid.childCount()):
                if mid.child(i).data(0, win._ROLE_KIND if hasattr(win, "_ROLE_KIND") else 256) == "global":
                    gitem = mid.child(i)
                    break
        if gitem is None and mid is not None:
            gitem = mid.child(0) if mid.childCount() else None
        t0 = time.perf_counter()
        if gitem is not None:
            win._populate_global_item(gitem)
        dt2 = time.perf_counter() - t0
        rows = 0
        if gitem is not None:
            def walk(it):
                nonlocal rows
                rows += 1
                for i in range(it.childCount()):
                    walk(it.child(i))
            walk(gitem)
        print(f"  展开一局(建 {rows} 行) {dt2 * 1000:.1f} ms")
        total_items = 0

        def count_items(it):
            nonlocal total_items
            total_items += 1
            for i in range(it.childCount()):
                count_items(it.child(i))
        root = win.tree.invisibleRootItem()
        for i in range(root.childCount()):
            count_items(root.child(i))
        print(f"  当前树里共 {total_items:,} 行")
        win.close()
        ok = dt < 1.0
        print(f"{'OK  ' if ok else 'FAIL'}  500 局重建侧栏 < 1 s  ({dt * 1000:.0f} ms)")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
