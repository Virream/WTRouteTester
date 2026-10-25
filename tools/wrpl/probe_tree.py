"""探针: 用真实回放建侧栏, 打印「队 → 玩家 → 载具」的树结构(offscreen)。

不读用户数据: RouteStore 指向临时目录。

用法:
    python tools/wrpl/probe_tree.py "回放文件\\服务器"
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                      # noqa: E402

from wt81111g import wrpl_reader as W                          # noqa: E402
from wt81111g.replay_import import build_record                # noqa: E402
from wt81111g.route_store import RouteStore                    # noqa: E402
from wt81111g.route_window import (                            # noqa: E402
    RouteTesterWindow, _ROLE_KIND as KIND)


def dump(item, depth: int = 0) -> None:
    kind = item.data(0, KIND)
    text = " | ".join(item.text(c) for c in range(3))
    print("  " * depth + f"[{kind}] {text}")
    for i in range(item.childCount()):
        dump(item.child(i), depth + 1)


def main(path: str) -> None:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    data = W.load_replay(path, expect_server=None)
    rec, stats = build_record(data, path if os.path.isfile(path) else "")

    tmp = tempfile.mkdtemp(prefix="wt81111g_tree_")
    store = RouteStore(os.path.join(tmp, "routes.json"))
    store.add_global(rec)

    win = RouteTesterWindow(store=store)
    win._reload_tree()

    root = win.tree.invisibleRootItem()
    print(f"=== 侧栏结构（{root.childCount()} 个顶层）===")
    for i in range(root.childCount()):
        dump(root.child(i))
    print(f"\n存档目录 {tmp}")
    print(f"线路 A {len(rec.sides['A'])} / B {len(rec.sides['B'])} · "
          f"有玩家名 {stats.mapped} · 判死 {stats.deaths}")
    win.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
