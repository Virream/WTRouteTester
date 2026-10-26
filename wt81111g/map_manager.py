"""「地图管理」对话框: 给底图命名, 并把它和回放里的地图代号关联起来。

**为什么需要这张表**(用户 2026-10-26 定下的机制): 同一张地图会以**两种身份**进来 ——

* 8111 实况 / 单条录制 → **图片哈希**(如 `c59f768c0fde`), 有底图、有校准;
* 回放文件解析         → **关卡代号**(如 `avg_finland`), 有轨迹、**但没有底图**。

8111 **不提供地图名**, 所以软件自己判不出"这两个其实是同一张图"。于是一张图会在
左侧列表里变成两行(`avg_finland` 和 `地图 3`), 各自的线路挂在各自下面。
用户在这张表里指认之后, 两者就归成同一张图: 回放的线路挂到那张图下,
用那张图的底图与校准, 显示名也统一成用户起的名字。

**可逆**: 关联只是一张映射表(`RouteStore.map_levels`), 任何记录的 `map_key` 都
不搬家。所以选错了直接改回来(或改回「未选择」)即可, 不会丢任何东西 ——
这就是为什么第三列是个可以随时改的下拉框, 而不是"一次性合并"按钮。

界面: 左边一条条键值对(键=哈希 / 值=名字输入框 / 可选=关卡代号), 右边是选中那条
的底图预览, 底部一个「校准地图位置」按钮(未选中时不可用)。
"""
from __future__ import annotations

import logging
from functools import partial

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .calib_capture import CalibCaptureDialog
from .map_cache import MapCache
from .map_calib import MAP_KEY_PREFIX
from .route_store import PLACEHOLDER_MAP_KEY, RouteStore

log = logging.getLogger("map_manager")

# 列号: 键(哈希) / 名字(输入框) / 关卡代号(下拉框)
COL_KEY, COL_NAME, COL_LEVEL = range(3)

PREVIEW_MIN = (380, 380)


class MapManagerDialog(QDialog):
    """地图身份表: 命名 + 关联回放关卡代号 + 底图预览 + 校准。"""

    def __init__(self, store: RouteStore, cache: MapCache, parent=None):
        super().__init__(parent)
        self.store = store
        self.cache = cache
        self.renamed = 0           # 名称/关联改了几处(关窗后主窗口据此提示)
        self.calib_changed = 0     # 校准做了几次(关窗后要重载底图)
        self._loading = False
        self._cur_key: str | None = None
        self._preview_pm: QPixmap | None = None    # 原图(缩放时才用)

        self.setWindowTitle("地图管理")
        self.resize(1120, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(10)

        # ---------------------------------------------------------- 左: 键值对
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["键(地图哈希)", "名字", "可选: 回放关卡代号"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        head = self.table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(COL_KEY, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(COL_LEVEL, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(COL_KEY, 200)
        self.table.setColumnWidth(COL_LEVEL, 190)
        body.addWidget(self.table, 3)

        # ---------------------------------------------------------- 右: 图片预览
        right = QVBoxLayout()
        right.setSpacing(6)
        self.lbl_preview_title = QLabel("")
        self.lbl_preview_title.setStyleSheet("color:#94a3b8; font-size:11px;")
        right.addWidget(self.lbl_preview_title)
        self.preview = QLabel("未选择地图")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(QSize(*PREVIEW_MIN))
        self.preview.setStyleSheet(
            "background:#0b1220; border:1px solid #1e293b; color:#64748b;")
        right.addWidget(self.preview, 1)
        body.addLayout(right, 4)

        root.addLayout(body, 1)

        # ---------------------------------------------------------- 底: 校准 + 关闭
        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_calib = QPushButton("校准地图位置")
        self.btn_calib.setEnabled(False)
        self.btn_calib.setToolTip(
            "先在左边的列表里**选中一条**, 这个按钮才知道要校准哪张图。\n\n"
            "点它之后: ① 通过 8111 抓这张图的地图图片; ② 记录图片覆盖的世界范围\n"
            "(即「图片应该摆在哪里」)。抓完会自动装好, 回放线路随即对齐底图。")
        self.btn_calib.clicked.connect(self._calibrate)
        row.addWidget(self.btn_calib)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet("color:#94a3b8; font-size:11px;")
        self.lbl_summary.setWordWrap(True)
        row.addWidget(self.lbl_summary, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.accept)
        row.addWidget(buttons)
        root.addLayout(row)

        self.reload()

    # ------------------------------------------------------------------ 列表
    def _row_keys(self) -> list[str]:
        """列表要显示哪些地图 —— **只显示图片哈希身份的**, 按首次见到顺序。

        为什么不列 `wrpl_<关卡代号>`: 那是回放侧的身份, 它要么已经关联到某张图
        (那样就该归到那张图下, 不该再占一行), 要么还没关联 —— 而"还没关联"时
        它既没有底图也没有校准, 摆在这里也做不了什么。它的数据在侧栏里照常显示,
        要靠"在游戏里进这张图"才会变成哈希身份出现在这里。
        """
        out: list[str] = []
        cached = {e.key for e in self.cache.entries()}
        for key in self.store.ordered_map_keys():
            if not key or key == PLACEHOLDER_MAP_KEY or key == "unknown":
                continue
            if key.startswith(MAP_KEY_PREFIX):
                continue                      # 回放身份(`wrpl_xxx`)不占一行
            if key in cached or self.store.name_of(key) or self.store.calib_of(key):
                out.append(key)
        # 缓存里有、但 store 还不认识的图(理论上不会有)也算上
        for key in cached:
            if key and key not in out and key != PLACEHOLDER_MAP_KEY:
                out.append(key)
        return out

    def reload(self) -> None:
        """重建列表(保留原来的选中行)。"""
        keep = self._cur_key
        self._loading = True
        try:
            self.table.setRowCount(0)
            for key in self._row_keys():
                self._add_row(key)
        finally:
            self._loading = False
        row = self._row_of_key(keep) if keep else -1
        if row < 0 and self.table.rowCount():
            row = 0
        if row >= 0:
            self.table.selectRow(row)
        self._on_selection_changed()
        self._update_summary()

    def _row_of_key(self, key: str | None) -> int:
        if not key:
            return -1
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_KEY)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == key:
                return r
        return -1

    def _add_row(self, key: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, 34)

        # ---- 键: 自动编号 + 图片哈希(auto 部分不随用户命名变化) ----
        item = QTableWidgetItem(f"{self.store.auto_name(key)}_{key}")
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setToolTip(f"地图哈希(图片内容): {key}\n"
                        f"底图: {'已缓存' if self.cache.has(key) else '没有'}\n"
                        f"校准: {self._calib_text(key)}")
        self.table.setItem(row, COL_KEY, item)

        # ---- 值: 名字(直接给输入框) ----
        edit = QLineEdit(self.store.name_of(key))
        edit.setPlaceholderText("未命名")
        edit.setClearButtonEnabled(True)
        edit.editingFinished.connect(partial(self._apply_name, key, edit))
        self.table.setCellWidget(row, COL_NAME, edit)

        # ---- 可选: 回放关卡代号(下拉框, 随时可改 = 可逆) ----
        combo = QComboBox()
        model = QStandardItemModel(combo)
        cur = self.store.level_of_hash(key)
        for label, data, enabled, tip in self._level_rows(key):
            it = QStandardItem(label)
            it.setData(data, Qt.ItemDataRole.UserRole)
            it.setEnabled(enabled)
            if tip:
                it.setToolTip(tip)
            model.appendRow(it)
        combo.setModel(model)
        combo.setCurrentIndex(max(0, combo.findData(cur)))
        combo.setToolTip(
            "这张图对应回放文件里的哪个关卡代号?\n"
            "选中之后, 那个关卡的线路就会挂到这张图下面, 用这张图的底图与校准。\n"
            "一个关卡代号只能给一张图用 —— 已经被别的地图选走的会变灰, 点不了。\n"
            "选错了直接改 —— 数据不会丢。")
        combo.currentIndexChanged.connect(partial(self._apply_level, key, combo))
        self.table.setCellWidget(row, COL_LEVEL, combo)

    def _level_rows(self, key: str) -> list[tuple[str, str, bool, str]]:
        """这一行下拉框该有哪些项 —— `[(显示文字, 存的值, 能不能选, 提示)]`。

        一个关卡代号只能归一张图: 已经被**别的**地图选走的仍然列出来, 但**变灰点不
        了**并注明被谁用了。不直接藏掉的原因(用户 2026-10-26): 突然少一项会让人
        以为软件坏了 / 自己看错了。
        本行自己当前选着的那个当然可用(否则就没法改成别的了)。
        """
        cur = self.store.level_of_hash(key)
        taken: dict[str, str] = {}
        for hk, lv in self.store.map_levels.items():
            if hk != key and lv:
                taken.setdefault(lv, hk)
        rows: list[tuple[str, str, bool, str]] = [("未选择", "", True, "")]
        for lv in self.store.level_choices():
            if lv in taken:
                owner = self.store.display_name(taken[lv])
                rows.append((lv, lv, False,
                             f"已经被「{owner}」用了 —— 一个关卡代号只能归一张图"))
            else:
                rows.append((lv, lv, True, ""))
        # 极端情况: 自己选着的那个已经不在库里了(记录被删?) —— 也要能显示出来
        if cur and all(data != cur for _label, data, _ok, _t in rows):
            rows.append((cur, cur, True, "这个关卡代号已经没有对应的回放记录了"))
        return rows

    def _refresh_level_choices(self) -> None:
        """关联变了 → 把**其它行**的下拉列表里的可用性重算一遍(被占走的变灰)。

        只改 `enabled` / tooltip, 不重建 model: 不抢焦点、不丢当前选中、
        也不会把底部的提示文字盖掉。
        """
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_KEY)
            combo = self.table.cellWidget(r, COL_LEVEL)
            if it is None or not isinstance(combo, QComboBox):
                continue
            key = it.data(Qt.ItemDataRole.UserRole)
            if not key:
                continue
            model = combo.model()
            if not isinstance(model, QStandardItemModel):
                continue
            blocked = combo.blockSignals(True)
            try:
                for _label, data, enabled, tip in self._level_rows(key):
                    idx = combo.findData(data)
                    row = model.item(idx) if idx >= 0 else None
                    if row is None:
                        continue
                    if row.isEnabled() != enabled:
                        row.setEnabled(enabled)
                    if row.toolTip() != tip:
                        row.setToolTip(tip)
            finally:
                combo.blockSignals(blocked)

    def _update_summary(self) -> None:
        n = self.table.rowCount()
        linked = sum(1 for k in self._row_keys() if self.store.level_of_hash(k))
        extra = f", 已关联关卡代号 {linked} 张" if linked else ""
        self.lbl_summary.setText(f"共 {n} 张地图{extra}")

    def _calib_text(self, key: str) -> str:
        calib = self.store.calib_of(key)
        if calib is None:
            return "未校准"
        return f"已校准 {calib.span_x:.0f}×{calib.span_z:.0f} m"

    # ------------------------------------------------------------------ 选中/预览
    def _on_selection_changed(self) -> None:
        if self._loading:
            return
        key = None
        model = self.table.selectionModel()
        rows = model.selectedRows() if model is not None else []
        if rows:
            it = self.table.item(rows[0].row(), COL_KEY)
            key = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        self._cur_key = key
        self.btn_calib.setEnabled(bool(key))
        self._update_preview()

    def _update_preview(self) -> None:
        """把右边换成当前选中那条的底图。"""
        key = self._cur_key
        if not key:
            self._preview_pm = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText("未选择地图")
            self.lbl_preview_title.setText("")
            return
        shown = self.store.display_name(key)
        level = self.store.level_of_hash(key)
        title = f"{shown} · {key}"
        if level:
            title += f" · 对应回放关卡 {level}"
        self.lbl_preview_title.setText(title)

        data = self.cache.load(key)
        pm = QPixmap()
        if data:
            pm.loadFromData(data)
        if pm.isNull():
            self._preview_pm = None
            self.preview.setPixmap(QPixmap())
            self.preview.setText("这张图还没有底图\n\n点下面的「校准地图位置」,\n"
                                 "进一局这张图就会自动抓取")
            return
        self._preview_pm = pm
        self._scale_preview()

    def _scale_preview(self) -> None:
        pm = self._preview_pm
        if pm is None:
            return
        w = max(64, self.preview.width() - 6)
        h = max(64, self.preview.height() - 6)
        self.preview.setPixmap(pm.scaled(
            w, h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):       # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._scale_preview()

    # ------------------------------------------------------------------ 编辑
    def _apply_name(self, key: str, edit: QLineEdit) -> None:
        """名字输入框失焦/回车 → 立即落盘(不关窗口, 可以接着改下一张)。"""
        if self._loading:
            return
        new = edit.text().strip()
        if new == self.store.name_of(key):
            return
        self.store.rename_map(key, new)
        self.renamed += 1
        shown = self.store.display_name(key)
        self.lbl_summary.setText(
            f"已保存: {shown}(第 {self.renamed} 处改动, 侧栏已同步)")
        self._update_preview()
        log.info("地图管理: 重命名 %s -> %r", key, new)

    def _apply_level(self, key: str, combo: QComboBox, _index: int = 0) -> None:
        """「可选」下拉框变了 → 关联/解除关联, 立即落盘。

        这是整个机制里**唯一**会改变地图归属的动作, 而且它只改 `map_levels` 这张
        映射表 —— 记录的 `map_key` 一个都不动, 所以改错能改回来。

        关联之后还要做一件事: 那个关卡**已经导入过的**线路是按当初的估算范围归一化
        的, 现在能拿到这张图的校准了, 得把它们重算一遍 —— 否则用户会发现"关联了,
        但线路还是歪的"(用户 2026-10-26 描述的场景正是要它们对齐)。
        """
        if self._loading:
            return
        level = combo.currentData() or ""
        if level == self.store.level_of_hash(key):
            return
        old = self.store.level_of_hash(key)
        if not self.store.set_map_level(key, level):
            return
        self.renamed += 1
        shown = self.store.display_name(key)
        renum = 0
        if level:
            calib = self.store.calib_of(key)
            if calib is not None:
                try:
                    from .replay_import import renormalize_map_records   # 懒导入
                    renum = renormalize_map_records(self.store, key, calib)
                    self.calib_changed += 1        # 线路位置变了 → 主窗口要重画
                except Exception as exc:                            # noqa: BLE001
                    log.warning("按新校准重算回放记录失败: %s", exc)
        if level:
            tail = f", 已按这张图的校准重算了 {renum} 条线路" if renum else ""
            self.lbl_summary.setText(
                f"已关联: {shown} ← 回放关卡 {level}{tail}")
        else:
            self.lbl_summary.setText(
                f"已解除关联: {shown} 不再对应回放关卡 {old}")
        log.info("地图管理: %s 的关卡代号 %r -> %r (重算 %d 条)",
                 key, old, level, renum)
        # 其它行的下拉列表跟着变: 被本行拿走的代号不该再出现在别处
        self._refresh_level_choices()
        self._update_preview()
        self._update_summary()

    # ------------------------------------------------------------------ 校准
    def _calibrate(self) -> None:
        """「校准地图位置」: ① 8111 抓图 ② 记录图片覆盖的世界范围。"""
        key = self._cur_key
        if not key:
            QMessageBox.information(self, "校准地图位置",
                                    "请先在左边的列表里选中一张地图")
            return
        shown = self.store.display_name(key)
        level = self.store.level_of_hash(key)
        dlg = CalibCaptureDialog(key, f"{shown}（{level or key}）", self)
        if dlg.start() != QDialog.DialogCode.Accepted or dlg.calib is None:
            self.lbl_summary.setText(
                f"「{shown}」这次没有校准 —— 想校的时候再点一次「校准地图位置」即可")
            return

        calib = dlg.calib
        img_note = "底图已装入"
        if dlg.image and not self.cache.put(key, dlg.image):
            img_note = "底图已存在且不更清晰, 保留原图"
        elif not dlg.image:
            img_note = "没有抓到图片, 只装了位置校准"
        self.store.set_calib(key, calib)
        self.calib_changed += 1
        # 已经导入的回放记录跟着重算一次 —— 不用让用户重新导入回放
        renum = 0
        try:
            from .replay_import import renormalize_map_records   # 懒导入: 带 zstandard
            renum = renormalize_map_records(self.store, key, calib)
        except Exception as exc:                        # noqa: BLE001
            log.warning("重算回放记录坐标失败: %s", exc)

        self.reload()
        extra = f"\n已按新校准重算了 {renum} 条回放记录的位置。" if renum else ""
        QMessageBox.information(
            self, "校准地图位置",
            f"「{shown}」校准完成\n\n"
            f"{calib.describe()}\n{img_note}{extra}\n\n"
            f"以后这张图的所有线路都会与底图像素一一对应"
            f"（1 像素 = {calib.span_x / max(1, calib.img_w):.2f} 米）。")
        log.info("地图管理: 校准 %s (%s)", key, calib.describe())

    # ------------------------------------------------------------------ 收尾
    def done(self, result: int) -> None:
        """关窗(含 Esc / 关闭按钮)前, 把还没失焦的输入框内容保存下来。"""
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_KEY)
            edit = self.table.cellWidget(r, COL_NAME)
            if it is None or not isinstance(edit, QLineEdit):
                continue
            key = it.data(Qt.ItemDataRole.UserRole)
            if key:
                self._apply_name(key, edit)
        super().done(result)
