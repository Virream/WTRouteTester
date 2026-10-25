"""「地图管理」对话框: 查看/命名/删除本地缓存的地图图片。

8111 **不提供地图名**(实测: `/map_info.json`/`/mission.json`/`/hud_map` 都没有,
官方 8111 页面本身也只用到 `/map.img`、`/map_info.json`、`/map_obj.json`、`/mission.json`),
地图身份只能靠图片哈希。所以这里让用户把哈希对应成看得懂的名字,
名字存进 `RouteStore.map_names`, 左侧侧栏会立刻显示这个名字。

列表列: 预览 / 名称 / 隐藏 / 图片大小 / 轨迹数 / 首次缓存时间

「隐藏」勾上去只是**不在左侧列表里显示**这张地图 —— 轨迹、名称、缓存图片全部保留,
用来看空战之类永远用不到的图(它们也会被当成“新地图”收进来)。

名称改在窗口顶部固定的「地图名称」输入框里 —— 表格内部**完全禁止编辑**。
之前用“双击单元格就地编辑”, 编辑器会落在图片所在的那一格里被缩略图盖住
(只看得到左右各一条竖线), 所以改成结构上不可能出错的方案。

**8111 校准**: 回放导入出来的地图(`wrpl_<关卡名>`)**没有底图也没有比例**
—— 8111 只给正在打的那张图。所以这里多了一个「导入 8111 校准…」:
选 `tools/wrpl/capture_8111.py` 抓出来的文件夹, 就把
① 世界矩形(`map_min`/`map_max`)与 ② 当时的地图图片一起装到选中的地图键上。
装完之后该图回放线路就与底图像素一一对应(实测 finland: 1 像素 = 1 米),
而且不用在游戏里再进一次这张图。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QBrush, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .map_cache import MapCache, MapEntry
from .map_calib import MAP_KEY_PREFIX, load_from_capture, load_image, save_to_capture
from .route_store import RouteStore

log = logging.getLogger("map_manager")

_THUMB = 84          # 预览缩略图边长(px)
_NAME_COL_W = 300    # “名称”列宽度(必须给足, 否则编辑框会压在预览图上)

# 列号
COL_PREVIEW, COL_NAME, COL_HIDDEN, COL_SIZE, COL_COUNT, COL_CALIB, COL_WHEN = range(7)


class MapManagerDialog(QDialog):
    """缓存地图管理(命名 / 预览 / 删除缓存图片)。"""

    def __init__(self, store: RouteStore, cache: MapCache, parent=None):
        super().__init__(parent)
        self.store = store
        self.cache = cache
        self.renamed = 0
        self.calib_changed = 0      # 导入/清除了几次校准(关窗后要重载底图)
        self.setWindowTitle("地图管理")
        self.resize(940, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        tip = QLabel(
            "8111 不提供地图名, 所以这里用「图片内容哈希」区分地图。\n"
            "改名字的步骤: 先在下面的列表里**选中一张地图**, 再在顶部「地图名称」框里输入,\n"
            "按回车(或点「保存名称」)立即生效 —— 表格里的名字只是显示, 不用双击去改。\n"
            "勾上「隐藏」= 这张地图不在左侧列表里显示(只用得到的地图就看得到了): "
            "轨迹、名称、缓存图片都不会被删, 随时取消勾选就能恢复。\n"
            "名称会显示在左侧列表, 也会写进已录制的轨迹记录; 缓存图片在 data/maps 下,"
            "同一张图只存一份; 删除图片不会删除轨迹记录。\n"
            "回放导入的地图(`wrpl_` 开头)没有底图也没有比例 —— 选中它再点"
            "「导入 8111 校准…」, 选 capture_8111.py 抓出来的文件夹即可")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#94a3b8;")
        root.addWidget(tip)

        # ---- 名称输入区(固定位置, 永远和表格里的图片隔着两行, 不可能重叠) ----
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        lbl_name = QLabel("地图名称:")
        lbl_name.setStyleSheet("font-weight:bold;")
        name_row.addWidget(lbl_name)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("先选中下面的一张地图, 然后在这里输入名称, 回车保存")
        self.edit_name.setClearButtonEnabled(True)
        self.edit_name.setEnabled(False)
        self.edit_name.returnPressed.connect(self._apply_name)
        self.edit_name.editingFinished.connect(self._apply_name)   # 失焦也保存
        name_row.addWidget(self.edit_name, 1)
        self.btn_apply = QPushButton("保存名称")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._apply_name)
        name_row.addWidget(self.btn_apply)
        root.addLayout(name_row)

        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ["预览", "名称", "隐藏", "回放校准", "图片", "轨迹", "首次缓存"])
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(_THUMB, _THUMB))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 关键: 表格里一律不许就地编辑 —— 以前开启 DoubleClicked 时, 双击到图片那一格
        # 会在图片格上叠一个输入框(被缩略图盖住, 只剩左右两条竖线)。
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        # 「隐藏」列的勾选框: 点一下立即落盘(与改名一样不需要点保存)
        self.table.itemChanged.connect(self._on_item_changed)
        self._loading = False
        self._cur_key: str | None = None     # 名称输入框当前对应的地图
        # 列宽: 预览固定; 名称列伸缩; 其余按内容。
        head = self.table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(COL_PREVIEW, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(COL_HIDDEN, QHeaderView.ResizeMode.Fixed)
        for col in (COL_CALIB, COL_SIZE, COL_COUNT, COL_WHEN):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(COL_PREVIEW, _THUMB + 16)
        self.table.setColumnWidth(COL_HIDDEN, 60)
        self.setMinimumWidth(_THUMB + 16 + _NAME_COL_W + 380)
        root.addWidget(self.table, 1)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet("color:#94a3b8; font-size:11px;")
        root.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.clicked.connect(self.reload)
        row.addWidget(self.btn_reload)
        self.btn_open = QPushButton("打开缓存文件夹")
        self.btn_open.clicked.connect(self._open_dir)
        row.addWidget(self.btn_open)
        self.btn_remove = QPushButton("删除选中的缓存图片")
        self.btn_remove.clicked.connect(self._remove_current)
        row.addWidget(self.btn_remove)
        self.btn_calib = QPushButton("导入 8111 校准…")
        self.btn_calib.setToolTip(
            "选 capture_8111.py 抓出来的文件夹\n"
            "→ 把地图外框(世界米)与该图图片一起装到选中的地图上\n"
            "→ 回放导入的线路就能与底图像素一一对应")
        self.btn_calib.clicked.connect(self._import_calib)
        row.addWidget(self.btn_calib)
        self.btn_calib_clear = QPushButton("清除校准")
        self.btn_calib_clear.setToolTip("只删校准数据(底图与轨迹都不动)")
        self.btn_calib_clear.clicked.connect(self._clear_calib)
        row.addWidget(self.btn_calib_clear)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.accept)
        row.addWidget(buttons)
        root.addLayout(row)

        self.reload()

    # ------------------------------------------------------------------ 列表
    def reload(self) -> None:
        """重建列表(有底图的按首次缓存时间倒序, 没底图的排后面)。"""
        keep = self._current_key()          # 导入/清除校准后会 reload, 别丢选中
        self._loading = True
        try:
            entries = self.cache.entries()
            self.table.setRowCount(0)
            cached = set()
            for entry in entries:
                self._add_row(entry)
                cached.add(entry.key)
            # 只有轨迹/校准、**没有底图**的地图也要列出来 —— 否则给回放导入的地图
            # (`wrpl_xxx`)根本选不中, 就没法给它导入校准。
            for key in self.store.ordered_map_keys():
                if key and key not in cached:
                    self._add_row(MapEntry(key=key, path="", saved_at=0.0, size=0))
        finally:
            self._loading = False
        self._update_summary()
        row = self._row_of_key(keep) if keep else -1
        if row < 0 and self.table.rowCount():
            row = 0                          # 默认选中第一张, 输入框直接可用
        if row >= 0:
            self.table.setCurrentCell(row, 0)
        self._on_selection_changed()

    def _row_of_key(self, key: str | None) -> int:
        """地图键 → 行号(找不到返回 -1)。"""
        if not key:
            return -1
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_PREVIEW)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == key:
                return r
        return -1

    def _update_summary(self) -> None:
        """底部汇总: 缓存张数 + 占用 + 隐藏了几张。"""
        entries = self.cache.entries()
        missing = [k for k in self.store.ordered_map_keys() if not self.cache.has(k)]
        hidden = sum(1 for e in entries if self.store.is_map_hidden(e.key))
        extra = ""
        if missing:
            extra += (f"    另有 {len(missing)} 张地图没有底图"
                      f"(已列在最后, 选中后可「导入 8111 校准…」装底图+校准)")
        if hidden:
            extra += f"    已隐藏 {hidden} 张(不在左侧列表显示)"
        self.lbl_summary.setText(
            f"共 {len(entries)} 张缓存地图, 合计 "
            f"{self.cache.total_bytes() / 1024.0 / 1024.0:.1f} MB{extra}")

    def _add_row(self, entry: MapEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, _THUMB + 10)

        # 预览(不可编辑: 否则双击图片会把输入框开在图片格里)
        thumb = QTableWidgetItem()
        if not entry.path:                  # 只有轨迹/校准, 还没有底图
            thumb.setText("无底图")
            thumb.setToolTip("这张图还没有底图: 选中它再点「导入 8111 校准…」")
        else:
            pm = QPixmap(entry.path)
            if pm.isNull():
                thumb.setText("无法预览")
            else:
                thumb.setIcon(QIcon(pm.scaled(
                    _THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
        thumb.setData(Qt.ItemDataRole.UserRole, entry.key)
        thumb.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, COL_PREVIEW, thumb)

        name = QTableWidgetItem()          # 只显示, 编辑在顶部输入框里做
        name.setData(Qt.ItemDataRole.UserRole, entry.key)
        name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, COL_NAME, name)
        self._update_name_cell(entry.key)

        # 隐藏: 勾上就不在左侧列表显示(空战地图之类永远用不到的图)
        hide = QTableWidgetItem()
        hide.setData(Qt.ItemDataRole.UserRole, entry.key)
        hide.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        hide.setCheckState(Qt.CheckState.Checked if self.store.is_map_hidden(entry.key)
                           else Qt.CheckState.Unchecked)
        hide.setToolTip("勾上 = 不在左侧列表里显示这张地图\n"
                        "轨迹/名称/缓存图片都不会删, 取消勾选就回来")
        self.table.setItem(row, COL_HIDDEN, hide)

        size = QTableWidgetItem(f"{entry.size_text}  {_image_size_text(entry.path)}"
                                if entry.path else "—")
        size.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, COL_SIZE, size)

        count = QTableWidgetItem(str(sum(1 for r in self.store.routes
                                         if r.map_key == entry.key)))
        count.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, COL_COUNT, count)

        calib = QTableWidgetItem(self._calib_text(entry.key))
        calib.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        calib.setToolTip(self._calib_tip(entry.key))
        calib.setForeground(QBrush(Qt.GlobalColor.gray)
                            if self.store.calib_of(entry.key) is None else QBrush())
        self.table.setItem(row, COL_CALIB, calib)

        when = QTableWidgetItem(entry.saved_at_text if entry.saved_at else "—")
        when.setFlags(Qt.ItemFlag.ItemIsEnabled)
        when.setToolTip(f"地图键: {entry.key}")
        self.table.setItem(row, COL_WHEN, when)

    def _calib_text(self, key: str) -> str:
        """「校准」列的显示文本。"""
        calib = self.store.calib_of(key)
        if calib is None:
            return "未校准" if key.startswith(MAP_KEY_PREFIX) else "—"
        return f"✓ {calib.span_x:.0f}×{calib.span_z:.0f} m"

    def _calib_tip(self, key: str) -> str:
        calib = self.store.calib_of(key)
        if calib is None:
            return ("这张图没有 8111 校准 —— 回放导入的线路只能按本局观测范围\n"
                    "估算比例, 位置不会与底图对齐。\n"
                    "选中这一行, 再点「导入 8111 校准…」即可。")
        tip = (f"已校准: {calib.describe()}\n"
               f"来源: {calib.source or '?'}"
               + (f" ({calib.captured_at})" if calib.captured_at else ""))
        if calib.zones:
            tip += "\n战区: " + ", ".join(
                f"{z.get('label')}({z.get('x')}, {z.get('z')})" for z in calib.zones)
        return tip

    def _update_name_cell(self, key: str) -> None:
        """把「名称」列刷新成当前真实值(没名字显示灰色“未命名”, 隐藏的加个标记)。"""
        for r in range(self.table.rowCount()):
            it = self.table.item(r, COL_NAME)
            if it is None or it.data(Qt.ItemDataRole.UserRole) != key:
                continue
            name = self.store.name_of(key)
            hidden = self.store.is_map_hidden(key)
            it.setText((name or "未命名") + ("  (已隐藏)" if hidden else ""))
            it.setForeground(QBrush(Qt.GlobalColor.gray)
                             if hidden or not name else QBrush())
            default = self.store.display_name(key)
            tip = f"地图键: {key}\n留空则按顺序自动编号为「{default}」"
            if hidden:
                tip += "\n(已勾选「隐藏」: 不在左侧列表显示)"
            it.setToolTip(tip)
            return

    # ------------------------------------------------------------------ 操作
    def _on_selection_changed(self) -> None:
        """选中另一张地图 → 把上一张刚输入的内容落盘, 再把新选中项的现有名称填进输入框。"""
        if self._loading:
            return
        self._apply_name()                  # 先保存上一张(还没回车就换行的情况)
        key = self._current_key()
        self._cur_key = key
        if key is None:
            self.edit_name.clear()
            self.edit_name.setEnabled(False)
            self.btn_apply.setEnabled(False)
            return
        self.edit_name.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.edit_name.setText(self.store.name_of(key))
        self.edit_name.setToolTip(f"地图键: {key}")
        self.edit_name.selectAll()

    def _apply_name(self) -> None:
        """把顶部输入框的内容写盘(立即生效, 不关窗口, 可接着改下一张)。

        以前是“双击单元格就地编辑 + 点保存才写入”: 编辑器没提交时名字会静默丢掉,
        现在输入框独立在表格外, 回车 / 失焦 / 切行 都会保存。
        """
        key = self._cur_key
        if not key:
            return
        new = self.edit_name.text().strip()
        if new == self.store.name_of(key):
            return
        self.store.rename_map(key, new)
        self.renamed += 1
        self._update_name_cell(key)
        shown = new or self.store.display_name(key)
        self.lbl_summary.setText(
            f"已保存: {shown}(第 {self.renamed} 次修改, 左侧列表已同步)")
        log.info("地图管理: 重命名 %s -> %r", key, new)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """「隐藏」列被点了一下 → 立即落盘(不关窗口, 左侧列表随后刷新)。"""
        if self._loading or item.column() != COL_HIDDEN:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        hidden = item.checkState() == Qt.CheckState.Checked
        if hidden == self.store.is_map_hidden(key):
            return
        self.store.set_map_hidden(key, hidden)
        self._update_name_cell(key)
        self._update_summary()
        log.info("地图管理: %s %r", "隐藏" if hidden else "取消隐藏", key)

    def _current_key(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, COL_PREVIEW)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _import_calib(self) -> None:
        """把 `capture_8111.py` 抓出来的文件夹装到选中的地图上(校准 + 底图)。"""
        self._apply_name()
        key = self._current_key()
        if key is None:
            QMessageBox.information(self, "导入 8111 校准", "请先选中一行(要导入到哪张地图)")
            return
        dir_path = QFileDialog.getExistingDirectory(
            self, "选择 capture_8111.py 抓取的文件夹(里面有 map_info.json)")
        if not dir_path:
            return
        try:
            calib = load_from_capture(dir_path)
        except (ValueError, OSError, KeyError) as exc:
            QMessageBox.warning(self, "导入 8111 校准", f"这个文件夹用不了:\n\n{exc}")
            return

        # 底图一起装上: 回放导入的地图本来没有图片, 没图就什么都画不出来
        img_note = "（文件夹里没有地图图片, 只装了校准）"
        img = load_image(dir_path)
        if img:
            if self.cache.put(key, img):
                img_note = f"底图已装入 {calib.img_w}×{calib.img_h}"
            else:
                img_note = "底图已存在且不更清晰, 保留原图"

        self.store.set_calib(key, calib)
        self.calib_changed += 1
        # 旧记录(按以前的估算范围归一的)跟着重算一次 —— 不用让用户重新导入回放
        renum = 0
        try:
            from .replay_import import renormalize_map_records   # 懒导入: 它带 zstandard
            renum = renormalize_map_records(self.store, key, calib)
        except Exception as exc:                        # noqa: BLE001 - 不影响导入校准
            log.warning("重算回放记录坐标失败: %s", exc)
        # 顺手在抓取目录里留一份 calib.json —— 以后要分发预置校准时直接拿这个文件
        try:
            save_to_capture(dir_path, calib)
        except OSError as exc:
            log.warning("写 calib.json 失败 %s: %s", dir_path, exc)

        shown = self.store.display_name(key)
        self.reload()
        extra = f"\n已按新校准重算了 {renum} 条回放记录的位置。" if renum else ""
        QMessageBox.information(
            self, "导入 8111 校准",
            f"已把校准装到「{shown}」\n\n"
            f"{calib.describe()}\n{img_note}{extra}\n\n"
            f"以后这张图的所有回放线路都会与底图像素一一对应"
            f"（1 像素 = {calib.span_x / max(1, calib.img_w):.2f} 米）。")
        log.info("地图管理: 导入校准 %s <- %s (%s)", key, dir_path, calib.describe())

    def _clear_calib(self) -> None:
        """删掉选中地图的校准(底图与轨迹都不动)。"""
        self._apply_name()
        key = self._current_key()
        if key is None:
            QMessageBox.information(self, "清除校准", "请先选中一行")
            return
        if self.store.calib_of(key) is None:
            QMessageBox.information(self, "清除校准",
                                    f"「{self.store.display_name(key)}」本来就没有校准")
            return
        ok = QMessageBox.question(
            self, "清除校准",
            f"清除「{self.store.display_name(key)}」的 8111 校准吗?\n\n"
            f"底图图片、轨迹记录都不会动, 只是回放导入时不再按它对齐\n"
            f"(改回按本局观测范围估算)。")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if self.store.clear_calib(key):
            self.calib_changed += 1
            self.reload()

    def _open_dir(self) -> None:
        path = self.cache.dir
        try:
            if sys.platform == "win32":
                os.startfile(path)          # noqa: S606 - 只在 Windows 用
            else:
                subprocess.Popen(["xdg-open", path])   # noqa: S603,S607
        except OSError as exc:
            log.warning("打开目录失败 %s: %s", path, exc)

    def _remove_current(self) -> None:
        self._apply_name()
        key = self._current_key()
        if key is None:
            QMessageBox.information(self, "删除缓存图片", "请先选中一行")
            return
        if not self.cache.has(key):
            QMessageBox.information(
                self, "删除缓存图片",
                f"「{self.store.display_name(key)}」现在没有底图可删。\n"
                f"(校准数据若要清理, 用旁边的「清除校准」)")
            return
        used = sum(1 for r in self.store.routes if r.map_key == key)
        ok = QMessageBox.question(
            self, "删除缓存图片",
            f"删除这张缓存图片吗?\n\n地图键: {key}\n名称: {self.store.display_name(key)}\n"
            f"该地图已有 {used} 条轨迹记录(记录与名称都会保留, 只是以后没有底图可显示)")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if self.cache.remove(key):
            self.reload()

    def done(self, result: int) -> None:
        """关窗(含 Esc / 关闭按钮)前把输入框里还没回车的名字保存下来。"""
        self._apply_name()
        super().done(result)


def _image_size_text(path: str) -> str:
    """尽力给出图片像素尺寸(取不到就返回空串)。"""
    pm = QPixmap(path)
    if pm.isNull():
        return ""
    return f"{pm.width()}×{pm.height()}"
