"""快查「空 data 启动 → 后来有条目」时「▶ 回放」按钮能不能自己变亮。

不需要解析回放(不用等 30 秒): 直接往 store 里塞一个最小的 GlobalRecord,
再走一次 `_reload_tree()` —— 导入流程结束也是调它。

用法: python tools\\wrpl\\probe_play_button.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication                       # noqa: E402

from wt81111g.route_store import (GlobalRecord, RouteStore,     # noqa: E402
                                  TrackPoint, UnitLife)
from wt81111g.route_window import RouteTesterWindow             # noqa: E402


def main() -> None:
    app = QApplication.instance() or QApplication([])           # noqa: F841
    tmp = tempfile.mkdtemp(prefix="wtpb_")
    store = RouteStore(os.path.join(tmp, "routes.json"))       # 空
    win = RouteTesterWindow(store)
    win.show()
    app.processEvents()

    print("=== 1) 空 data 启动 ===")
    print(f"   全局条目 {len(store.globals)}  «播放» enabled = "
          f"{win.btn_play.isEnabled()}   (预期 False)")

    # 造一个最小全局条目(和「从录像导入」产出的结构一致: 一个 GlobalRecord
    # + 两条 UnitLife), 然后像导入结束那样重建侧栏
    key = "wrpl_avg_finland"
    store.known_maps.append(key)
    g = GlobalRecord(route_id="fake-replay-1", map_key=key,
                     map_name="avg_finland", created_at="2026-10-26 00:24",
                     duration=120.0, mode_name="战区控制")
    g.sides["A"] = [UnitLife(
        life_id="fake-replay-1:A1", icon="测试玩家 · ussr_t_80u",
        player="测试玩家", vehicle_cls="ussr_t_80u", side="A",
        map_key=key, dead=True, death_t=100.0,
        points=[TrackPoint(x=0.1 + i * 0.01, y=0.5, t=i * 10.0, speed=30.0)
                for i in range(11)])]
    g.sides["B"] = [UnitLife(
        life_id="fake-replay-1:B1", icon="敌人 · ussr_t_72b3",
        player="敌人", vehicle_cls="ussr_t_72b3", side="B",
        map_key=key,
        points=[TrackPoint(x=0.8 - i * 0.01, y=0.4, t=i * 10.0, speed=28.0)
                for i in range(11)])]
    store.globals.append(g)
    win._reload_tree()                       # 导入结束后走的就是这一句
    app.processEvents()

    print("\n=== 2) 塞进 1 个全局条目 + 重建侧栏 ===")
    print(f"   全局条目 {len(store.globals)}  «播放» enabled = "
          f"{win.btn_play.isEnabled()}   (预期 True)")

    print("\n=== 3) 侧栏层级 ===")

    def dump(item, depth=0) -> None:
        print("   " + "  " * depth
              + f"[{item.data(0, 256)}] {item.text(1)!r} {item.text(2)!r}")
        for i in range(item.childCount()):
            dump(item.child(i), depth + 1)

    for i in range(win.tree.topLevelItemCount()):
        dump(win.tree.topLevelItem(i))

    print("\n=== 4) 选中全局条目 -> 点播放 ===")
    for it in win._iter_all_items():
        if it.data(0, 256) == "global":
            it.setSelected(True)
            break
    app.processEvents()
    print(f"   _playable_tracks() -> {len(win._playable_tracks())}")
    if win.btn_play.isEnabled():
        win._toggle_playback()
        print(f"   playing={win.player.playing} records={len(win.player.records)}")


if __name__ == "__main__":
    main()
