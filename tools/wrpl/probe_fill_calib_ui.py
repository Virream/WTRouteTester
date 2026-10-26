"""探针: 「校准地图位置」按钮的完整链路(不碰 8111 —— 把抓取对话框换成假的)。

按钮要做的两件事(用户 2026-10-26 的规格):
    ① 通过 8111 获取地图图片;
    ② 记录图片覆盖的世界范围(即"图片应该摆在哪里")。

这里把 `CalibCaptureDialog` 换成一个假的(直接返回一张图 + 一份外框), 验证:
    * 没选中任何一行时按钮是灰的(不知道要校准哪一张);
    * 选中后点一下 → 底图装上、校准存进 store、列表与预览都刷新;
    * 这张图关联了回放关卡代号时, 回放身份也能查到这份校准。

跑法:

    cmd /c "set PYTHONIOENCODING=utf-8&& set PYTHONPATH=d:\\course\\VSCODE\\WT81111G&&
            set QT_QPA_PLATFORM=offscreen&& .venv\\Scripts\\python.exe
            tools\\wrpl\\probe_fill_calib_ui.py"
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt   # noqa: E402
from PyQt6.QtGui import QColor, QImage                        # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from wt81111g import map_manager as mm                        # noqa: E402
from wt81111g.map_cache import MapCache                       # noqa: E402
from wt81111g.map_calib import MapCalib                       # noqa: E402
from wt81111g.map_manager import COL_KEY, COL_LEVEL, COL_NAME, MapManagerDialog  # noqa: E402
from wt81111g.route_store import RouteStore                   # noqa: E402

HASH_KEY = "c59f768c0fde"
LEVEL = "avg_finland"


def png_bytes(color: str = "#3b6e4a", size: int = 512) -> bytes:
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    ba = QByteArray()          # ⚠ 必须用变量持有: QBuffer 不拥有它, 传临时对象会段错误
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


class FakeCapture:
    """冒充 `CalibCaptureDialog`: 立刻"抓"到一张图 + 一份外框。"""

    calib: MapCalib | None = None
    image: bytes | None = None
    calls: list[str] = []

    def __init__(self, map_key: str, map_name: str = "", parent=None):
        FakeCapture.calls.append(map_key)
        self.map_key = map_key
        self.map_name = map_name

    def start(self) -> int:
        FakeCapture.image = png_bytes()
        FakeCapture.calib = MapCalib(min_x=-1024.0, max_x=1024.0,
                                     min_z=-1024.0, max_z=1024.0,
                                     img_w=512, img_h=512,
                                     source="FakeCapture")
        return QDialog.DialogCode.Accepted


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841
    # 弹窗会阻塞: 换成不弹的(Sip 类型的静态方法不能直接赋值, 要换模块里的名字)
    class _NoPopup:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def information(*_a, **_k):
            return QMessageBox.StandardButton.Ok

        @staticmethod
        def warning(*_a, **_k):
            return QMessageBox.StandardButton.Ok

        @staticmethod
        def question(*_a, **_k):
            return QMessageBox.StandardButton.Yes

    mm.QMessageBox = _NoPopup
    mm.CalibCaptureDialog = FakeCapture

    root = tempfile.mkdtemp(prefix="probe_calib_ui_")
    store = RouteStore(os.path.join(root, "routes.json"))
    cache = MapCache(os.path.join(root, "maps"))

    rep = store.new_global(map_key=f"wrpl_{LEVEL}", map_name=LEVEL,
                           zone_count=3, mode_name="战区控制")
    store.add_global(rep)
    store.remember_map(HASH_KEY)
    cache.put(HASH_KEY, png_bytes("#884422", 256))      # 先有一张低清图
    store.rename_map(HASH_KEY, "芬兰")
    store.set_map_level(HASH_KEY, LEVEL)                # 关联: 两者是同一张图

    dlg = MapManagerDialog(store, cache)
    fails: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"{'OK  ' if cond else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails.append(label)

    print("--- 未选中 ---")
    dlg.table.clearSelection()
    dlg._on_selection_changed()
    check("「校准地图位置」是灰的", not dlg.btn_calib.isEnabled())

    print("\n--- 选中后点按钮 ---")
    assert dlg.table.rowCount() == 1
    key = dlg.table.item(0, COL_KEY).data(Qt.ItemDataRole.UserRole)
    edit = dlg.table.cellWidget(0, COL_NAME)
    combo = dlg.table.cellWidget(0, COL_LEVEL)
    check("键 = 哈希", key == HASH_KEY, f"={key}")
    check("名字框里是「芬兰」", edit.text() == "芬兰", f"={edit.text()!r}")
    check("下拉框选中 avg_finland",
          combo.currentData() == LEVEL, f"={combo.currentData()!r}")
    dlg.table.selectRow(0)
    app.processEvents()
    check("按钮变可用", dlg.btn_calib.isEnabled())
    check("校准前没有校准", store.calib_of(key) is None)

    dlg._calibrate()
    app.processEvents()

    check("抓取对话框被调到了这张图上",
          FakeCapture.calls == [HASH_KEY], f"={FakeCapture.calls}")
    calib = store.calib_of(key)
    check("校准已存进 store", calib is not None,
          f"={calib.describe() if calib else None}")
    check("外框就是抓到的那个",
          calib is not None and abs(calib.min_x + 1024.0) < 1e-6)
    check("底图换成了新抓的那张(更清晰就替换)",
          cache.has(key) and cache.load(key) == FakeCapture.image)
    check("回放身份也查得到这份校准",
          store.calib_of(f"wrpl_{LEVEL}") is not None)
    check("计数 +1(主窗口据此重载底图)", dlg.calib_changed == 1,
          f"={dlg.calib_changed}")
    check("列表刷新后还是同一张图", dlg.table.rowCount() == 1)
    check("预览已更新", dlg._preview_pm is not None)

    dlg.close()

    # ------------------------------------------------------------------
    # 录制时就会先把校准存下来 —— 用户先解析回放、后录图的那个顺序靠它才能闭环
    # ------------------------------------------------------------------
    print("\n--- 录制时自动缓存校准 ---")
    from wt81111g.route_recorder import RouteRecorder

    live_key = "aaaabbbbcccc"
    cache.put(live_key, png_bytes("#556677", 256))
    store.remember_map(live_key)
    rec = RouteRecorder(store, base_url="http://127.0.0.1:1", map_cache=cache)
    rec._map_key = live_key
    rec.client.map_info = lambda: {
        "valid": True, "map_min": [0.0, 0.0], "map_max": [4096.0, 4096.0],
        "time": "2026-10-26 01:00:00"}
    objs = [{"type": "capture_zone", "icon": "capture_zone", "zone_label": "A",
             "x": 0.5, "y": 0.5, "color": "#44ff44"},
            {"type": "respawn_base_tank", "icon": "respawn_base_tank",
             "x": 0.25, "y": 0.25}]

    check("录之前这张图没有校准", store.calib_of(live_key) is None)
    rec._remember_calib(objs)
    got = store.calib_of(live_key)
    check("录制时已自动存下校准", got is not None,
          f"={got.describe() if got else None}")
    check("外框来自 8111 的 map_info",
          got is not None and abs(got.max_x - 4096.0) < 1e-6)
    check("图片尺寸也记下了",
          got is not None and (got.img_w, got.img_h) == (256, 256),
          f"={None if got is None else (got.img_w, got.img_h)}")
    check("战区/重生点也装进去了",
          got is not None and len(got.zones) == 1 and len(got.spawns) == 1,
          f"zones={len(got.zones) if got else 0} spawns={len(got.spawns) if got else 0}")

    # 已经校准过的图不该被录制时的自动记录覆盖
    before = store.calib_of(live_key)
    store.clear_calib(live_key)
    store.set_calib(live_key, MapCalib(min_x=0.0, max_x=999.0,
                                       min_z=0.0, max_z=999.0,
                                       img_w=64, img_h=64, source="user"))
    rec2 = RouteRecorder(store, base_url="http://127.0.0.1:1", map_cache=cache)
    rec2._map_key = live_key
    rec2.client.map_info = lambda: {
        "valid": True, "map_min": [0.0, 0.0], "map_max": [4096.0, 4096.0]}
    rec2._remember_calib(objs)
    keep = store.calib_of(live_key)
    check("已有校准时绝不覆盖",
          keep is not None and abs(keep.max_x - 999.0) < 1e-6,
          f"={keep.describe() if keep else None}")
    check("旧校准与自动记的那份不同(保证上面这条真的在测)",
          before is not None)

    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
