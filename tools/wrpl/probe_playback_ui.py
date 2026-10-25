"""复现「路线播放器无法播放」: 在离屏窗口里真的点一次「▶ 回放」。

用 dist 里那份真实 routes.json(回放导入的条目), 选中侧栏里的线路 → 点播放按钮,
然后把异常、`player.playing`、`player.span` 和播放到 1 秒后的状态都打出来。

用法: python tools\\wrpl\\probe_playback_ui.py [routes.json]
"""
from __future__ import annotations

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                       # noqa: E402

from wt81111g.route_store import RouteStore                     # noqa: E402
from wt81111g.route_window import RouteTesterWindow             # noqa: E402

DEFAULT = os.path.join(ROOT, "dist", "WTRouteTester", "data", "routes.json")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    app = QApplication.instance() or QApplication([])           # noqa: F841
    store = RouteStore(path)
    win = RouteTesterWindow(store)
    win.show()
    app.processEvents()

    print(f"routes.json: {path}")
    print(f"单条路线 {len(store.routes)}, 全局条目 {len(store.globals)}")
    print("known_maps:", store.known_maps)
    print("map_names :", dict(list(store.map_names.items())[:5]))
    print(f"侧栏一级条目 {win.tree.topLevelItemCount()} 个")
    for i in range(win.tree.topLevelItemCount()):
        it = win.tree.topLevelItem(i)
        print(f"  [{i}] kind={it.data(0, 256)!r} id={it.data(0, 257)!r} "
              f"text={it.text(0)!r} children={it.childCount()}")
    kinds = {}
    for it in win._iter_all_items():
        kinds[it.data(0, 256)] = kinds.get(it.data(0, 256), 0) + 1
    print("侧栏全部条目:", kinds)

    # 1) 不选任何东西直接点播放(用户最常见操作)
    print("\n== 1) 不选中任何条目, 直接点「▶ 回放」==")
    print(f"   按钮 enabled = {win.btn_play.isEnabled()}")
    try:
        win._toggle_playback()
    except Exception:
        traceback.print_exc()
    print(f"   after toggle: playing={win.player.playing} "
          f"records={len(win.player.records)} span={win.player.span}")
    win._end_playback()

    # 2) 选中全局条目本身(用户在侧栏里最常这么点)再播
    print("\n== 2) 在侧栏选中全局条目, 再点播放 ==")
    g = next((x for x in store.globals if x.count()), None)
    if g is None:
        print("   没有全局条目")
        return
    target = None
    for it in win._iter_all_items():
        if (it.data(0, 256) == "global"
                and it.data(0, 257) == g.route_id):
            target = it
            break
    print(f"   条目 {g.label}: 树里找到 global 行 = {target is not None}")
    if target is not None:
        target.setSelected(True)
    app.processEvents()
    print(f"   _selection_playables() -> {len(win._selection_playables())}")
    print(f"   _playable_tracks()     -> {len(win._playable_tracks())}")
    print(f"   _visible_lives         -> {len(win._visible_lives)}")
    try:
        win._toggle_playback()
    except Exception:
        traceback.print_exc()
    print(f"   after toggle: playing={win.player.playing} "
          f"records={len(win.player.records)} span={win.player.span}")
    # 真推 0.35 秒, 看 playhead 有没有前进 + 每帧的 set_playback 是否抛异常
    t_before = win.player.time
    for _ in range(12):
        time.sleep(0.03)
        win.player._on_tick()
        app.processEvents()
    print(f"   推进后 t {t_before:.2f} -> {win.player.time:.2f}  "
          f"playing={win.player.playing}  lbl={win.lbl_play.text()}")
    print(f"   map_view.playback_records = "
          f"{len(win.map_view.playback_records or [])}")
    win._end_playback()


if __name__ == "__main__":
    main()
