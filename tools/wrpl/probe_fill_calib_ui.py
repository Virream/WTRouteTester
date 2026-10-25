"""验证「补全信息」按钮: 回放导入的地图(未校准)那一行确实出现按钮, 已校准的不出现。

不真的去连 8111(离屏环境没有游戏), 只看 UI 结构 + 点击后的行为入口。
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton      # noqa: E402

from wt81111g.map_cache import MapCache                     # noqa: E402
from wt81111g.map_calib import MapCalib                     # noqa: E402
from wt81111g.map_manager import COL_CALIB, MapManagerDialog  # noqa: E402
from wt81111g.route_store import RouteStore                 # noqa: E402


def main() -> None:
    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="wtmm_")
    store = RouteStore(os.path.join(tmp, "routes.json"))
    cache = MapCache(os.path.join(tmp, "maps"))

    store.known_maps.append("wrpl_avg_finland")
    store.known_maps.append("wrpl_avg_kursk")
    store.set_calib("wrpl_avg_kursk", MapCalib(min_x=0, max_x=4096,
                                               min_z=0, max_z=4096,
                                               img_w=1024, img_h=1024,
                                               source="test"))
    dlg = MapManagerDialog(store, cache)

    rows = {}
    for r in range(dlg.table.rowCount()):
        it = dlg.table.item(r, 0)
        if it is not None:
            rows[it.data(0x0100)] = r                 # UserRole
    print("行:", sorted(rows))
    assert "wrpl_avg_finland" in rows and "wrpl_avg_kursk" in rows, rows

    r1 = rows["wrpl_avg_finland"]
    w1 = dlg.table.cellWidget(r1, COL_CALIB)
    assert isinstance(w1, QPushButton) and w1.text() == "补全信息", w1
    print("未校准的回放地图 → 该行有「补全信息」按钮 ✓")

    r2 = rows["wrpl_avg_kursk"]
    w2 = dlg.table.cellWidget(r2, COL_CALIB)
    assert w2 is None, "已校准的行不该再出现按钮"
    print("已校准的 → 只显示文本:", dlg.table.item(r2, COL_CALIB).text())

    # 预览列: 无底图时给出提示文案
    prev = dlg.table.item(r1, 0)
    assert prev.text() == "无底图", prev.text()

    # 按钮点击 → 走 _fill_calib(这里把弹窗替换成"立刻取消", 只验证接线)
    called = []
    real = rw_fill = dlg._fill_calib
    dlg._fill_calib = lambda k: called.append(k)
    w1.click()
    dlg._fill_calib = real
    assert called == ["wrpl_avg_finland"], called
    print("按钮接线 ✓ (_fill_calib 收到正确的地图键)")

    dlg2 = MapManagerDialog(store, cache)
    assert dlg2.table.rowCount() == dlg.table.rowCount()
    print("\n全部通过")


if __name__ == "__main__":
    main()
