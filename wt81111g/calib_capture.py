"""「补全信息」: 等用户进一局 → 自动从 8111 抓地图图片 + 世界外框 → 生成校准。

**为什么一张图只补一次**(2026-10-25 实测结论):
8111 的 `map_min` / `map_max` 是**地图图片覆盖的世界矩形**, 而图片是按关卡名
(`/levels/avg_finland.bin`)给的 —— 与游戏模式无关。同一张图的两种子模式
(`finland_Dom` 三点 / `finland_Conq3` 单点)实测:
地面载具、首波重生点的世界坐标分布**完全重合**(x 368~1748 / 494~1547,
z 340~1764 / 382~1724, 都压在 8111 报的坦克重生点 (1051~1194, 371~422) 上),
说明两种模式用的是同一套世界坐标系。所以校准(矩形 + 图片 + 像素换算)
**一张图做一次就够**; 只有「战区有哪几个」是模式相关的(那部分不写进校准的换算里)。

流程(用户点「补全信息」后):
1. 弹窗让他进对应战局(任意模式);
2. 每 0.8 秒问一次 `/map_info.json`, `valid=true` 且能拿到**真**地图图片
   (不是机库那张 "?" 占位图) 就开始抓;
3. 抓一轮 `/map_obj.json`, 与 `map_info` 一起交给 `map_calib.build_from_live()`;
4. 成功 → 弹窗自己关掉, 图片与校准由调用方装进缓存/存储。
"""
from __future__ import annotations

import hashlib
import logging
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QProgressBar, QVBoxLayout,
)

from .api8111 import WT8111
from .garage import GARAGE_MAP_SHA1
from .map_calib import MapCalib, build_from_live

log = logging.getLogger("calib_capture")

_TICK_MS = 800          # 轮询 /map_info.json 的间隔
_IMG_TIMEOUT = 8.0      # 取整张地图图片的超时(本地 8111, 1~2 MB 足够)
_MAX_WAIT = 30 * 60 * 1000      # 等 30 分钟还没进战局就自己放弃


class CalibCaptureDialog(QDialog):
    """等用户进战局 → 抓 8111 数据 → `self.calib` / `self.image` 有值。

    用 `exec()` 的返回值判断: `Accepted` = 抓到了; 其它 = 用户取消 / 超时。
    """

    def __init__(self, map_key: str, map_name: str = "", parent=None):
        super().__init__(parent)
        self.map_key = map_key
        self.calib: MapCalib | None = None
        self.image: bytes | None = None
        self._api = WT8111()
        self._t0 = time.monotonic()
        self._stamp = time.strftime("%Y%m%d_%H%M%S")
        self._busy = False
        self._saw_battle = False

        self.setWindowTitle("补全地图信息")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(520)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel(f"<b>{map_name or map_key}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(title)

        tip = QLabel(
            "现在请进一局**这张地图**的战局(任意模式都行 —— 同一张图的各个模式\n"
            "共用同一套世界坐标, 补一次就够)。\n\n"
            "程序会一直等你, 检测到进入战局后自动抓取地图图片与世界外框,\n"
            "抓完这个窗口会自己关掉。\n\n"
            "⚠ 不用真的打完一局: 载入完成、能看到地图时就可以退出。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#94a3b8;")
        root.addWidget(tip)

        self.lbl_status = QLabel("等待进入战局…")
        self.lbl_status.setStyleSheet("font-weight:bold;")
        root.addWidget(self.lbl_status)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)          # 不确定进度(一直在动)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    def start(self) -> int:
        """开始等(等价于 `exec()`, 只是先把定时器开起来)。"""
        self._timer.start()
        return self.exec()

    def done(self, result: int) -> None:      # noqa: D102 - 关窗就停轮询
        self._timer.stop()
        super().done(result)

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        if self._busy:                       # 上一轮还没回来(取图片可能慢)
            return
        if time.monotonic() - self._t0 > _MAX_WAIT / 1000.0:
            self.lbl_status.setText("等太久了, 先放弃 —— 可以稍后再点一次「补全信息」")
            self._timer.stop()
            QTimer.singleShot(1200, self.reject)
            return
        self._busy = True
        try:
            self._probe()
        finally:
            self._busy = False

    def _probe(self) -> None:
        info = self._api.map_info()
        if not info.get("valid"):
            self.lbl_status.setText("等待进入战局…（8111 还没给出地图外框）")
            return
        self._saw_battle = True
        self.lbl_status.setText("已在战局里, 正在取地图图片…")

        img = self._api.map_image(timeout=_IMG_TIMEOUT)
        if not img:
            self.lbl_status.setText("战局已检测到, 但 /map.img 还没应答 —— 稍等…")
            return
        if hashlib.sha1(img).hexdigest() == GARAGE_MAP_SHA1:
            self.lbl_status.setText("现在拿到的还是机库占位图, 继续等真地图…")
            return

        try:
            calib = build_from_live(info, img, self._api.map_objects(),
                                    source=f"8111 现场抓取 {self._stamp}")
        except ValueError as exc:
            self.lbl_status.setText(f"数据还不完整: {exc}")
            return
        if not (calib.img_w and calib.img_h):
            self.lbl_status.setText("拿到图片但读不出尺寸 —— 稍等再试…")
            return

        self.calib = calib
        self.image = img
        log.info("补全信息: %s <- %s", self.map_key, calib.describe())
        self.accept()
