"""跑图线路测试器 —— 主窗口。

布局(左 / 右 / 下 三部分):
    ┌────────┬──────────────────────────┐
    │        │                          │
    │ 侧栏   │        地图显示区         │
    │ (全高) │     (右上角向下向左延伸)  │
    │  两级  ├──────────────────────────┤
    │  树形  │       信息显示区          │
    └────────┴──────────────────────────┘

- 侧栏: 一级=地图名(可展开), 二级=线路记录(右键删除、双击回放);
- 地图: 8111 地图图片 + 玩家位置 + 轨迹(按速度着色) + 标记圈与时间;
- 信息: 顶部为轨迹回放(与下方录制功能用分割线隔开),
        下方为载具型号 / 速度 / 计时器 + 三个录制按钮(支持全局快捷键)。
        换图(地图图片指纹变化)时自动清除不属于当前地图的轨迹。
"""
from __future__ import annotations

import logging
import os
import sys
import time

from PyQt6.QtCore import QSettings, QSize, Qt, QTimer
from PyQt6.QtGui import (
    QAction, QActionGroup, QBrush, QCloseEvent, QColor, QFont, QIcon, QPalette,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHeaderView, QHBoxLayout, QInputDialog, QLabel,
    QListView, QMainWindow, QMenu, QMessageBox, QProgressDialog, QPushButton,
    QSizePolicy, QSplitter, QTextBrowser, QTreeView, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from .global_hotkey import (
    ACTION_LABELS, DEFAULT_BINDINGS, GlobalHotkey, GlobalHotkeyWatcher, describe_binding,
    load_bindings, reset_bindings, save_bindings,
)
from .gfx_info import PREF_AUTO, PREF_SOFTWARE, AdapterOption, GpuNotice, enumerate_adapters
from .help_text import about_html
from .hotkey_dialog import HotkeyDialog
from .icons import ICON_SIZE, circle_icon, eye_icon, pin_icon
from .map_cache import MapCache
from .map_manager import MapManagerDialog
from .route_map_view import RouteMapView
from .route_playback import PLAYBACK_SPEEDS, RoutePlayer
from .record_overlay import (
    ANCHORS, COLOR_TEXT, COLOR_TITLE, RecordOverlay, RecordOverlaySettings,
)
from .record_overlay_dialog import RecordOverlayDialog
# 回放(.wrpl)导入: 放顶层 —— 打包时缺 zstandard 之类会在**启动**就暴露,
# 而不是等用户点「从录像中获取路线」才报错(打包版没控制台, 那种错极难查)
from .replay_import import import_replay
from .route_recorder import (DEFAULT_GLOBAL_INTERVAL_MS, DEFAULT_HUD_INTERVAL_MS,
                             DEFAULT_INTERVAL_MS, RouteRecorder)
from .route_store import (
    GLOBAL_SIDES, GLOBAL_SIDE_LABELS, PLACEHOLDER_MAP_KEY, GlobalRecord, RouteRecord,
    RouteStore, fmt_speed, fmt_time, fmt_time_ms,
)
from .sound import play as play_sound
from .sound import play_error as play_error_sound
from .sound import settings as sound_settings
from .sound_dialog import SoundSettingsDialog
from .tree_view import RouteTreeWidget
from .zones import ground_spawns, layout_spread, ownership_text, spawns_from_json
from .zones import spawns_to_json, zones_to_json
from .zones import CAPTURE_ANCHORS, SIDE_ENEMY, SIDE_NEUTRAL, SIDE_OURS
from .zones import remap_capture_color

log = logging.getLogger("route_window")


def _log_file_path() -> str:
    """主日志文件的路径(`logging.basicConfig(filename=...)` 里设的那个)。

    并行导入的子进程是新解释器: spawn 带不过去任何 handler, 得自己配一次
    —— 打包版是 `--windowed`, 子进程里的告警没地方去就只能静悄悄地丢掉。
    """
    for h in logging.getLogger().handlers:
        name = getattr(h, "baseFilename", "")
        if name:
            return str(name)
    return ""


def _create_map_view(parent, *, prefer: str = PREF_AUTO,
                     force_software: bool = False) -> RouteMapView:
    """建地图控件: 优先 GPU(pygfx), 不行就软件渲染。

    这里**必须惰性导入** `route_map_gl`: 它一 import 就会加载 `wgpu-native` 的 DLL,
    而无头/offscreen 平台下这会和 Qt 冲突(实测整个进程 0xC000041D 崩掉, 连测试都跑不了),
    而且普通用户也省掉几百毫秒启动时间。`create_map_view` 内部还会判平台与异常回退。

    返回值上挂着 `gpu_notice`(`GpuNotice | None`)与 `gpu_adapter`(用的哪块卡),
    主窗口据此决定要不要弹一次窗 —— **以前是静默回退软件渲染**,
    用户只会觉得"为什么这么卡", 完全不知道原因。
    """
    try:
        from .route_map_gl import create_map_view
    except Exception as exc:  # noqa: BLE001
        log.exception("GPU 画布模块导入失败, 使用软件渲染")
        view = RouteMapView(parent)
        view.gpu_notice = GpuNotice(
            kind="missing", title="GPU 加速组件缺失",
            body="程序里负责显卡加速的组件没能加载(pygfx / wgpu)。\n\n"
                 "地图已改用软件绘制 —— 能用, 但线条多的时候会慢。\n\n"
                 "如果是自己打包的版本, 请检查打包是否漏了 wgpu 的 DLL;\n"
                 "如果是官方发布版, 请把这个日志发给作者。",
            detail=f"{type(exc).__name__}: {exc}")
        view.gpu_adapter = "软件渲染(QPainter)"
        return view
    return create_map_view(parent, prefer=prefer, force_software=force_software)


def _gpu_module_loaded() -> bool:
    """本进程里 GPU 画布模块**已经成功导入**了吗?

    菜单里要枚举显卡, 而枚举就得 `import wgpu` —— 在无头/离屏平台上那会让整个进程
    崩掉(见 `_create_map_view`)。所以只在"已经加载过"的前提下才枚举:
    加载过就说明平台没问题, 再 import 一次是空操作。
    """
    return f"{__package__}.route_map_gl" in sys.modules


# 采样间隔选项(毫秒):
# 40ms(≈25Hz)与 8111 陆战数据更新率匹配; 20ms(≈50Hz)接近 8111 请求上限。
#
# **这不是一个该让用户随时调的东西**(2026-10-23 用户意见): 调得太小会把 8111
# 轮询推到上限、调得太大轨迹会变稀疏出怪结果。所以它从信息区下拉框收进了
# 菜单「设置 → 轮询间隔」, 主界面只留一个很小的只读频率提示。
INTERVAL_CHOICES_MS = (20, 25, 30, 40, 50, 75, 100)
# `/hudmsg` 的轮询选项(毫秒): 1.3.0 改用回放文件后已不再使用。
# （保留常量只是为了让旧设置项能被认出来删掉，见 `__init__` 里的清理）

# 设置版本: **改了某一项的“默认值”就递增**。
# QSettings 会记住用户上次选的值, 不重置的话新默认对老用户永远不生效。
# 2 = 2026-10-24 全局轮询 40→100ms、hudmsg 500→2000ms
SETTINGS_VERSION = 2


def rate_choice_ms(ms, choices=INTERVAL_CHOICES_MS) -> int:
    """把任意毫秒值归一成选项里的一个(取最接近的), 默认取表里的 40。"""
    try:
        value = float(ms)
    except (TypeError, ValueError):
        value = float(DEFAULT_INTERVAL_MS)
    return int(min(choices, key=lambda x: abs(x - value)))

_ROLE_KIND = Qt.ItemDataRole.UserRole
_ROLE_ID = Qt.ItemDataRole.UserRole + 1
_ROLE_SIG = Qt.ItemDataRole.UserRole + 2      # 全局线路行的队别(A/B)
# 分组行的稳定标识(地图/模式): 侧栏重建后用它对上“原来折起来的是哪几个分组”
_ROLE_KEY = Qt.ItemDataRole.UserRole + 3
# 「子项还没建」的标记(= 全局条目的 route_id)。一局就有 200 多条线, 五百局是 10 万行 ——
# 启动时全建出来要一两秒, 所以子项**展开到那一局时才建**。
_ROLE_LAZY = Qt.ItemDataRole.UserRole + 4

# 层级行(地图/模式/版本)只当“分组标题”: 可点击展开、可右键, 但**不可被选中** ——
# 否则多选时会把标题行也选进去(回放/基准只应以轨迹条目为单位)。
GROUP_ITEM_FLAGS = Qt.ItemFlag.ItemIsEnabled

# 侧栏列: 0=展开/收起(眼睛图标) 1=地图/模式/版本/线路 2=信息(条数或用时)
_COL_EYE = 0
_COL_NAME = 1
_COL_INFO = 2          # “信息”列: 条数 / 用时
_COL_TIME = _COL_INFO

# 眼睛列宽度(px): 最深的一条是全局录制里的线路(地图 → 模式 → 版本 → 全局 → 队伍 → 线路,
# 缩进 12×5), 图标 18px, 再加一点间距 —— 取小了图标会被列边界裁掉/压到名称列的文字上
EYE_COLUMN_WIDTH = 6 * 12 + 18 + 8


class RouteTesterWindow(QMainWindow):
    """跑图线路测试器主窗口。"""

    def __init__(self, store: RouteStore | None = None, parent=None,
                 map_cache: MapCache | None = None, start_recorder: bool = True,
                 settings: QSettings | None = None):
        super().__init__(parent)
        self.setWindowTitle("跑图线路测试器 —— 战争雷霆")
        self.resize(1400, 880)

        self.store = store or RouteStore()
        # 地图图片本地缓存: 关掉游戏后仍能用缓存的底图看回放
        # (map_cache / start_recorder / settings 可注入: 测试里指向临时目录/临时配置,
        #  且不启动真实 8111 轮询 —— 否则会**写进用户真实的 QSettings**)
        # 必须在 _reload_tree() 之前赋值 —— 侧栏会用它判断“哪些地图只有缓存图片”
        self.map_cache = map_cache if map_cache is not None else MapCache()
        # 同一张地图可能被 8111 以不同分辨率发过来(1024²/2048²/4096²) —— 老版本会按
        # 字节哈希把它们当成两张地图。启动时把这类重复条目合并掉(见 _merge_duplicate_maps)
        self._merged_maps = self._merge_duplicate_maps()
        self._last_duration = 0.0
        self._last_text_at = 0.0
        self._last_map_try = 0.0      # 上次尝试用缓存底图的时间(节流)
        self._last_record: RouteRecord | None = None
        # 眼睛点亮的轨迹 id: 它们会同时显示在地图上(取代了以前 Shift 多选的做法)
        self._visible_ids: set[str] = set()
        # 全局录制里“眼睛点亮”的线路 id(`<条目 id>:<队><序号>`), 与 _visible_ids 分开存:
        # 两类记录不是一回事(前者能回放, 后者只是折线), 混在一起容易误判。
        self._visible_lives: set[str] = set()
        # 最近一次完成的全局录制条目(用于状态提示/自动展开)
        self._last_global: GlobalRecord | None = None
        # 选中集合(决定回放播哪几条; 用于判断“本次新加进来的是哪张地图”)
        self._sel_ids: set[str] = set()
        self._sel_groups: set[tuple[str, str]] = set()
        # “被父项带进选中范围”的条目(全局条目/A·B 队行被选中时的子行):
        # 它们会被回放, 但 Qt 不会给它们高亮 → 手动刷选中色(见 _refresh_implied_marks)
        self._implied_items: list[QTreeWidgetItem] = []
        # “基准模式”: 双击某条轨迹后, 它的(地图键, 模式名)被锁为基准 ——
        # 之后多选/回放只能在同一地图同一模式内进行
        # (见 _group_of / _playable_group / _on_tree_selection_changed)
        self._anchor: tuple[str, str] | None = None
        self._reselecting = False
        # 侧栏是否已经建过一次: 首次构建(= 刚启动)默认**全部折叠**, 之后的重建
        # 才按用户手动展开/折叠过的状态恢复(见 _restore_folded)
        self._tree_built = False
        # “粘性状态栏文字”: 提示类信息(回放开始/结束、显示切换…)在若干秒内不被刷新覆盖
        self._sticky_text = ""
        self._sticky_until = 0.0
        # 采样/轮询间隔(毫秒)持久化(PyQt 自带的 QSettings)。
        # **三档分开**(2026-10-23 用户要求): 单条录制 / 全局录制 / hudmsg。
        # 入口在菜单「设置 → 轮询间隔」—— 主界面不再放这个下拉框(见上方常量注释)。
        # 可注入: 测试必须用**临时配置**, 否则会改写用户真实设置
        # (以前就踩过: 测试跑一遍, 用户的轮询间隔/显示开关全被改掉)。
        self._settings = settings if settings is not None else QSettings(
            "WTBlackList", "RouteTester")
        # 默认值改过(见 SETTINGS_VERSION) → 把老用户存的值清掉, 让新默认生效
        try:
            _ver = int(self._settings.value("settings_version", 1))
        except (TypeError, ValueError):
            _ver = 1
        if _ver < SETTINGS_VERSION:
            for _key in ("global_interval_ms", "hud_interval_ms"):
                self._settings.remove(_key)
            self._settings.setValue("settings_version", SETTINGS_VERSION)

        def _saved_int(key: str, default: float, choices) -> int:
            try:
                raw = self._settings.value(key, default)
            except TypeError:
                raw = default
            return rate_choice_ms(raw, choices)

        self._saved_ms = _saved_int("interval_ms", DEFAULT_INTERVAL_MS,
                                    INTERVAL_CHOICES_MS)
        # 1.3.0 改用回放文件之后, “全局录制轮询率”与“hudmsg 轮询率”已经没用了
        # (用户 2026-10-26 要求只留单条的那一档) —— 旧的设置项顺手清掉。
        for _stale in ("global_interval_ms", "hud_interval_ms"):
            if self._settings.contains(_stale):
                self._settings.remove(_stale)
        # “以速度从零变化时刻作为起始时间点”是**非默认开启**的功能: 每次启动都不勾选。
        # 不写入 QSettings —— 否则上次勾选的状态会被带到下次启动, 看起来就像默认开启。
        self._saved_on_move = False
        if self._settings.contains("start_on_move"):   # 清掉早期版本留下的值
            self._settings.remove("start_on_move")
        # 回放显示选项: “被击毁后路线变暗” / “被击毁标记” —— **默认都开**, 并记住设置
        try:
            self._saved_dead_dim = int(self._settings.value("dead_dim", 1)) != 0
        except (TypeError, ValueError):
            self._saved_dead_dim = True
        try:
            self._saved_dead_mark = int(self._settings.value("dead_mark", 1)) != 0
        except (TypeError, ValueError):
            self._saved_dead_mark = True
        # 「显示战区」(小菱形 + A/B/C 字母)开关: 菜单「设置」里勾选, 记住设置
        try:
            self._show_zones = int(self._settings.value("show_zones", 1)) != 0
        except (TypeError, ValueError):
            self._show_zones = True
        # 「显示重生位置」(红/蓝空心方块)开关: 同上
        try:
            self._show_spawns = int(self._settings.value("show_spawns", 1)) != 0
        except (TypeError, ValueError):
            self._show_spawns = True
        # 「显示机场跑道」开关: 只有「从回放导入」的条目有数据(8111 录制的没有机场信息)
        try:
            self._show_airfields = int(self._settings.value("show_airfields", 1)) != 0
        except (TypeError, ValueError):
            self._show_airfields = True
        # ---- 图形(显卡)设置(用户 2026-10-27 要求可在设置里指定用哪块卡)
        # 存的格式: "auto" = 自动选择 / "<device>|<backend>" = 指定某块适配器;
        # 「强制软件绘制」单独一个布尔(勾上就不碰 GPU, 连枚举都不做)。
        self._gfx_prefer = str(self._settings.value("gfx_adapter", PREF_AUTO) or PREF_AUTO)
        try:
            self._gfx_software = int(self._settings.value("gfx_software", 0)) != 0
        except (TypeError, ValueError):
            self._gfx_software = False
        # 上面两个是**本次生效**的值(菜单勾选 / 崩溃标记都看它们)。
        # 下面两个是**存进设置、下次启动才生效**的值 —— 两者必须分开:
        # 用户改了设置但还没重启时, "现在是 GPU 在跑"这件事不能跟着变,
        # 否则 `_clear_gfx_pending` 会以为走的是软件渲染、不去抹崩溃标记。
        self._gfx_prefer_saved = self._gfx_prefer
        self._gfx_software_saved = self._gfx_software
        # 上次启动是不是在用 GPU 画布时**崩掉**了。
        # 为什么需要它: wgpu 配置 surface 失败会在 **Rust 层 panic** —— try/except 抓不住,
        # 打包版还是 --windowed(无控制台), 用户看到的就是"闪退、什么都没有"。
        # 所以启动时先留一个"我正在尝试 GPU"的标记, 画布活过几秒再抹掉;
        # 下次启动发现标记还在 = 上次崩了 → 这次直接走软件渲染, 至少能打开。
        try:
            self._gfx_crashed_before = int(self._settings.value("gfx_pending", 0)) != 0
        except (TypeError, ValueError):
            self._gfx_crashed_before = False
        self._gpu_notice: GpuNotice | None = None
        self._gpu_notice_shown = False
        self._gpu_adapters: list[AdapterOption] = []
        self._gpu_adapters_error = ""
        if self._gfx_crashed_before and not self._gfx_software:
            log.warning("上次启动时 GPU 画布没能活下来(可能崩在 wgpu 内部) → 本次强制软件渲染")
            self._gfx_software = True
            # **持久化**: 下次也别再试了。用户修好驱动后要自己取消「强制软件绘制」
            # (提示里就是这么写的), 否则每次启动都要崩一次。
            self._settings.setValue("gfx_software", 1)
            self._settings.setValue("gfx_pending", 0)
            self._settings.sync()
            self._gfx_software_saved = True
            self._gpu_notice = GpuNotice(
                kind="crashed", title="上次用显卡渲染时程序崩了",
                body="这个版本的显卡渲染在你的机器上没能正常启动(程序和驱动之间的问题),\n"
                     "所以本次先用**软件绘制**启动 —— 能正常用, 只是线条多的时候会慢。\n\n"
                     "可以试试:\n"
                     "  · 更新显卡驱动;\n"
                     "  · 在「设置 → 图形」里换另一块显卡(例如核显/独显互换), 再取消"
                     "「强制软件绘制」重启。",
                )
        else:
            # 留标记: 只有真的走 GPU 才需要(软件渲染不会崩在 wgpu 里)
            if not self._gfx_software:
                self._settings.setValue("gfx_pending", 1)
                self._settings.sync()      # 必须**立刻**落盘 —— 崩了就没机会再写了
        # 录制叠加层(独立透明置顶窗, 显示录制进度/实际采集率; 默认关闭)
        self._overlay_settings = self._load_overlay_settings()

        # 快捷键设置必须先读(菜单里的「同时使用按键轮询」勾选状态要用), 监控对象在后面再建
        self._hotkey_bindings = load_bindings(self._settings)
        self._last_hotkey_at: dict[str, float] = {}
        try:
            self._poll_enabled = int(self._settings.value("hotkey_poll", 1)) != 0
        except (TypeError, ValueError):
            self._poll_enabled = True

        self._build_ui()
        self._build_menu()
        # 叠加层对象必须在 _build_menu 之后建(菜单要读 _overlay_settings),
        # 且必须在 recorder 之前 —— _on_tick 每帧都会用到它
        self.overlay = RecordOverlay(self._overlay_settings, parent=None)
        # 显卡提示: 延到事件循环转起来再弹(那时主窗口已经画出来了, 弹窗有正确的父窗口)。
        # 无头/离屏环境下 `_show_gpu_notice` 自己会跳过 —— 测试不会被阻塞。
        QTimer.singleShot(300, self._show_gpu_notice)
        # 画布活过 4 秒 → 认为"这次 GPU 启动成功", 抹掉崩溃标记。
        # 为什么要等: wgpu 的 panic 常常发生在 surface configure(也就是渲染头几帧),
        # 那一刻之前的代码都已经"成功返回"了 —— 只有真跑几帧才说明没问题。
        QTimer.singleShot(4000, self._clear_gfx_pending)
        self.map_view.set_show_zones(self._show_zones)
        self.map_view.set_show_spawns(self._show_spawns)
        self.map_view.set_show_airfields(self._show_airfields)
        self.map_view.set_dead_style(dim=self._saved_dead_dim, mark=self._saved_dead_mark)
        self._reload_tree()
        if self._merged_maps:
            names = []
            for _drop, keep in self._merged_maps:
                name = self.store.display_name(keep)
                if name not in names:
                    names.append(name)
            self._set_status(
                f"已把同一张地图的重复条目(不同分辨率)合并为: {'、'.join(names)}"
                f"(原数据已备份为 routes.json.bak)", sticky=10.0)
        # 后台轮询 8111
        self.recorder = RouteRecorder(self.store, parent=self, map_cache=self.map_cache)
        self.recorder.tick.connect(self._on_tick)
        self.recorder.map_changed.connect(self._on_map_changed)
        self.recorder.map_cached.connect(self._on_map_cached)
        self.recorder.start_armed.connect(self._on_start_armed)
        self.recorder.recording_started.connect(self._on_recording_started)
        self.recorder.recording_cancelled.connect(self._on_recording_cancelled)
        self.recorder.mark_added.connect(self._on_mark_added)
        self.recorder.recording_finished.connect(self._on_recording_finished)
        self.recorder.global_armed.connect(self._on_global_armed)
        self.recorder.global_started.connect(self._on_global_started)
        self.recorder.global_finished.connect(self._on_global_finished)
        self.recorder.global_cancelled.connect(self._on_global_cancelled)
        self.recorder.notice.connect(self._on_notice)
        if start_recorder:
            self.recorder.start()
        self.recorder.set_interval_ms(self._saved_ms)
        self.lbl_rate.setText(f"轮询 {1000 // int(self._saved_ms)} Hz")

        # 轨迹回放(按倍速推进 playhead)
        self.player = RoutePlayer(parent=self)
        self.player.progress.connect(self._on_playback_progress)
        self.player.playing_changed.connect(self._on_playback_state)
        self.player.finished.connect(self._on_playback_finished)
        self.player.reached_mark.connect(self._on_playback_mark)
        try:
            saved_speed = float(self._settings.value("playback_speed", 1.0))
        except (TypeError, ValueError):
            saved_speed = 1.0
        idx_speed = self.cmb_speed.findData(saved_speed)
        if idx_speed >= 0:
            self.cmb_speed.setCurrentIndex(idx_speed)
        self.player.set_speed(saved_speed)

        # 全局快捷键: **两套通道**同时开着 —— 系统热键(RegisterHotKey)+ 轮询
        # (GetAsyncKeyState); 任意一套命中就触发, 同一动作 0.3s 内去重。
        # 为什么两套: 用户反馈游戏里右Shift+小键盘按了没反应(疑似键盘多键冲突/被挡),
        # 系统热键不靠定时器也不读按键状态, 最可靠; 轮询则能区分左右 Shift 等精确组合。
        self.hotkeys = GlobalHotkeyWatcher(self._hotkey_bindings, parent=self)
        self.hotkeys.triggered.connect(self._hotkey_action)
        if self._poll_enabled:
            self.hotkeys.start()
        self.sys_hotkeys = GlobalHotkey(self._hotkey_bindings, parent=self)
        self.sys_hotkeys.triggered.connect(self._hotkey_action)
        self.sys_hotkeys.start()

        self._update_buttons(False)
        if not GlobalHotkeyWatcher.supported():
            self.lbl_status.setText("全局快捷键仅在 Windows 下可用, 请使用下方按钮操作")
        # 启动时游戏可能没开: 尝试用缓存底图显示已有轨迹
        self._restore_map_for_current_view()

    # ==================================================================== UI
    def _build_menu(self) -> None:
        """顶部菜单栏: 「地图」(管理/缓存) / 「设置」(显示开关) / 「帮助」。"""
        menu = self.menuBar().addMenu("地图(&M)")
        self.act_manage = QAction("地图管理(命名/关联回放关卡代号/校准)…", self)
        self.act_manage.triggered.connect(self._open_map_manager)
        menu.addAction(self.act_manage)
        menu.addSeparator()
        act_dir = QAction("打开地图缓存文件夹", self)
        act_dir.triggered.connect(self._open_map_dir)
        menu.addAction(act_dir)
        self.act_refresh = QAction("用缓存底图重新显示当前轨迹", self)
        self.act_refresh.triggered.connect(self._restore_map_for_current_view)
        menu.addAction(self.act_refresh)

        setting = self.menuBar().addMenu("设置(&S)")
        self.act_zones = QAction("显示战区位置(A/B/C)", self)
        self.act_zones.setCheckable(True)
        self.act_zones.setChecked(self._show_zones)
        self.act_zones.setToolTip(
            "对战局内 8111 给出的战区(capture_zone)画小菱形 + 字母。\n"
            "战局外/看缓存底图时, 画的是这张地图以前见过的战区布局。")
        self.act_zones.toggled.connect(self._on_show_zones)
        setting.addAction(self.act_zones)

        self.act_spawns = QAction("显示重生位置(红/蓝方块)", self)
        self.act_spawns.setCheckable(True)
        self.act_spawns.setChecked(self._show_spawns)
        self.act_spawns.setToolTip(
            "对战局内 8111 给出的重生点(respawn_base_tank)画空心方块:\n"
            "红 = 敌方, 蓝 = 我方。一个位置一个小方块 —— 连成一片的就是“重生区域”\n"
            "(游戏里重生位置就在这个区域里随机分配)。\n"
            "战局外/看缓存底图时, 画的是这张地图这个版本以前记下的重生位置。")
        self.act_spawns.toggled.connect(self._on_show_spawns)
        setting.addAction(self.act_spawns)

        self.act_airfields = QAction("显示机场跑道(蓝/红长条, 示意)", self)
        self.act_airfields.setCheckable(True)
        self.act_airfields.setChecked(self._show_airfields)
        self.act_airfields.setToolTip(
            "把两队机场的跑道画成**长条**(直升机坪是小方块)。\n"
            "坐标与朝向是从回放里「机场防空」阵地的分布估的, 长度按主轴跨度估 ——\n"
            "**只是示意**, 不要用它量距离。\n"
            "8111 实时录制的条目没有这些数据, 所以那种图上不会出现跑道。\n"
            "机场常在底图图框**外面**, 缩小(滚轮)就能看到。")
        self.act_airfields.toggled.connect(self._on_show_airfields)
        setting.addAction(self.act_airfields)

        # ---- 图形(显卡): 用户 2026-10-27 要求"默认自动, 用户觉得有必要就自己指定"。
        # 适配器列表只在本进程**真的加载过** GPU 模块时才枚举 ——
        # 无头/离屏平台上 import wgpu 会让整个进程崩掉(见 _create_map_view 的说明)。
        setting.addSeparator()
        gfx_menu = setting.addMenu("图形(&G)")
        self._gfx_group = QActionGroup(self)
        self._gfx_group.setExclusive(True)
        self._gfx_actions: dict[str, QAction] = {}
        act_auto = QAction("自动选择(推荐)", self)
        act_auto.setCheckable(True)
        act_auto.setToolTip(
            "让程序自己挑: 优先真显卡(D3D12 → Vulkan → OpenGL), **不会**挑 WARP。\n"
            "笔记本双显卡时如果发现选错了卡(比如跑在核显上), 再手动指定。")
        act_auto.triggered.connect(lambda _c=False: self._on_pick_adapter(PREF_AUTO))
        self._gfx_group.addAction(act_auto)
        gfx_menu.addAction(act_auto)
        self._gfx_actions[PREF_AUTO] = act_auto

        if _gpu_module_loaded():
            self._gpu_adapters, self._gpu_adapters_error = enumerate_adapters()
        for opt in self._gpu_adapters:
            act = QAction(opt.label, self)
            act.setCheckable(True)
            act.setToolTip(
                f"{opt.detail}\n"
                + ("⚠ 这是 **CPU 软件模拟**(WARP) —— 能用, 但比软件绘制还慢,\n"
                   "  只在排查问题时选它。\n" if opt.is_fallback else
                   "选中后**重启程序**才会生效。\n"))
            act.triggered.connect(
                lambda _c=False, k=opt.key: self._on_pick_adapter(k))
            self._gfx_group.addAction(act)
            gfx_menu.addAction(act)
            self._gfx_actions[opt.key] = act
        if self._gpu_adapters_error:
            hint = QAction(f"（枚举显卡失败: {self._gpu_adapters_error}）", self)
            hint.setEnabled(False)
            gfx_menu.addAction(hint)
        elif _gpu_module_loaded() and not self._gpu_adapters:
            hint = QAction("（没有枚举到任何显卡）", self)
            hint.setEnabled(False)
            gfx_menu.addAction(hint)

        gfx_menu.addSeparator()
        self.act_gfx_software = QAction("强制软件绘制(不用 GPU, 最慢但最保险)", self)
        self.act_gfx_software.setCheckable(True)
        self.act_gfx_software.setChecked(self._gfx_software)
        self.act_gfx_software.setToolTip(
            "完全不碰显卡: 用 QPainter 画线。\n"
            "显卡驱动有问题、远程桌面/虚拟机里跑、或者不确定是不是显卡导致卡顿时勾它。\n"
            "⚠ 改了以后**重启程序**才生效。")
        self.act_gfx_software.triggered.connect(
            lambda _c=False: self._on_pick_adapter(PREF_SOFTWARE))
        # 三种选择互斥: 自动 / 某块卡 / 强制软件 —— 所以它也在同一个动作组里
        self._gfx_group.addAction(self.act_gfx_software)
        gfx_menu.addAction(self.act_gfx_software)
        self._gfx_actions[PREF_SOFTWARE] = self.act_gfx_software
        act_gfx_info = QAction("当前用的渲染方式…", self)
        act_gfx_info.triggered.connect(self._show_gfx_info)
        gfx_menu.addAction(act_gfx_info)
        # 勾选状态按**本次实际生效**的来(含"上次崩了自动切软件"的情况)。
        # 指定的那块卡如果已经不存在了, 就把「自动」勾上 —— 这也正是运行时会做的事。
        _cur = PREF_SOFTWARE if self._gfx_software else self._gfx_prefer
        if _cur not in self._gfx_actions:
            _cur = PREF_AUTO
        self._gfx_actions[_cur].setChecked(True)

        # ---- 轮询间隔: 从信息区挪到菜单(2026-10-23)。
        # 用户口径: “采样设置不该让用户经常改, 最好是定在一个合适的数值, 现在这项功能
        # 在主界面太显眼, 用户可能没事干就会调得过高或过低导致 bug。”
        # 曾经拆成三档(单条 / 全局 / hudmsg); 1.3.0 改用回放文件之后后两档已经
        # 没有用武之地, 用户 2026-10-26 要求**只留单条录制**这一档。
        setting.addSeparator()
        rate_menu = setting.addMenu("轮询间隔(&R)")
        rate_menu.setToolTip(
            "单条线路录制的轮询间隔: 决定主循环频率与单条轨迹的采样密度。\n"
            "一般不用改: 40 ms(25 Hz)与 8111 陆战数据的更新率一致。")
        self._rate_actions = []
        _rate_group = QActionGroup(self)
        _rate_group.setExclusive(True)
        for _ms in INTERVAL_CHOICES_MS:
            _text = (f"{_ms} ms({1000 // _ms} Hz)" if _ms < 1000
                     else f"{_ms} ms({_ms / 1000:g} s)")
            _act = QAction(_text, self)
            _act.setCheckable(True)
            _act.setChecked(int(_ms) == int(self._saved_ms))
            _act.setToolTip(rate_menu.toolTip())
            _act.triggered.connect(
                lambda _c=False, v=int(_ms): self._set_record_rate_ms(v))
            _rate_group.addAction(_act)
            rate_menu.addAction(_act)
            self._rate_actions.append(_act)

        # ---- 叠加层(录制时浮在屏幕角落的小窗)
        setting.addSeparator()
        ov_menu = setting.addMenu("叠加层(&O)")
        self.act_overlay = QAction("启用录制叠加层", self)
        self.act_overlay.setCheckable(True)
        self.act_overlay.setChecked(self._overlay_settings.enabled)
        self.act_overlay.setToolTip(
            "勾选后, 开始录制(单条 / A·B 队)时在屏幕角落浮出小块信息:\n"
            "状态与用时、**实际采集率**(每秒写入多少个轨迹点)、8111 轮询频率、\n"
            "轨迹点数、标记数、当前速度与载具; 结束录制自动隐藏。\n"
            "它是独立的外部置顶窗(点击穿透), 不注入游戏 —— "
            "要浮在游戏上请用「全屏窗口(无边框)」模式。")
        self.act_overlay.toggled.connect(self._on_overlay_enabled)
        ov_menu.addAction(self.act_overlay)
        ov_menu.addSeparator()

        # 位置: 四角之一(动作组互斥)
        pos_menu = ov_menu.addMenu("位置")
        self._overlay_pos_group = QActionGroup(self)
        self._overlay_pos_group.setExclusive(True)
        self._overlay_pos_actions: dict[str, QAction] = {}
        for key, name in ANCHORS.items():
            act = QAction(name, self)
            act.setCheckable(True)
            act.setChecked(key == self._overlay_settings.anchor)
            act.triggered.connect(lambda _c=False, k=key: self._on_overlay_anchor(k))
            self._overlay_pos_group.addAction(act)
            self._overlay_pos_actions[key] = act
            pos_menu.addAction(act)

        act_ov_set = QAction("叠加层设置(边距 / 背景不透明度 / 圆角 / 字号)…", self)
        act_ov_set.triggered.connect(self._open_overlay_settings)
        ov_menu.addAction(act_ov_set)

        hk_menu = self.menuBar().addMenu("快捷键(&K)")
        act_hk_set = QAction("快捷键设置(按键 / 修饰键)…", self)
        act_hk_set.setToolTip("给“开始录制 / 结束录制 / 标记位置”分别指定按键, 改动立即生效。")
        act_hk_set.triggered.connect(self._open_hotkey_settings)
        hk_menu.addAction(act_hk_set)
        self.act_poll = QAction("同时使用按键轮询(可区分左右 Shift)", self)
        self.act_poll.setCheckable(True)
        self.act_poll.setChecked(self._poll_enabled)
        self.act_poll.setToolTip(
            "主通道是**系统热键**(RegisterHotKey): 由 Windows 直接把按键事件通知本程序,\n"
            "本程序既不读按键状态也不挂钩子。\n"
            "这一项是额外的兜底通道(GetAsyncKeyState 轮询), 好处是能区分“右Shift”这种精确组合,\n"
            "而且能兼容某些系统热键注册失败的键。\n"
            "如果你不想让程序去查询按键状态, 可以关掉它(只留系统热键)。\n"
            "两条通道都只是“接收按键通知”: 不注入按键、不挂钩子、不碰游戏进程。")
        self.act_poll.toggled.connect(self._on_poll_toggled)
        hk_menu.addAction(self.act_poll)
        hk_menu.addSeparator()
        act_hk_reset = QAction("恢复默认快捷键(F5 / F6 / F7)", self)
        act_hk_reset.triggered.connect(self._reset_hotkeys)
        hk_menu.addAction(act_hk_reset)

        snd_menu = self.menuBar().addMenu("音效(&A)")
        self.act_sound_on = QAction("启用提示音", self)
        self.act_sound_on.setCheckable(True)
        self.act_sound_on.setChecked(sound_settings().enabled)
        self.act_sound_on.toggled.connect(self._on_sound_toggled)
        snd_menu.addAction(self.act_sound_on)
        act_sound_set = QAction("音效设置(音量 / 自定义音效文件)…", self)
        act_sound_set.triggered.connect(self._open_sound_settings)
        snd_menu.addAction(act_sound_set)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        act_about = QAction("关于 / 功能说明(快捷键、界面用法)…", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    # ------------------------------------------------------------ 图形(显卡)
    def _on_pick_adapter(self, key: str) -> None:
        """菜单「设置 → 图形」里选了 自动 / 某块卡 / 强制软件绘制。

        **只存不改**: pygfx 的 device 是全局单例, 建好之后不能再换 ——
        所以三项都是"下次启动生效", 界面上必须这么说, 别让用户以为是热切换。
        """
        if key == PREF_SOFTWARE:
            new_prefer, new_soft = self._gfx_prefer_saved, True
        else:
            new_prefer, new_soft = key, False
        if (new_prefer, new_soft) == (self._gfx_prefer_saved, self._gfx_software_saved):
            return
        self._settings.setValue("gfx_adapter", new_prefer)
        self._settings.setValue("gfx_software", 1 if new_soft else 0)
        # 换了显卡/换回 GPU → 把"上次崩过"的结论作废, 让下次启动真的去试一次。
        # (否则用户修好驱动、选回显卡, 却因为旧标记仍然走软件, 会以为设置没生效)
        self._settings.setValue("gfx_pending", 0)
        self._settings.sync()
        self._gfx_prefer_saved, self._gfx_software_saved = new_prefer, new_soft
        if new_soft:
            what = "软件绘制(QPainter)"
        elif key == PREF_AUTO:
            what = "自动选择显卡"
        else:
            what = f"指定显卡「{key.replace('|', ' / ')}」"
        log.info("图形设置改为 %s(下次启动生效)", what)
        QMessageBox.information(
            self, "图形设置",
            f"已记录: {what}\n\n"
            "⚠ 这个设置**重启程序后才生效** —— 显卡渲染设备只能在启动时建一次。")

    def _show_gfx_info(self) -> None:
        """菜单「设置 → 图形 → 当前用的渲染方式…」: 把实际状态摆出来。

        用户报"卡"时让他点这里, 一眼就能看出是走了 WARP、走了软件渲染,
        还是显卡正常(而卡在别的地方)。
        """
        lines = [f"本次实际使用: {getattr(self.map_view, 'gpu_adapter', '未知')}", ""]
        lines.append(f"设置项: gfx_adapter = {self._gfx_prefer!r}, "
                     f"gfx_software = {int(self._gfx_software)}")
        if self._gfx_software:
            lines.append("  （强制软件绘制 / 或上次启动崩溃后自动切换）")
        lines.append("")
        if not _gpu_module_loaded():
            lines.append("GPU 模块没有加载(无头平台或组件缺失), 未枚举显卡。")
        elif self._gpu_adapters_error:
            lines.append(f"枚举显卡失败: {self._gpu_adapters_error}")
        else:
            lines.append(f"本机共 {len(self._gpu_adapters)} 块适配器:")
            for i, opt in enumerate(self._gpu_adapters, 1):
                mark = " ←★" if opt.key == self._gfx_prefer else ""
                lines.append(f"  [{i}] {opt.label}   ({opt.detail}){mark}")
        lines.append("")
        lines.append("说明: 只有 Windows 桌面被支持; 远程桌面 / 云电脑 / 虚拟机不保证可用;")
        lines.append("      2012 年之前的显卡(DX12 之前)驱动已停止维护, 同样不支持。")
        lines.append("细节见 data\\route_tester.log。")
        QMessageBox.information(self, "渲染方式", "\n".join(lines))

    def _show_gpu_notice(self) -> None:
        """启动后把 `gpu_notice` 弹一次 —— 让用户知道"为什么没用上显卡"。

        以前是静默回退软件渲染, 用户只觉得卡, 完全不知道原因。
        **无头/离屏环境不弹**(测试会卡死), 只写日志。
        """
        notice = self._gpu_notice or getattr(self.map_view, "gpu_notice", None)
        if notice is None or self._gpu_notice_shown:
            return
        self._gpu_notice_shown = True
        log.warning("显卡提示[%s] %s | %s", notice.kind, notice.title, notice.body)
        app = QApplication.instance()
        if app is not None and app.platformName().lower() in ("offscreen", "minimal", "vnc"):
            return
        icon = (QMessageBox.Icon.Information if notice.kind == "adapter-gone"
                else QMessageBox.Icon.Warning)
        box = QMessageBox(icon, "显卡", notice.as_text(), QMessageBox.StandardButton.Ok, self)
        box.setDetailedText(
            (notice.detail or "") + "\n\n"
            "可以在「设置 → 图形」里手动指定显卡, 或勾选「强制软件绘制」去掉这条提示。")
        box.exec()

    def _clear_gfx_pending(self) -> None:
        """画布活下来了 → 抹掉"我正在尝试 GPU"的标记。

        只在**已经用上 GPU**时才需要(软件渲染不会崩在 wgpu 里)。
        调用时机见 `__init__` 里那个 4 秒的 QTimer。
        """
        if self._gfx_software:
            return
        if self._settings.contains("gfx_pending"):
            self._settings.setValue("gfx_pending", 0)
            self._settings.sync()

    def _show_about(self) -> None:
        """菜单「帮助 → 关于」: 全部说明都收在这里。
        界面上不再放常驻提示文字(侧栏底部、信息区那几行已删除) ——
        那些文字只在第一次用的时候有用, 之后纯粹是占地方。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("关于 / 功能说明 —— 跑图线路测试器")
        dlg.resize(780, 660)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(10, 10, 10, 10)
        view = QTextBrowser(dlg)
        view.setOpenExternalLinks(True)
        view.setHtml(about_html(self._hotkey_bindings))
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    # ------------------------------------------------------------ 录制叠加层
    def _load_overlay_settings(self) -> RecordOverlaySettings:
        """从 QSettings 读叠加层外观(读不到/值损坏就退回默认)。"""
        s = self._settings

        def _int(key: str, default: int) -> int:
            try:
                return int(s.value(key, default))
            except (TypeError, ValueError):
                return default

        return RecordOverlaySettings(
            enabled=_int("overlay_enabled", 0) != 0,
            anchor=str(s.value("overlay_anchor", "") or ""),
            margin=_int("overlay_margin", 24),
            opacity=_int("overlay_opacity", 62),
            corner=_int("overlay_corner", 10),
            font_size=_int("overlay_font_size", 10),
        ).normalized()

    def _save_overlay_settings(self) -> None:
        o = self._overlay_settings
        s = self._settings
        s.setValue("overlay_enabled", 1 if o.enabled else 0)
        s.setValue("overlay_anchor", o.anchor)
        s.setValue("overlay_margin", int(o.margin))
        s.setValue("overlay_opacity", int(o.opacity))
        s.setValue("overlay_corner", int(o.corner))
        s.setValue("overlay_font_size", int(o.font_size))

    def _on_overlay_enabled(self, on: bool) -> None:
        self._overlay_settings.enabled = bool(on)
        self.overlay.apply_settings(self._overlay_settings)
        self._save_overlay_settings()
        self._set_status(
            "录制叠加层已启用: 开始录制时会在屏幕%s显示"
            % ANCHORS.get(self._overlay_settings.anchor, "角落")
            if on else "录制叠加层已关闭", sticky=3.0)

    def _on_overlay_anchor(self, key: str) -> None:
        self._overlay_settings.anchor = key
        self.overlay.apply_settings(self._overlay_settings)
        self._save_overlay_settings()
        act = self._overlay_pos_actions.get(key)
        if act is not None and not act.isChecked():
            act.setChecked(True)

    def _open_overlay_settings(self) -> None:
        """「设置 → 叠加层 → 叠加层设置…」: 改动实时预览, 取消则回滚。"""
        before = RecordOverlaySettings(**vars(self._overlay_settings))
        dlg = RecordOverlayDialog(self._overlay_settings, self)

        def on_changed() -> None:
            self._overlay_settings = dlg.settings
            self.overlay.apply_settings(self._overlay_settings)

        dlg.settings_changed.connect(on_changed)
        ok = dlg.exec() == QDialog.DialogCode.Accepted
        if not ok:
            self._overlay_settings = before          # 取消 → 回到打开前的样子
            self.overlay.apply_settings(self._overlay_settings)
            return
        self._overlay_settings = dlg.settings
        self.overlay.apply_settings(self._overlay_settings)
        self._save_overlay_settings()
        if self.act_overlay.isChecked() != self._overlay_settings.enabled:
            self.act_overlay.setChecked(self._overlay_settings.enabled)
        act = self._overlay_pos_actions.get(self._overlay_settings.anchor)
        if act is not None:
            act.setChecked(True)
        self._set_status("叠加层设置已保存", sticky=3.0)

    def _update_overlay(self, info: dict) -> None:
        """把当前录制状态推给叠加层(由 `_on_tick` 的 ~12Hz 文本节流块驱动)。

        显示的内容:

        * 第一行: 状态 + 用时(准备录制 / 录制中 / A·B 队);
        * **采集 x 点/秒**: 最近 2 秒真正写进轨迹的点数率 —— 这才是"实际采样率",
          它低于轮询频率是正常的(采样还要过“采样间隔 + 最小位移”两道闸,
          车不动时一个点也不写); 两个数并排显示就能一眼看出是哪一边的问题;
        * 点数 / 标记数 / 当前速度 / 载具。

        注意: 判断"要不要显示"必须看**单条录制与全局录制两个状态** ——
        `info["recording"]` 只表示单条录制(`recorder._rec is not None`),
        全局录制时它一直是 False(以前就因此 A/B 队录制时叠加层根本不出现)。
        """
        if not self._overlay_settings.enabled:
            return
        g = info.get("global")
        if not info.get("recording") and not g:
            self.overlay.set_active(False)
            return
        rate = float(info.get("rate") or 0.0)
        if g:
            # 全局录制: 0 点由"第一名玩家重生"定下, 所以等待阶段的用时没有意义
            side = str(g.get("side") or "?")
            lives = int(g.get("lives") or 0)
            active = int(g.get("active") or 0)
            if g.get("waiting"):
                head = f"● {side} 队: 等待第一名玩家重生…"
                elapsed = 0.0
            else:
                head = f"● {side} 队录制中"
                elapsed = float(g.get("elapsed") or 0.0)
            lines = [
                (f"{head}  {fmt_time(elapsed)}", COLOR_TITLE),
                (f"已录线路 {lives} 条 · 进行中 {active} 辆", COLOR_TEXT),
                (f"8111 轮询 {rate:.1f} Hz", COLOR_TEXT),
            ]
        else:
            elapsed = float(info.get("elapsed") or 0.0)
            head = "● 准备录制 等待载具移动…" if info.get("armed") else "● 录制中"
            lines = [
                (f"{head}  {fmt_time(elapsed)}", COLOR_TITLE),
                (f"采集 {float(info.get('point_rate') or 0.0):.1f} 点/秒 · "
                 f"轮询 {rate:.1f} Hz", COLOR_TEXT),
                (f"点数 {int(info.get('points') or 0)} · "
                 f"标记 {int(info.get('marks') or 0)} 个", COLOR_TEXT),
                (f"{fmt_speed(float(info.get('speed') or 0.0))} km/h · "
                 f"{info.get('vehicle') or '未知载具'}", COLOR_TEXT),
            ]
        self.overlay.set_lines(lines)
        self.overlay.set_active(True)

    def _on_show_zones(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画战区。"""
        self._show_zones = bool(checked)
        self._settings.setValue("show_zones", 1 if checked else 0)
        self.map_view.set_show_zones(self._show_zones)
        self._set_status("已显示地图上的战区位置" if checked else "已隐藏地图上的战区位置",
                         sticky=2.0)

    def _on_sound_toggled(self, on: bool) -> None:
        """菜单开关: 提示音总开关(立即落盘)。"""
        sound_settings().set_enabled(bool(on))
        self._set_status("已启用提示音" if on else "已关闭提示音(所有音效都不再播放)",
                         sticky=2.0)
        if on:
            play_sound("mark")          # 打开时响一声, 便于确认音量

    def _on_show_spawns(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画重生位置(红/蓝空心方块)。"""
        self._show_spawns = bool(checked)
        self._settings.setValue("show_spawns", 1 if checked else 0)
        self.map_view.set_show_spawns(self._show_spawns)
        self._set_status("已显示地图上的重生位置(红=敌方, 蓝=我方)" if checked
                         else "已隐藏地图上的重生位置", sticky=2.0)

    def _on_show_airfields(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画机场跑道(回放导入的条目才有数据)。"""
        self._show_airfields = bool(checked)
        self._settings.setValue("show_airfields", 1 if checked else 0)
        self.map_view.set_show_airfields(self._show_airfields)
        self._set_status("已显示机场跑道(示意, 长度是估的)" if checked
                         else "已隐藏机场跑道", sticky=2.0)

    def _open_sound_settings(self) -> None:
        """菜单「音效 → 音效设置」: 音量 / 自定义音效文件(改动立即生效)。"""
        dlg = SoundSettingsDialog(sound_settings(), self)
        dlg.exec()
        self.act_sound_on.setChecked(sound_settings().enabled)   # 对话框里可能改过开关
        self._set_status("音效设置已更新", sticky=2.0)

    def _open_map_dir(self) -> None:
        try:
            os.startfile(self.map_cache.dir)     # noqa: S606 - 仅 Windows
        except OSError as exc:
            self._set_status(f"打开缓存目录失败: {exc}", sticky=3.0)

    def _open_map_manager(self) -> None:
        """打开「地图管理」; 关掉后总是刷新侧栏(名字可能改了, 也可能删了缓存图片)。"""
        dlg = MapManagerDialog(self.store, self.map_cache, self)
        dlg.exec()
        self._reload_tree()
        if dlg.calib_changed:
            # 校准/底图变了 → 当前这张图立刻重画一次(否则还挂着旧图或空白网格)
            key = self.map_view.map_key
            data = self.map_cache.load(key) if key else None
            if data is not None:
                self.map_view.set_map_bytes(data, key=key)
                self._refresh_map_tracks()
            self._set_status(
                f"已更新 {dlg.calib_changed} 处地图校准(回放导入的线路已按新比例对齐)",
                sticky=4.0)
            return
        hidden = sum(1 for k in self.store.ordered_map_keys()
                     if self.store.is_map_hidden(k))
        tail = f", 已隐藏 {hidden} 张" if hidden else ""
        if dlg.renamed:
            self._set_status(f"已保存 {dlg.renamed} 处地图改动(侧栏已同步{tail})",
                             sticky=3.0)
        else:
            self._set_status(f"地图管理已关闭(名字/关卡代号关联都会立即保存{tail})", sticky=2.0)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.addWidget(self._build_sidebar())

        right = QWidget(splitter)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.map_view = _create_map_view(right, prefer=self._gfx_prefer,
                                         force_software=self._gfx_software)
        rl.addWidget(self.map_view, 1)
        rl.addWidget(self._build_info_panel(right), 0)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1080])
        root.addWidget(splitter)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------ 侧栏
    def _build_sidebar(self) -> QWidget:
        box = QWidget(self)
        box.setMinimumWidth(200)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 4, 8)
        layout.setSpacing(6)

        title = QLabel("地图 / 线路")
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        self.tree = RouteTreeWidget(box)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["展开/收起", "地图 / 线路", "信息"])
        # 缩进会占用第 0 列: 二级条目内容从 ~2×缩进 处开始, 因此眼睛列必须够宽,
        # 否则图标会溢出到名称列、与文字叠在一起(Qt 不会把图标裁剪在列内)
        self.tree.setIndentation(12)
        self.tree.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        # 眼睛列固定宽(缩进 + 图标 + 间距), 名称列吃掉剩余宽度, “用时”按内容自适应
        header = self.tree.header()
        # QTreeWidget 默认让最后一列拉伸(stretchLastSection), 会顶掉下面的列宽设置
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COL_EYE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(24)
        self.tree.setColumnWidth(_COL_EYE, EYE_COLUMN_WIDTH)
        # 选中行不够显眼 → 把选中底色调**深**(系统默认的浅蓝高亮在两种主题下都偏淡)。
        # 用 palette 而不是 QSS: QSS 的 `::item` 会接管单元格绘制, 容易连带改掉
        # 交替行/图标/文字顏色; 只换 Highlight 两个颜色, 其余绘制保持系统原生。
        pal = self.tree.palette()
        # Active / Inactive 两组都要设: 窗口失焦时 Qt 用的是 Inactive.Highlight,
        # 只设 Active 的话切出去再回来会看到浅灰高亮(实测取样仍是 #f3f3f3)。
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            pal.setColor(group, QPalette.ColorRole.Highlight, QColor("#1d4ed8"))
            pal.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#f8fafc"))
        self.tree.setPalette(pal)
        # 选中的条目决定「回放」播哪几条(可 Shift/Ctrl 多选);
        # 而「地图上有没有显示」完全由眼睛图标控制 —— 两者职责分开。
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_menu)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        # 展开一个「全局录制」条目时才去建它下面那 200 多条线路行(见 `_populate_global_item`)
        self.tree.itemExpanded.connect(self._on_tree_item_expanded)
        # 双击轨迹行 = 把它所在的“地图·模式·版本”定为基准(之后多选只在该版本内)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        layout.addWidget(self.tree, 1)
        return box

    # -------------------------------------------------------------- 信息显示区
    def _build_info_panel(self, parent=None) -> QWidget:
        panel = QFrame(parent)
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        # 5 行内容(回放 / 分割线 / 状态+间隔 / 载具… / 录制按钮)的最小高度约 151,
        # 取 172 留出余量, 同时固定高度可让上方地图区域不随文字变化抖动
        panel.setFixedHeight(172)
        grid = QGridLayout(panel)
        grid.setContentsMargins(12, 8, 12, 10)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)

        # ---- 第 0 行: 轨迹回放(置顶, 与下方录制功能用分割线隔开)
        play_box = QHBoxLayout()
        play_box.setSpacing(8)
        self.btn_play = QPushButton("▶ 回放")
        self.btn_play.setMinimumHeight(30)
        self.btn_play.clicked.connect(self._toggle_playback)
        play_box.addWidget(self.btn_play)

        self.btn_play_mark = QPushButton("» 到下一标记暂停")
        self.btn_play_mark.setMinimumHeight(30)
        self.btn_play_mark.clicked.connect(self._play_to_next_mark)
        play_box.addWidget(self.btn_play_mark)

        play_box.addWidget(QLabel("倍速:"))
        self.cmb_speed = QComboBox()
        for x in PLAYBACK_SPEEDS:
            self.cmb_speed.addItem(f"{x:g}X", x)
        self.cmb_speed.currentIndexChanged.connect(self._on_speed_changed)
        self.cmb_speed.setToolTip("回放倍速 0.5X ~ 16X")
        play_box.addWidget(self.cmb_speed)

        # ---- 回放显示选项(两个都默认勾选)
        # “被击毁”判定只看 8111: 线路对应的 icon 突然消失、且当时场上还有别的单位
        # → 算阵亡; 全场一起消失(回到车库/录制结束) 不算(有人整局不死)。
        self.chk_dead_dim = QCheckBox("被击毁后路线变暗")
        self.chk_dead_dim.setChecked(self._saved_dead_dim)
        self.chk_dead_dim.setToolTip(
            "回放时, 线路对应的玩家被击毁之后, 这条线路整条变成半透明纯灰\n"
            "(不再按速度着色), 一眼能看出哪些人已经阵亡。")
        self.chk_dead_dim.stateChanged.connect(self._on_dead_style_changed)
        play_box.addWidget(self.chk_dead_dim)

        self.chk_dead_mark = QCheckBox("被击毁标记")
        self.chk_dead_mark.setChecked(self._saved_dead_mark)
        self.chk_dead_mark.setToolTip(
            "在被击毁的位置(线路末端)画一个红色 X, 大小与回放光标白点相当。\n"
            "回放越过阵亡时刻后才出现。")
        self.chk_dead_mark.stateChanged.connect(self._on_dead_style_changed)
        play_box.addWidget(self.chk_dead_mark)

        self.lbl_play = QLabel("回放: --")
        self.lbl_play.setStyleSheet("color:#94a3b8; font-size:11px;")
        play_box.addWidget(self.lbl_play, 1)

        # 回放期间其余功能会被禁用, 必须用它退出回放(见 _update_buttons)
        self.btn_play_stop = QPushButton("■ 结束播放")
        self.btn_play_stop.setMinimumHeight(30)
        self.btn_play_stop.setToolTip("退出回放并恢复显示眼睛点亮的轨迹(回放期间其余功能不可用)")
        self.btn_play_stop.clicked.connect(self._end_playback)
        play_box.addWidget(self.btn_play_stop)
        grid.addLayout(play_box, 0, 0, 1, 4)

        # 分割线: 用 1px 实心线(而非 Sunken QFrame —— 在浅色主题下槽线对比度太低)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background-color:#94a3b8; border:none;")
        grid.addWidget(line, 1, 0, 1, 4)

        # ---- 第 2 行: 连接状态(+ 一个极小的只读轮询频率)
        # 两段长文字都允许被压缩(否则窄窗口下会把侧栏挤到最小宽度)
        self.lbl_status = QLabel("正在连接 8111…")
        self.lbl_status.setStyleSheet("color:#94a3b8;")
        self.lbl_status.setMinimumWidth(240)
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        grid.addWidget(self.lbl_status, 2, 0, 1, 3)

        # 采样间隔**不再放在主界面**(2026-10-23): 它是装机时定一次的东西, 摆在
        # 信息区里太显眼 —— 用户随手调到过大/过小反而会出问题。现在收进
        # 菜单「设置 → 采样间隔」; 这里只留一个很小的只读实际轮询频率(排查用)。
        self.lbl_rate = QLabel("")
        self.lbl_rate.setStyleSheet("color:#64748b; font-size:11px;")
        self.lbl_rate.setToolTip(
            "8111 的实际轮询频率。\n采样间隔在菜单「设置 → 采样间隔」里, 一般不用改")
        grid.addWidget(self.lbl_rate, 2, 3, alignment=Qt.AlignmentFlag.AlignRight)

        # ---- 第 3 行: 载具 / 速度 / 计时 / 标记
        self.lbl_vehicle = self._info_field("载具", "—")
        self.lbl_speed = self._info_field("速度", "— km/h")
        self.lbl_timer = self._info_field("计时", "00:00")
        self.lbl_marks = self._info_field("标记", "0 个")
        self.lbl_timer.setStyleSheet("font-size:18px; font-weight:bold; color:#f59e0b;")

        grid.addWidget(self.lbl_vehicle, 3, 0)
        grid.addWidget(self.lbl_speed, 3, 1)
        grid.addWidget(self.lbl_timer, 3, 2)
        grid.addWidget(self.lbl_marks, 3, 3)

        # ---- 第 4 行: 录制操作按钮
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_start = QPushButton("开始录制轨迹")
        self.btn_stop = QPushButton("结束录制轨迹")
        self.btn_mark = QPushButton("标记当前位置")
        self.btn_start.clicked.connect(lambda: self._handle_action("start"))
        self.btn_stop.clicked.connect(lambda: self._handle_action("stop"))
        self.btn_mark.clicked.connect(lambda: self._handle_action("mark"))
        # 图标 = 地图上对应的那个圈(绿色起点圈 / 红色终点圈 / 黄色标记圈),
        # 颜色与相对大小都跟地图一致, 一眼能对上(见 icons.circle_icon)
        for btn, kind in ((self.btn_start, "start"), (self.btn_stop, "stop"),
                          (self.btn_mark, "mark")):
            btn.setIcon(circle_icon(kind))
            btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        for b in (self.btn_start, self.btn_stop, self.btn_mark):
            b.setMinimumHeight(34)
            btns.addWidget(b)
        self.btn_start.setStyleSheet("font-weight:bold;")

        # 可选: 不按“点下按钮的时刻”计时, 而以速度从 0 变为非 0 的前一时刻为起点
        # (非默认开启: 每次启动都是未勾选状态)
        self.chk_start_on_move = QCheckBox("以速度从零变化时刻作为起始时间点")
        self.chk_start_on_move.setChecked(self._saved_on_move)
        self.chk_start_on_move.setToolTip(
            "勾选后, 点「开始录制轨迹」不会立即计时:\n"
            "等载具速度从 0 变为非 0 时, 以最近一次速度为 0 的时刻作为计时起点。\n"
            "适合“点开始录制 → 切回游戏 → 再起步”的用法, 切窗口的延迟不会被算进成绩。\n"
            "注: 若开始时车已在行驶(回溯窗口内没有速度为 0 的时刻), 仍按按下时刻计时。")
        self.chk_start_on_move.stateChanged.connect(self._on_start_on_move_changed)
        btns.addWidget(self.chk_start_on_move)
        btns.addStretch(1)

        # ---- 竖线分隔: 左边是“单条线路录制”, 右边是“全局录制”(整局所有玩家的线路)
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet("background-color:#94a3b8; border:none;")
        sep.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        btns.addSpacing(10)
        btns.addWidget(sep)
        btns.addSpacing(10)

        self.btn_imp_local = QPushButton("从录像中获取路线(本地)")
        self.btn_imp_server = QPushButton("从录像中获取路线(服务器)")
        self.btn_imp_local.setToolTip(
            "选一个**本地回放文件**(`.wrpl`, 游戏内「保存回放」得到的那个)。\n"
            "一次就能拿到这局**两队全部地面单位**的轨迹(不用分 A/B 两次录)、\n"
            "也不用对轴 —— 回放本身就是同一条时间轴。\n"
            "地图名直接取自回放头(`levels/avg_xxx.bin`)。\n"
            "⚠ 本地回放对「没被看见的单位」只会低频记录(约 2 Hz),"
            "想要精确的逐帧轨迹请用服务器回放。")
        self.btn_imp_server.setToolTip(
            "选**服务器回放分片所在的目录**(官网下载的那一堆 `.wrpl` 放同一个文件夹)。\n"
            "服务器回放的采样率是 24 Hz(本地约 2 Hz), 路面细节更完整。\n"
            "不要只选一个分片 —— 单个分片只有 1/N 的对局数据。")
        self.btn_imp_local.clicked.connect(self._import_replay_local)
        self.btn_imp_server.clicked.connect(self._import_replay_server)
        for b in (self.btn_imp_local, self.btn_imp_server):
            b.setMinimumHeight(34)
            btns.addWidget(b)
        self.btn_imp_local.setStyleSheet("font-weight:bold;")
        grid.addLayout(btns, 4, 0, 1, 4)

        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(3, 1)
        return panel

    @staticmethod
    def _info_field(name: str, value: str) -> QLabel:
        lbl = QLabel(f"{name}: {value}")
        lbl.setStyleSheet("font-size:13px;")
        return lbl

    # ==================================================================== 侧栏内容
    def _iter_route_items(self):
        """按侧栏顺序递归遍历全部**轨迹条目**(当前是 地图 → 模式 → 版本 → 轨迹)。

        递归而不是写死层数: 以后再加/减一层也不用改遍历处。
        """
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, _ROLE_KIND) == "route":
                    yield ch
                else:
                    yield from walk(ch)

        root = self.tree.invisibleRootItem()
        yield from walk(root)

    def _iter_life_items(self):
        """递归遍历全部**全局录制里的线路条目**(全局条目 → A/B 队 → 线路)。"""
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, _ROLE_KIND) == "glife":
                    yield ch
                else:
                    yield from walk(ch)

        root = self.tree.invisibleRootItem()
        yield from walk(root)

    def _life_ids_of(self, item: QTreeWidgetItem) -> list[str]:
        """某个全局条目 / 队伍行下面的全部线路 id。"""
        if item.data(0, _ROLE_KIND) == "glife":
            return [item.data(0, _ROLE_ID)]
        out: list[str] = []

        def walk(node: QTreeWidgetItem) -> None:
            for i in range(node.childCount()):
                ch = node.child(i)
                if ch.data(0, _ROLE_KIND) == "glife":
                    out.append(ch.data(0, _ROLE_ID))
                else:
                    walk(ch)

        walk(item)
        return out

    def _life_map_keys(self) -> dict[str, str]:
        """{线路 id: 所属地图键(已按图片哈希归并)} —— 换图时用它判断哪些全局线路已不属于当前地图。

        ⚠ 按 `life_id` 的 `<条目 id>:` 前缀**直接查**, 不去枚举每条线路 ——
        一局 200 多条线, 五百局就是 10 万条, 枚举一次会把所有会话都读进内存。
        已删掉的条目自然查不到(调用方会把那些 id 当成“不属于当前地图”清掉)。
        """
        by_gid = {g.route_id: self.store.canon(g.map_key) for g in self.store.globals}
        out: dict[str, str] = {}
        for lid in self._visible_lives:
            gid = lid.rpartition(":")[0]
            if gid in by_gid:
                out[lid] = by_gid[gid]
        return out

    def _tree_modes(self):
        """把「单条路线」与「全局录制条目」合并成 **地图 → 模式** 两层。

        用户 2026-10-26 定的地图结构(不要再去细分区域了):
            一张地图 = 3 个模式 —— **战区控制(3 点) / 战区攻陷(2 点) / 战区争夺(1 点)**;
            控制与攻陷再分大图/小图, 争夺是从 3 个战区里随机抽一个 ——
            它们都是**同一张图的同一个模式**, 只是作战范围不同, 不该拆成两行。

        返回 `[(地图键, [(模式名, [路线...], [全局条目...])])]`。
        全局条目按 `(地图键, 模式名)` 归到对应模式; 某个模式**只有**全局条目
        (还没录过单条路线)也会补一组, 否则它就没地方放。
        """
        routes = self.store.grouped_by_mode()
        glob = self.store.globals_by_mode()
        out: list[tuple[str, list[tuple[str, list, list]]]] = []
        for key in self.store.ordered_map_keys():
            if self.store.is_map_hidden(key):
                continue                     # 用户在「地图管理」里勾了隐藏
            modes: list[tuple[str, list, list]] = []
            used: set[str] = set()
            for mode_label, recs in routes.get(key, []):
                modes.append((mode_label, list(recs), glob.get((key, mode_label), [])))
                used.add(mode_label)
            for (mk, mode_label), items in glob.items():
                if mk != key or mode_label in used:
                    continue
                modes.append((mode_label, [], list(items)))
            if modes:
                out.append((key, modes))
        return out

    @staticmethod
    def _count_text(n_route: int, n_global: int) -> str:
        """分组行的“信息”列: 单条路线条数 + 全局录制局数(没有全局录制就不写)。"""
        return f"{n_route} 条 · {n_global} 局" if n_global else f"{n_route} 条"

    def _add_global_item(self, parent: QTreeWidgetItem, g: GlobalRecord) -> None:
        """在模式行下挂一个「全局录制」条目: 条目 → A/B 队 → 玩家 → 每辆车的线路。

        ⚠ **子项延后到展开时才建**(`_populate_global_item`): 一局就有 200 多条线,
        五百局是 10 万行 —— 启动时全建出来要一两秒、还要吃掉上百 MB。
        """
        mark = f"  [{g.mark_label}]" if g.mark_label else ""
        item = QTreeWidgetItem(["", f"全局 {g.label}{mark}", f"{g.count()} 条"])
        item.setData(0, _ROLE_KIND, "global")
        item.setData(0, _ROLE_ID, g.route_id)
        item.setData(0, _ROLE_KEY, f"global|{g.route_id}")
        item.setData(0, _ROLE_LAZY, g.route_id)      # 展开时才建子项
        prefix = f"{g.route_id}:"
        item.setIcon(_COL_EYE, eye_icon(
            any(lid.startswith(prefix) for lid in self._visible_lives)))
        item.setToolTip(_COL_EYE, "点击显示 / 隐藏整局的全部线路")
        tooltip = [
            f"全局录制 · {g.map_name} · {g.mode_label}",
            f"录制时间 {g.created_at.replace('T', ' ')}",
            f"用时 {fmt_time(g.duration)} · 共 {g.count()} 条线路",
            f"标记: {g.mark_label or '(无 — 点「A/B 队录制」会新建一个条目)'}",
        ]
        tooltip.append("右键可标记为 A/B 队录制目标或删除")
        item.setToolTip(_COL_NAME, "\n".join(tooltip))
        parent.addChild(item)
        if item.isExpanded():            # 恢复展开状态后可能已经是展开的
            self._populate_global_item(item)
        else:
            self._add_lazy_placeholder(item, g.count())

    def _add_lazy_placeholder(self, item: QTreeWidgetItem, n: int) -> None:
        """给还没展开的全局条目挂一个占位子项。

        为什么必须有它(Qt 的坑): **QTreeWidget 里没有子项的条目根本不画展开箭头** ——
        没有箭头就点不开, 也就永远等不到 `itemExpanded`, 于是“展开才建子项”变成了
        “永远展不开”(用户 2026-10-26 报的 bug)。有了占位行箭头就在, 一展开就换掉。
        """
        if n <= 0:
            return                        # 本来就是空局, 没必要给箭头
        ph = QTreeWidgetItem(["", f"（展开载入 {n} 条线路）", ""])
        ph.setFlags(Qt.ItemFlag.NoItemFlags)      # 不可选、不可点
        ph.setData(0, _ROLE_KIND, "lazy")
        item.addChild(ph)

    def _populate_global_item(self, item: QTreeWidgetItem) -> None:
        """把某个全局条目的子项(队 → 玩家 → 每辆车)建出来 —— **展开时才调**。"""
        gid = item.data(0, _ROLE_LAZY)
        if not gid:
            return
        item.setData(0, _ROLE_LAZY, None)       # 先标记, 免得重入
        while item.childCount():                # 去掉占位行(真正展开时才走到这里)
            item.takeChild(0)
        g = self.store.get_global(gid)
        if g is None:
            return

        def _add_life_item(parent_item: QTreeWidgetItem, life, side_key: str) -> None:
            """一辆车的线路行。"""
            # 已经在玩家分组下面了，没必要再把玩家名重复一遍 -> 只留型号
            title = life.label
            if getattr(life, "player", "") and getattr(life, "vehicle_cls", ""):
                title = f"{life.short_id}  {fmt_time(life.t0)}  {life.vehicle_cls}"
            litem = QTreeWidgetItem(["", title, fmt_time(life.duration)])
            litem.setData(0, _ROLE_KIND, "glife")
            litem.setData(0, _ROLE_ID, life.life_id)
            litem.setData(0, _ROLE_SIG, side_key)
            litem.setData(0, _ROLE_KEY, g.route_id)
            litem.setIcon(_COL_EYE, eye_icon(life.life_id in self._visible_lives))
            litem.setToolTip(_COL_EYE, "点击显示 / 隐藏这条线路")
            detail = f"线路 {life.short_id}（{side_key} 队）· {life.icon or life.obj_type}"
            if life.player:
                detail += f"\n玩家 {life.player}"
            if life.vehicle_cls:
                detail += f"\n型号 {life.vehicle_cls}"
            detail += (f"\n出现 {fmt_time(life.t0)} → 消失 {fmt_time(life.t1)}"
                       f"\n点数 {life.point_count} · 最高速 "
                       f"{fmt_speed(life.vmax)} km/h")
            detail += "\n（已阵亡）" if life.dead else "\n（到对局结束还在场上）"
            detail += f"\n色标 {life.color or '-'}(8111 原始颜色, 仅参考)"
            litem.setToolTip(_COL_NAME, detail)
            parent_item.addChild(litem)

        for side in GLOBAL_SIDES:
            lives = g.lives(side)
            # AI 组没内容就不显示(8111 录制 / 老的回放里永远为空)
            if not lives and side not in ("A", "B"):
                continue
            sitem = QTreeWidgetItem(["", GLOBAL_SIDE_LABELS[side], f"{len(lives)} 条"])
            sitem.setData(0, _ROLE_KIND, "gside")
            sitem.setData(0, _ROLE_ID, g.route_id)
            sitem.setData(0, _ROLE_SIG, side)
            sitem.setData(0, _ROLE_KEY, f"gside|{g.route_id}|{side}")
            sitem.setFlags(GROUP_ITEM_FLAGS)
            sitem.setIcon(_COL_EYE, eye_icon(any(l.life_id in self._visible_lives
                                                for l in lives)))
            sitem.setToolTip(_COL_EYE, "点击显示 / 隐藏这一组的全部线路")
            if side == "AI":
                sitem.setToolTip(_COL_NAME,
                                 "回放里**没有玩家身份**的单位: 会动的 AI 车 / 工事。\n"
                                 "它们既没有出生组名(玩家载具的身份来源), 也没有击杀记录,\n"
                                 "所以不能归到 A / B 任何一队。")
            else:
                sitem.setToolTip(_COL_NAME,
                                 "回放的 /map_obj.json 只给**观战那一方**的单位,\n"
                                 "所以要分两次录制: 一次观战 A 方, 一次观战 B 方。")
            item.addChild(sitem)
            # ---- 按**玩家**分组（回放导入才能拿到玩家名）--------------------------
            # 同一个玩家一局里会死很多次、开过很多台车; 以前全平铺在一层里,
            # 想跟一个人的完整轨迹得自己数。现在: 队 → 玩家 → 各台车,
            # 玩家按首次出现时间排序, 载具按出场时间正序。
            groups: dict[str, list] = {}
            for life in sorted(lives, key=lambda x: (x.t0, x.life_id)):
                groups.setdefault(getattr(life, "player", "") or "", []).append(life)
            for player in sorted(groups, key=lambda p: (not p, groups[p][0].t0)):
                glives = groups[player]
                if not player:                   # 8111 录制的没有玩家名 -> 照旧平铺
                    for life in glives:
                        _add_life_item(sitem, life, side)
                    continue
                total = sum(l.duration for l in glives)
                dead_n = sum(1 for l in glives if l.dead)
                # 一辆车可能因为「隔太久没上报 / 一步瞬移」被切成多段，各自是独立
                # 的线路（方便单独看/单独隐藏）。所以这里要分两种口径：
                #   * 台数 = 去掉 `_N` 段号后的去重数（不然一辆车数成三辆）
                #   * 存活 = 这些段的**区间并集**（直接求和会把重叠时间算两遍）
                seen_cars: set[str] = set()
                for l in glives:
                    sid = l.short_id
                    head, _, tail = sid.rpartition("_")
                    seen_cars.add(head if (head and tail.isdigit()) else sid)
                iv = sorted((l.t0, l.t1) for l in glives)
                total = 0.0
                if iv:
                    a0, b0 = iv[0]
                    for a, b in iv[1:]:
                        if a <= b0:
                            b0 = max(b0, b)
                        else:
                            total += b0 - a0
                            a0, b0 = a, b
                    total += b0 - a0
                pitem = QTreeWidgetItem([
                    "",
                    f"{player} · {len(seen_cars)} 台 · 存活 {fmt_time(total)}",
                    f"阵亡 {dead_n}"])
                pitem.setData(0, _ROLE_KIND, "gplayer")
                pitem.setData(0, _ROLE_ID, g.route_id)
                pitem.setData(0, _ROLE_SIG, side)
                pitem.setData(0, _ROLE_KEY, f"gplayer|{g.route_id}|{side}|{player}")
                pitem.setFlags(GROUP_ITEM_FLAGS)
                pitem.setIcon(_COL_EYE, eye_icon(any(
                    l.life_id in self._visible_lives for l in glives)))
                pitem.setToolTip(_COL_EYE, "点击显示 / 隐藏这位玩家的全部线路")
                pitem.setToolTip(_COL_NAME,
                                 f"{player}（{GLOBAL_SIDE_LABELS[side]}）\n"
                                 f"载具 {len(seen_cars)} 台（{len(glives)} 段轨迹）· "
                                 f"在场 {fmt_time(total)} · 阵亡 {dead_n} 段")
                sitem.addChild(pitem)
                for life in glives:
                    _add_life_item(pitem, life, side)

    def _reload_tree(self) -> None:
        # 重建前先记住哪些分组是折叠的: 「地图管理」关掉、录完一条轨迹、改名或删除
        # 之后都会走这里重建侧栏, 否则用户刚折起来的分组会被全部重新展开。
        folded = self._folded_keys()
        first_build = not getattr(self, "_tree_built", False)   # 刚启动的那一次
        self._implied_items = []        # 旧条目马上要被 tree.clear() 删掉
        self.tree.blockSignals(True)
        self.tree.clear()
        alive: set[str] = set()          # 仍存在于侧栏的轨迹 id(用于修剪显示集合)
        alive_lives: set[str] = set()    # 仍存在于侧栏的全局线路 id
        # 三层: 地图 → 模式(由战区数量推出) → 轨迹 / 全局条目
        # (用户 2026-10-26: 同一模式的大图/小图/不同战区都不再拆成"区域1/2")
        modes_all = self._tree_modes()
        for map_key, modes in modes_all:
            n_route = sum(len(r) for _m, r, _g in modes)
            n_glob = sum(len(g) for _m, _r, g in modes)
            top = QTreeWidgetItem(["", self.store.display_name(map_key),
                                   self._count_text(n_route, n_glob)])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            top.setData(0, _ROLE_KEY, f"map|{map_key}")
            top.setFlags(GROUP_ITEM_FLAGS)
            f = QFont()
            f.setBold(True)
            top.setFont(_COL_NAME, f)
            self.tree.addTopLevelItem(top)
            for mode_label, routes, globs in modes:
                mid = QTreeWidgetItem(["", mode_label,
                                       self._count_text(len(routes), len(globs))])
                mid.setData(0, _ROLE_KIND, "mode")
                mid.setData(0, _ROLE_ID, map_key)
                mid.setData(0, _ROLE_KEY, f"mode|{map_key}|{mode_label}")
                mid.setFlags(GROUP_ITEM_FLAGS)
                mid.setToolTip(_COL_NAME, self._mode_tip(map_key, mode_label))
                top.addChild(mid)
                for rec in routes:
                    child = QTreeWidgetItem(
                        ["", rec.label, fmt_time(rec.duration)])
                    child.setData(0, _ROLE_KIND, "route")
                    child.setData(0, _ROLE_ID, rec.route_id)
                    child.setIcon(_COL_EYE, eye_icon(rec.route_id in self._visible_ids))
                    child.setToolTip(_COL_EYE, "点击显示 / 隐藏该轨迹")
                    child.setToolTip(_COL_NAME,
                                     f"{rec.map_name} · {rec.mode_label} | "
                                     f"{rec.vehicle} | 用时 {fmt_time(rec.duration)}")
                    mid.addChild(child)
                    alive.add(rec.route_id)
                # 全局录制条目排在单条路线之后(用户要求: 加在这一级的末尾)
                for g in globs:
                    self._add_global_item(mid, g)
        # 已缓存的全局线路 id: 按 `<条目 id>:` 前缀剪掉已删条目的 ——
        # 不去枚举每条线路(那样会把所有会话的线路清单读进内存)
        alive_gids = {g.route_id for g in self.store.globals}
        alive_lives.update(lid for lid in self._visible_lives
                           if lid.rpartition(":")[0] in alive_gids)
        # 展开/折叠统一放在重建末尾恢复(见 _restore_folded)
        # 已缓存图片、但还没录过轨迹的地图也列出来(灰色, 用时列写“仅缓存”):
        # 否则在「地图管理」里给它们改完名字, 主界面根本看不到任何变化。
        # 顺便把它们**已经见过的模式**挂上(0 条) —— 还没录到轨迹时就看得见有哪些模式。
        listed = {key for key, _modes in modes_all}
        for map_key in self.store.ordered_map_keys():
            if map_key in listed or not self.map_cache.has(map_key):
                continue
            if self.store.is_map_hidden(map_key):
                continue                     # 勾了隐藏的不占侧栏
            top = QTreeWidgetItem(["", self.store.display_name(map_key), "仅缓存"])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            top.setData(0, _ROLE_KEY, f"map|{map_key}")
            top.setFlags(GROUP_ITEM_FLAGS)
            f = QFont()
            f.setBold(True)
            top.setFont(_COL_NAME, f)
            top.setForeground(_COL_NAME, QBrush(Qt.GlobalColor.gray))
            seen_modes: list[str] = []
            for layout in self.store.zone_layouts(map_key):
                mode = str(layout.get("mode") or "")
                if mode and mode not in seen_modes:
                    seen_modes.append(mode)
            top.setToolTip(_COL_NAME,
                           "这张地图只缓存了图片, 还没录过轨迹\n"
                           f"见过的模式: {'、'.join(seen_modes) or '(还没见过战区)'}\n"
                           "右键可重命名地图, 或菜单「地图 → 地图管理…」")
            self.tree.addTopLevelItem(top)
            for mode in seen_modes:
                var = QTreeWidgetItem(["", mode, "0 条"])
                var.setData(0, _ROLE_KIND, "mode")
                var.setData(0, _ROLE_ID, map_key)
                var.setData(0, _ROLE_KEY, f"mode|{map_key}|{mode}")
                var.setFlags(GROUP_ITEM_FLAGS)
                var.setForeground(_COL_NAME, QBrush(Qt.GlobalColor.gray))
                var.setToolTip(_COL_NAME, self._mode_tip(map_key, mode))
                top.addChild(var)
        self._restore_folded(folded, collapse_all=first_build)
        self._tree_built = True
        self.tree.blockSignals(False)
        self._visible_ids &= alive       # 被删除的记录自动从“显示中”移除
        self._visible_lives &= alive_lives
        # 侧栏内容变了(导入 / 删除 / 重命名) → 「▶ 回放」的可用性必须跟着变。
        # 用户 2026-10-26: 删掉 data 目录后重新导入, 「▶ 回放」一直是灰的 —— 因为
        # 启动时 store 是空的(`has_track=False`), 导入后没人再算一次。
        if hasattr(self, "btn_play") and hasattr(self, "recorder"):
            self._update_buttons()
        if self._anchor is not None and not any(
                (rec := self.store.get(rid)) is not None and self._group_of(rec) == self._anchor
                for rid in alive):
            self._anchor = None          # 基准那些轨迹被删光了 → 解除锁定
        self._refresh_anchor_icon()      # 图钉要跟着基准标回对应版本行
        self._refresh_map_tracks()
        self._on_tree_selection_changed()

    def _folded_keys(self) -> set[str]:
        """当前被**折叠**的分组标识(`_ROLE_KEY`); 用于侧栏重建后恢复展开状态。"""
        folded: set[str] = set()

        def walk(item: QTreeWidgetItem) -> None:
            key = item.data(0, _ROLE_KEY)
            if key and not item.isExpanded():
                folded.add(key)
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))
        return folded

    def _restore_folded(self, folded: set[str], collapse_all: bool = False) -> None:
        """恢复展开状态。

        * `collapse_all=True`(刚启动的那一次): **全部折叠** —— 打开软件先看到一排地图/线路,
          想看哪张再自己展开;
        * 之后的重建(关掉「地图管理」、录完一条、改名、删除): 用户折起来的保持折叠,
          用户展开过的保持展开; **新出现的**分组(新地图/新模式/新版本)默认展开。
        """
        def walk(item: QTreeWidgetItem) -> None:
            key = item.data(0, _ROLE_KEY)
            if key:
                expanded = not collapse_all and key not in folded
                item.setExpanded(expanded)
                # 重建时 tree 的信号是屏蔽的 → 展开事件不会触到, 这里手动补建子项
                if expanded and item.data(0, _ROLE_LAZY):
                    self._populate_global_item(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

    def _expand_item(self, node_id: str) -> None:
        """把某个条目(单条轨迹 / 全局录制条目)的各级分组全部展开。

        启动时分组是折叠的, 新录的这条会藏在里面 —— 不展开的话侧栏看起来几乎没变化。
        """
        found: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                ch = item.child(i)
                if (ch.data(0, _ROLE_ID) == node_id
                        and ch.data(0, _ROLE_KIND) in ("route", "global")):
                    found.append(ch)
                    return
                walk(ch)

        walk(self.tree.invisibleRootItem())
        node = found[0] if found else None
        while node is not None:
            if node.data(0, _ROLE_LAZY):
                self._populate_global_item(node)      # 先把子项建出来
            node.setExpanded(True)
            node = node.parent()

    def _on_tree_item_expanded(self, item: QTreeWidgetItem) -> None:
        """展开一个「全局录制」条目 → 这时才建它下面的队/玩家/线路行。"""
        if item is None or not item.data(0, _ROLE_LAZY):
            return
        self._populate_global_item(item)
        self._refresh_implied_marks()      # 新出现的行也要按父项的选中状态刷一遍

    def _expand_to_route(self, route_id: str) -> None:
        """兼容旧调用点: 展开到某条单条轨迹。"""
        self._expand_item(route_id)

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        """点击第一列的**眼睛图标** → 切换该条目在地图上的显示/隐藏。"""
        if col != _COL_EYE:
            return
        kind = item.data(0, _ROLE_KIND)
        if kind == "route":
            if self._in_playback():
                # 回放绘制优先于轨迹显示, 此时改眼睛没有意义(面板上也已禁用侧栏)
                self._set_status("回放中无法修改轨迹显示, 请先点「■ 结束播放」", sticky=2.0)
                return
            route_id = item.data(0, _ROLE_ID)
            self._set_visible(item, route_id not in self._visible_ids)
        elif kind in ("glife", "gside", "global", "gplayer"):
            if self._in_playback():
                self._set_status("回放中无法修改轨迹显示, 请先点「■ 结束播放」", sticky=2.0)
                return
            ids = self._life_ids_of(item)
            label = item.text(_COL_NAME)
            # 判断“这一层当前亮不亮”只算真实存在的线路(残留 id 会让点一下全灭)
            live_ids = [gid for gid in ids if self._life_by_id(gid) is not None]
            shown = not any(gid in self._visible_lives for gid in live_ids)
            self._set_lives_visible(ids, shown, what=f"「{label}」")

    def _set_lives_visible(self, ids: list[str], shown: bool, what: str = "") -> None:
        """统一控制“全局录制里的线路”在地图上的显示/隐藏。

        只点亮本条的线路时**不清掉别人**: 全局线路之间不冲突(它们同属一张地图,
        本来就是整局所有玩家的轨迹, 叠在一起才有意义)。

        **只认真正存在的线路**: `_visible_lives` 里万一残留了已重录/删除的 id,
        会让“点一下眼睛”的判断(any in ...)全错 —— 所以先过滤。
        """
        existing = {gid for gid in ids if self._life_by_id(gid) is not None}
        if not existing:
            return
        if shown:
            self._visible_lives.update(existing)
        else:
            self._visible_lives -= existing
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        if shown:
            # 点在缓存底图上看不到线路时, 自动切到这条全局录制所属的地图
            for g in self.store.globals:
                if any(life.life_id in self._visible_lives for side in GLOBAL_SIDES
                       for life in g.lives(side)):
                    self._ensure_map_for([g])
                    break
        tail = f" {what}" if what else ""
        self._set_status(
            f"{'已显示' if shown else '已隐藏'}{tail} {len(existing)} 条全局线路"
            f"(当前共 {len(self._visible_lives)} 条)", sticky=2.5)


    def _visible_route_ids(self) -> list[str]:
        """按侧栏顺序列出眼睛点亮的轨迹 id。"""
        return [it.data(0, _ROLE_ID) for it in self._iter_route_items()
                if it.data(0, _ROLE_ID) in self._visible_ids]

    def _merge_duplicate_maps(self) -> list[tuple[str, str]]:
        """把“同一张地图被发成不同分辨率”的重复条目合并成一张地图。

        8111 同一张地图会以 1024²/2048²/4096² 不同分辨率发出(实测), 老版本按字节
        哈希当地图身份 → 同一张图被当成两张(2026-10-09 用户反馈)。
        `MapCache.merge_duplicates` 会按“缩到 64×64 灰度 + 结构哈希”认出来,
        并把轨迹/名字/战区布局/校准并到保留的那个键上(会先备份 routes.json)。

        ⚠ 判定比运行时写盘那套(`similar_key`)**更严**(用户 2026-10-26 要求):
        双向 + 与组内**所有**成员都比 + 带最近邻余量 —— 因为这里会删掉多余的图片
        文件、还会改 `routes.json`, 宁可少合也不能错合。
        """
        try:
            merged = self.map_cache.merge_duplicates(self.store)
        except Exception:  # noqa: BLE001 - 合并失败不应该影响启动
            log.exception("合并重复地图失败")
            return []
        if merged:
            log.info("启动时合并了 %d 个重复地图条目", len(merged))
        return merged

    def _refresh_map_tracks(self) -> None:
        """把“眼睛点亮的轨迹”交给地图显示, 并把**选中的那几条**标为高亮。

        两层绘制:
        * 普通层 = `_visible_ids`(眼睛点亮) → 不透明灰色;
        * **高亮层 = 侧栏当前选中的那几条里、眼睛也点亮了的** → 速度配色, 可多条;
          没选中任何条目时才退回“刚录制的那条”。

        高亮层**不会**把眼睛没点亮的轨迹画出来: 点眼睛时该行会先被选中,
        若高亮层又把它画回去, 就会看起来像“点了眼睛没反应”(2026-10-07 修的)。
        """
        # 重录/删除后可能残留已经不存在的线路 id → 先剪掉（否则眼睛与显示会错位）
        alive_lives = {life.life_id for g in self.store.globals
                       for side in GLOBAL_SIDES for life in g.lives(side)}
        if not self._visible_lives <= alive_lives:
            self._visible_lives &= alive_lives
            self._refresh_eye_icons()
        recs = [r for rid in self._visible_route_ids()
                if (r := self.store.get(rid)) is not None]
        # 眼睛点亮的全局线路也当成普通轨迹画(灰色层)
        recs += self._visible_life_objects()
        picked = self._selection_playables()
        if picked:
            highlight = [r for r in picked if self._is_shown(r)]
        else:
            # 没选中时退回“刚录制的那条”(只在它还显示着的时候高亮)
            hl = self._last_record
            highlight = [hl] if (hl is not None and hl.route_id in self._visible_ids) else []
        recorder = getattr(self, "recorder", None)   # 构造早期还没有 recorder
        if recorder is not None and recorder.recording:
            # 录制中 live 是正在录的那条, 不能被选中/高亮覆盖
            self.map_view.set_visible_tracks(recs)
            self.map_view.set_highlight([])
            return
        self.map_view.set_visible_tracks(recs)
        self.map_view.set_highlight(highlight)
        self.map_view.set_live(None)
        self._apply_zones()

    def _is_shown(self, rec) -> bool:
        """这条轨迹/线路当前是不是“眼睛点亮”状态。"""
        rid = getattr(rec, "route_id", "")
        return rid in self._visible_ids or rid in self._visible_lives

    def _visible_life_objects(self) -> list:
        """眼睛点亮的全局线路(按 `_visible_lives` 里的 id 取出来)。"""
        if not self._visible_lives:
            return []
        out: list = []
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                for life in g.lives(side):
                    if life.life_id in self._visible_lives:
                        out.append(life)
        return out


    def _life_owner(self, life) -> GlobalRecord | None:
        """全局线路(`UnitLife`)所属的全局条目(找不到返回 None)。"""
        lid = getattr(life, "life_id", "")
        if not lid:
            return None
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                if any(lf.life_id == lid for lf in g.lives(side)):
                    return g
        return None

    def _zone_signature_for_view(self) -> str:
        """当前视图(回放中的 / 眼睛点亮的轨迹)对应的模式指纹(取不到就空串)。

        多条轨迹时只在它们指纹一致时才返回 —— 否则无法决定画哪套战区。

        2026-10-22 修: 全局线路(`UnitLife`)**自己没有** `zone_signature`,
        以前这里直接把它当空串跳过 → `layout_for(key, "")` 退化成 `layouts[0]`,
        于是回放 AB 队录制的线路时画的是**单条路线录制时**那套战区位置。
        现在对全局线路反查它所属的 `GlobalRecord.zone_signature`。
        """
        recs = self.map_view.playback_records or self.map_view.visible_tracks
        sigs = {s for r in recs if (s := getattr(r, "zone_signature", ""))}
        if len(sigs) == 1:
            return sigs.pop()
        if sigs:
            return ""                    # 多条轨迹模式不一致 → 无法决定
        gsigs: set[str] = set()          # 全是全局线路 → 问它们所属的条目
        for r in recs:
            owner = self._life_owner(r)
            if owner is not None and owner.zone_signature:
                gsigs.add(owner.zone_signature)
        return gsigs.pop() if len(gsigs) == 1 else ""

    def _playback_global(self) -> GlobalRecord | None:
        """回放中若含全局线路, 返回它们所属的全局条目(用于取战区时间轴)。"""
        for r in self.map_view.playback_records:
            owner = self._life_owner(r)
            if owner is not None:
                return owner
        return None

    def _zone_state_side(self) -> str:
        """回放时按哪一次观战(A/B)的战区快照取色。

        播放的线路都来自同一队 → 就用那一队; A/B 混播时取**条数多的那一队**
        (平手取 A)。反正取到之后都会按“两队各自的重生点颜色”重映射一次,
        所以选谁只影响用哪一份快照, 不影响最终配色。
        """
        counts: dict[str, int] = {}
        for rec in self.map_view.playback_records:
            side = str(getattr(rec, "side", "") or "")
            if side in ("A", "B"):
                counts[side] = counts.get(side, 0) + 1
        if not counts:
            return ""
        return max(sorted(counts), key=lambda s: counts[s])

    def _remap_zone_state(self, snap: list[dict], session_side: str) -> list[dict]:
        """把战区快照的颜色从“观战者视角”换到“两队各自重生点的颜色”。

        8111 的颜色里 `#174DFF` 永远是**摄像机那一队**。A、B 是两次观战、视角相反,
        所以同一条状态在两次录制里的 `ours` 指的是不同队伍。这里按
        “`ours` → 该次观战那一队在地图上的重生点颜色 / `enemy` → 另一队的”重算混色
        (进度比例不变), 于是回放时两支队伍的颜色始终固定(2026-10-23 用户要求)。
        算不出队伍颜色(没有线路可投票)时原样返回。
        """
        if not session_side:
            return list(snap)
        other = "B" if session_side == "A" else "A"
        own = self.map_view._side_spawn_color(session_side)
        opp = self.map_view._side_spawn_color(other)
        if own is None or opp is None:
            return list(snap)
        palette = {
            SIDE_NEUTRAL: CAPTURE_ANCHORS[SIDE_NEUTRAL],
            SIDE_OURS: (own.red(), own.green(), own.blue()),
            SIDE_ENEMY: (opp.red(), opp.green(), opp.blue()),
        }
        out: list[dict] = []
        for z in snap:
            row = dict(z)
            new = remap_capture_color(row.get("color"), palette)
            if new:
                row["color"] = new
            out.append(row)
        return out

    def _apply_airfields(self) -> None:
        """决定地图上画哪一局的机场跑道。

        机场数据只有**从回放导入**的条目才有(见 `replay_import._airfields_from_ecs`),
        8111 实时录制的没有。取舍顺序:

        1. 正在回放某一局 → 就画那一局(和战区时间轴、线路保持一致);
        2. 否则 → 画**当前这张图**最近导入的那一局(有机场数据的那几局里最新的)。

        坐标要用**该条目自己的 `norm_rect`** 归一化 —— 线路的 `points` 就是用它算的,
        用别的矩形会让跑道和线路错位。
        """
        g = self._playback_global() if self._in_playback() else None
        if g is None or not g.airfields:
            cur = self.store.canon(self.map_view.map_key or "")
            cands = [x for x in self.store.globals
                     if x.airfields
                     and self.store.canon(x.map_key or "") == cur]
            cands.sort(key=lambda x: x.created_at or "")
            g = cands[-1] if cands else None
        if g is None or not g.airfields:
            self.map_view.set_airfields(None, None)
            return
        self.map_view.set_airfields(g.airfields, g.norm_rect)

    def _apply_zones(self, info: dict | None = None) -> None:
        """决定地图上画哪套战区(小菱形 + A/B/C)。
        1. **回放全局录制** → 用录像里记的战区状态时间轴(归属变化、占领中都能重现);
           这是 2026-10-22 用户要求的: 回放 AB 队线路时不该显示当前战局/别的录像的战区;
        2. 战局内且 8111 正在给同一张图 → 用**实时**战区;
        3. 其它(游戏关了/在看缓存底图) → 用该地图存过的布局:
           优先匹配“正在看的轨迹的模式指纹”, 没有就取最近一次见过的。
        """
        key = self.map_view.map_key
        if not key:
            return
        # 机场跑道只跟“这是哪一局”有关, 与战区分支无关 —— 先独立更新掉
        self._apply_airfields()
        recorder = getattr(self, "recorder", None)
        live = list((info or {}).get("zones") or [])
        live_spawns = list((info or {}).get("spawns") or [])
        layout = self.store.layout_for(key, self._zone_signature_for_view())
        # 1) 回放全局录制: 按 playhead 取当时的战区快照
        if self._in_playback():
            g = self._playback_global()
            entry = g.zone_state_entry(self.player.time, self._zone_state_side()) \
                if g is not None else None
            if entry:
                zones = entry.get("zones") or []
                # 重映射用**这条快照自己的 side**(哪一次观战录的), 不是播放的那一队:
                # 旧数据没有 side → 不动颜色(那时也分不出两次观战)
                self.map_view.set_zones(self._remap_zone_state(
                    zones, str(entry.get("side") or "")))
                self.map_view.set_spawns(spawns_to_json(ground_spawns(
                    spawns_from_json((layout or {}).get("spawns")))))
                return
        if live and recorder is not None and recorder.map_key == key:
            self.map_view.set_zones(zones_to_json(live))
            # 只画**地面**重生位置(空军的 fighter/bomber/ucav 也记了, 但地图上不画)
            self.map_view.set_spawns(spawns_to_json(ground_spawns(live_spawns)))
            return
        # 旧布局: 位置 + 当时存下的归属色(用户 2026-10-22 要求把归属显示回来;
        # `blink` 不存也不画 —— 那只是那一刻"正在被占领"的瞬时状态)
        self.map_view.set_zones([
            {"label": z.get("label"), "x": z.get("x"), "y": z.get("y"),
             "color": z.get("color"), "side": z.get("side")}
            for z in (layout or {}).get("zones") or []
        ])
        # 重生位置: 存过就画(同一张图不同版本存的是各自的那份)
        self.map_view.set_spawns(spawns_to_json(ground_spawns(
            spawns_from_json((layout or {}).get("spawns")))))

    def _selection_tracks(self) -> list[RouteRecord]:
        """侧栏中当前选中的线路(可 Shift/Ctrl 多选), 按侧栏顺序。"""
        out: list[RouteRecord] = []
        for it in self._iter_route_items():
            if not self._is_item_selected(it):
                continue
            rec = self.store.get(it.data(0, _ROLE_ID))
            if rec is not None:
                out.append(rec)
        return out

    def _select_route_ids(self, ids) -> None:
        """在侧栏中选中指定线路(不处理信号, 由调用方决定后续动作)。"""
        want = set(ids)
        for it in self._iter_route_items():
            it.setSelected(it.data(0, _ROLE_ID) in want)

    def _life_by_id(self, life_id: str):
        """按 life_id 找全局线路(找不到返回 None)。"""
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                for life in g.lives(side):
                    if life.life_id == life_id:
                        return life
        return None

    def _implied_by_ancestor(self, item: QTreeWidgetItem) -> bool:
        """该条目**自己没被选中**, 但祖先(全局条目 / A·B 队行)在选中范围里。

        `_selection_lives()` 与 `_refresh_implied_marks()` 共用这一条判据 ——
        “会被算进待回放”与“要画成选中”必须是同一套逻辑, 否则界面又会骗人。
        """
        node: QTreeWidgetItem | None = item.parent()
        while node is not None:
            if (self._is_item_selected(node)
                    and node.data(0, _ROLE_KIND) in ("glife", "gside", "global")):
                return True
            node = node.parent()
        return False

    def _refresh_implied_marks(self) -> None:
        """把“被父项带进选中范围”的条目也画成选中高亮(用户 2026-10-26)。

        **问题**: Shift 框选很容易把「全局 10-26 13:07」整行框进来 —— 于是回放播的
        是整局 100 条, 而用户以为只选了 4 条。这本身不是 bug(选中全局条目就该播整局),
        真正的问题是 Qt 只高亮**真正在 selectionModel 里**的条目: 父项被选中时,
        下面几十行“也会被回放”的线路一点变化都没有。

        **做法**: 这些行手动刷 `palette().highlight()` 背景 + `highlightedText` 前景,
        与真选中视觉上一致; 不再是“被动选中”时把这两个 role 清掉(还原默认)。
        用 `_implied_items` 记住上一批, 只清那一批 —— 不必每次重刷整棵树。
        """
        for it in self._implied_items:          # 先还原上一批
            try:
                for col in (_COL_NAME, _COL_INFO):
                    it.setData(col, Qt.ItemDataRole.BackgroundRole, None)
                    it.setData(col, Qt.ItemDataRole.ForegroundRole, None)
            except RuntimeError:
                pass        # 树已经重建/条目被删 → 旧的 C++ 对象不存在了
        self._implied_items = []
        pal = self.palette()
        bg, fg = QBrush(pal.highlight()), QBrush(pal.highlightedText())
        for it in self._iter_all_items():
            if it.data(0, _ROLE_KIND) not in ("global", "gside", "glife"):
                continue
            if self._is_item_selected(it) or not self._implied_by_ancestor(it):
                continue        # 自己选中的本来就有高亮; 没有被祖先带进来的不用管
            for col in (_COL_NAME, _COL_INFO):
                it.setData(col, Qt.ItemDataRole.BackgroundRole, bg)
                it.setData(col, Qt.ItemDataRole.ForegroundRole, fg)
            self._implied_items.append(it)

    def _selection_lives(self) -> list:
        """侧栏中选中的**全局线路**(按侧栏顺序)。

        选中整个全局条目 / A·B 队行时, 把它们下面的全部线路都算进来;
        只选中几条线路时就只算那几条 —— 与用户要的“选了就播选中的”一致。
        按**树的顺序**取(不能用 `selectedItems()` 的顺序, 那是选择模型的顺序)。

        选中与否以 `selectionModel` 为准(见 `_is_item_selected` 的说明): 只看
        `QTreeWidgetItem.isSelected()` 会把“父项折叠时只有内部标志”的条目也算进来,
        于是明明取消掉的其它地图的线路还在选中集合里。
        """
        def hit(item: QTreeWidgetItem) -> bool:
            return self._is_item_selected(item) or self._implied_by_ancestor(item)

        out: list = []
        for it in self._iter_life_items():
            if not hit(it):
                continue
            life = self._life_by_id(it.data(0, _ROLE_ID))
            if life is not None:
                out.append(life)
        return out

    def _iter_all_items(self):
        """递归遍历侧栏里的**全部**条目(不限可见性)。"""
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                yield ch
                yield from walk(ch)

        yield from walk(self.tree.invisibleRootItem())

    def _is_item_selected(self, item: QTreeWidgetItem) -> bool:
        """该条目此刻是否真的处于选中状态(index 级判断)。

        不能用 `QTreeWidgetItem.isSelected()`: 父项折叠时它可能只是 item 自身的
        标志位, 并不在 selectionModel 里(实测同一行 isSelected()=True 但
        `selectedIndexes()` 里没有它) —— 那样子项会被误认为“选中”。
        也不能用 `_ROLE_KEY` 建集合: 同一个全局条目下的所有线路条目共用同一个 KEY。
        """
        model = self.tree.selectionModel()
        if model is None:
            return False
        idx = self.tree.indexFromItem(item, 0)
        return idx.isValid() and model.isSelected(idx)

    def _selection_playables(self) -> list:
        """回放/高亮用: 选中的单条轨迹 + 选中的全局线路(侧栏顺序)。"""
        return self._selection_tracks() + self._selection_lives()

    def _playable_group(self, r) -> tuple[str, str]:
        """一条**可播放线路**属于哪个分组 (地图键, 模式名) —— 与 `_group_of` 同义。

        单条轨迹自带 `map_key` / `zone_count`, 直接走 `_group_of`。
        全局线路(`UnitLife`)只有 map_key, 它的模式在**所属全局录制**上。

        ⚠ 2026-10-26 修: 这里曾经返回 `(g.map_key, g.zone_signature)` —— 那是四层树
        时代的“版本(战区指纹)”语义, 而单条轨迹那边早就改成了 `(canon(key), 模式名)`。
        两边格式不同 ⇒ **同一张图同一模式的「单条轨迹 + 全局线路」混选时永远判成跨版本**,
        一半选中会被默默取消, 图钉也永远标不到模式行上。
        """
        if isinstance(r, RouteRecord):
            return self._group_of(r)
        lid = getattr(r, "life_id", "")
        if lid:
            for g in self.store.globals:
                if any(l.life_id == lid for side in GLOBAL_SIDES for l in g.lives(side)):
                    return (self.store.canon(g.map_key or ""), g.mode_label or "")
        return (self.store.canon(getattr(r, "map_key", "") or ""), "")

    def _item_groups(self, item: QTreeWidgetItem) -> set[tuple[str, str]]:
        """某个侧栏条目(任意层级)涉及哪些分组 —— 取消越界选中时按它判定。"""
        groups: set[tuple[str, str]] = set()
        for lid in self._life_ids_of(item):
            life = self._life_by_id(lid)
            if life is not None:
                groups.add(self._playable_group(life))
        if not groups:
            rec = self.store.get(item.data(0, _ROLE_ID))
            if rec is not None:
                groups.add(self._playable_group(rec))
        return groups

    def _on_tree_selection_changed(self) -> None:
        """选中项变化 → 回放将播放这几条; 同时**限制为同一张地图的同一模式**。

        跨地图(底图只能是一张)/跨模式(战区数量不同, 线路本来就不可比)一起回放没有意义,
        所以选中了多个分组时只保留一个:

        * 双击定过“基准”时, **基准优先** —— Ctrl 点到别处不会把基准换掉(锁住);
        * 没定基准时保留“本次新加进来的那个分组”。

        基准(图钉)还会**跟随当前选中所在的分组** —— 刚开始没选中任何条目时,
        第一个点到的线路就把它的“地图·模式”定为基准。

        判定用 `_selection_playables()`(单条轨迹 + 全局线路)而不是只看单条轨迹:
        以前用 `_selection_tracks()`, 而它只认 `kind == "route"` 的条目 —— 选中
        全局条目 / A·B 队行 / 全局线路时列表是空的, 跨地图限制完全不生效
        (用户 2026-10-22: 选中 AB 队所有路线后还能 Ctrl 点别的地图的路线)。
        """
        if self._reselecting or self._in_playback():
            return
        playables = self._selection_playables()
        if not playables:
            self._sel_ids = set()
            self._sel_groups = set()
            self._refresh_implied_marks()   # 清空选中时把“被动选中”的底色也清掉
            self._update_play_label()
            self._refresh_map_tracks()   # 选中全局线路时也要把高亮刷出来
            return                       # 清空选中时保留基准(图钉不消失)
        groups = {self._playable_group(r) for r in playables}
        if len(groups) > 1:
            keep: tuple[str, str] | None = None
            if self._anchor is not None and self._anchor in groups:
                keep = self._anchor
            else:
                new_groups = groups - self._sel_groups
                keep = next(iter(new_groups), None) or self._playable_group(playables[-1])
            self._reselecting = True
            try:
                self.tree.blockSignals(True)
                # 按**条目**取消选中(它可能是全局条目/队伍行, 没法用 route_id 选回去)。
                # 两套都清: selectionModel(真正生效的选中) + item 自身标志位 ——
                # 折叠组里的条目可能只改了自身标志, 不在 selectionModel 里
                # (`selectedItems()` 还会过滤隐藏项, 所以也不能用它)。
                model = self.tree.selectionModel()
                seen: set[int] = set()
                for idx in list(model.selectedIndexes()):
                    it = self.tree.itemFromIndex(idx)
                    if it is None or id(it) in seen:
                        continue
                    seen.add(id(it))
                    if self._item_groups(it) - {keep}:
                        it.setSelected(False)
                for it in self._iter_all_items():
                    if it.isSelected() and self._item_groups(it) - {keep}:
                        it.setSelected(False)
                self.tree.blockSignals(False)
            finally:
                self._reselecting = False
            playables = [r for r in playables if self._playable_group(r) == keep]
            groups = {keep}
            self._set_status(
                "同一张地图的同一个模式才能一起回放/显示, 已只保留一个分组内的选中条目",
                sticky=3.0)
        self._sel_groups = groups
        self._sel_ids = {getattr(r, "route_id", "") for r in playables}
        grp = self._playable_group(playables[0])
        if grp != self._anchor:
            self._anchor = grp          # 基准跟着当前选中走
            self._refresh_anchor_icon()
        self._refresh_implied_marks()   # 父项选中 → 子行也要看得出“会被回放”
        self._update_play_label()       # 所以先算标记, 再算“其中整局占几条”
        self._refresh_map_tracks()      # 高亮层 = 选中的那几条 → 选中变了要重画

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """双击轨迹行 → 定基准 + **把它显示到地图上并切到对应地图图片**。

        双击地图/模式/版本行不做处理(保留 Qt 默认的展开/折叠)。
        """
        if item.data(0, _ROLE_KIND) != "route":
            if item.data(0, _ROLE_KIND) in ("global", "gside", "glife"):
                # 双击全局条目/队伍/线路 = 显示它并切到对应地图(不能回放, 只能看)
                if self._in_playback():
                    self._set_status("回放中无法切换显示, 请先点「■ 结束播放」", sticky=2.0)
                    return
                self._set_lives_visible(self._life_ids_of(item), True)
            return
        if self._in_playback():
            self._set_status("回放中无法切换基准, 请先点「■ 结束播放」", sticky=2.0)
            return
        rec = self.store.get(item.data(0, _ROLE_ID))
        if rec is None:
            return
        self._anchor = self._group_of(rec)
        self._reselecting = True
        try:
            self.tree.blockSignals(True)
            self._select_route_ids([rec.route_id])     # 基准只选中这一条
            self.tree.blockSignals(False)
        finally:
            self._reselecting = False
        self._sel_ids = {rec.route_id}
        self._refresh_implied_marks()   # 双击把选中收缩到一条 → 旧的全选高亮要清掉
        self._refresh_anchor_icon()
        self._update_play_label()
        # 双击同时就把它显示出来(自动隐掉其它版本), 并把底图切到它的地图
        self._set_visible(item, True)
        self._set_status(
            f"基准已锁定: {self._group_name(*self._anchor)} · {rec.label}; "
            f"已切到该地图并显示这条轨迹", sticky=4.0)

    # --------------------------------------------------- 分组(地图 + 模式)
    def _group_of(self, rec: RouteRecord) -> tuple[str, str]:
        """一条轨迹属于哪个分组: (地图键, 模式名) —— 模式由战区数量推出。

        不再带战区指纹: 同一模式的“大地图/小地图/不同战区”都算同一组
        (用户 2026-10-26: 只按战区数量简单分类即可)。
        """
        return (self.store.canon(getattr(rec, "map_key", "") or ""),
                getattr(rec, "mode_label", "") or "")

    def _group_name(self, map_key: str, mode_label: str) -> str:
        """分组的可读名字(地图 · 模式)。"""
        parts = [self.store.display_name(map_key), mode_label]
        return " · ".join(p for p in parts if p)

    def _refresh_anchor_icon(self) -> None:
        """把图钉图标标到“当前基准模式”那一行(其它模式行清掉图标)。"""
        for i in range(self.tree.topLevelItemCount()):
            self._refresh_anchor_icon_in(self.tree.topLevelItem(i))

    def _refresh_anchor_icon_in(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.data(0, _ROLE_KIND) == "mode":
                grp = (ch.data(0, _ROLE_ID), ch.text(_COL_NAME))
                ch.setIcon(_COL_EYE, pin_icon() if grp == self._anchor else QIcon())
            else:
                self._refresh_anchor_icon_in(ch)

    def _update_play_label(self) -> None:
        """信息区“回放”那一栏: 显示基准分组 + 待回放条数(含选中的全局线路)。

        条数后面会补一句「(含整局 N 条)」—— N = 被**全局条目带进来**的线路数。
        为什么要它(用户 2026-10-26): 全局条目行**折叠着**的时候, 它下面那些被动
        选中的高亮根本看不见, 「待回放 100 条」就成了唯一线索; 但只给个总数仍然
        看不出“我明明只点了 4 条”。分开写一下, 一眼就知道是怎么回事。
        """
        n = len(self._selection_playables())
        n_implied = sum(1 for it in self._implied_items
                        if it.data(0, _ROLE_KIND) == "glife")
        if not n_implied:
            extra = ""
        elif n_implied >= n:            # 全是整局带进来的(用户一条都没单独点)
            extra = f"（整局 {n_implied} 条）"
        else:
            extra = f"（其中整局 {n_implied} 条）"
        if self._anchor is not None:
            base = self._group_name(*self._anchor)
            self.lbl_play.setText(f"基准 {base} | 待回放 {n} 条{extra}" if n
                                  else f"基准 {base} | 未选轨迹")
        else:
            self.lbl_play.setText(f"待回放: {n} 条{extra}" if n else "回放: --")

    def _show_all_tracks(self, shown: bool) -> None:
        """一键显示/隐藏轨迹(**只限同一地图同一模式** —— 不同模式的线路不可比)。

        从树的第一个分组开始, 只点亮该分组内的轨迹(树的顺序是最新在前,
        所以实际是“最新那个模式”); 想换分组就双击其它模式的轨迹改基准。
        """
        rows = list(self._iter_route_items())
        groups: list[tuple[str, str]] = []
        for ch in rows:
            rec = self.store.get(ch.data(0, _ROLE_ID))
            if rec is None:
                continue
            grp = self._group_of(rec)
            if grp not in groups:
                groups.append(grp)
        keep = self._anchor if (self._anchor in groups) else (groups[0] if groups else None)
        if shown:
            keep_ids = {ch.data(0, _ROLE_ID) for ch in rows
                        if keep is not None
                        and (rec := self.store.get(ch.data(0, _ROLE_ID))) is not None
                        and self._group_of(rec) == keep}
            self._visible_ids = keep_ids       # 只显示这一个分组(其它分组一并隐掉)
            self._set_status(f"已显示 {len(keep_ids)} 条轨迹(只限同一模式)", sticky=2.0)
        else:
            self._visible_ids.clear()
            self._set_status("已隐藏全部轨迹", sticky=2.0)
        self._refresh_eye_icons()
        self._refresh_map_tracks()

    def _on_tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, _ROLE_KIND)
        menu = QMenu(self)
        if kind in ("global", "gside", "glife"):
            self._fill_global_menu(menu, item, kind)
        elif kind == "route":
            act_show = QAction("在地图上显示", self)
            act_show.triggered.connect(lambda: self._show_on_map(item))
            menu.addAction(act_show)
            act_hide = QAction("从地图上隐藏", self)
            act_hide.triggered.connect(lambda: self._hide_from_map(item))
            menu.addAction(act_hide)
            menu.addSeparator()
            act_del = QAction("删除该记录", self)
            act_del.triggered.connect(lambda: self._delete_route(item))
            menu.addAction(act_del)
        else:
            if kind == "mode":     # 模式(战区控制 / 战区攻陷 / 战区争夺)
                act_show_var = QAction("显示本模式全部轨迹", self)
                act_show_var.triggered.connect(
                    lambda: self._show_tracks_of(item, True))
                menu.addAction(act_show_var)
                act_hide_var = QAction("隐藏本模式全部轨迹", self)
                act_hide_var.triggered.connect(
                    lambda: self._show_tracks_of(item, False))
                menu.addAction(act_hide_var)
                menu.addSeparator()
            else:
                act_rename = QAction("重命名地图", self)
                act_rename.triggered.connect(lambda: self._rename_map(item.data(0, _ROLE_ID)))
                menu.addAction(act_rename)
                menu.addSeparator()
                act_show_all = QAction("显示本图全部轨迹", self)
                act_show_all.triggered.connect(lambda: self._show_all_tracks(True))
                menu.addAction(act_show_all)
                act_hide_all = QAction("隐藏全部轨迹", self)
                act_hide_all.triggered.connect(lambda: self._show_all_tracks(False))
                menu.addAction(act_hide_all)
                menu.addSeparator()
            act_expand = QAction("展开/折叠", self)
            act_expand.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
            menu.addAction(act_expand)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # --------------------------------------------------- 全局录制的右键菜单
    def _fill_global_menu(self, menu: QMenu, item: QTreeWidgetItem, kind: str) -> None:
        """全局条目 / A·B 队行 / 单条线路的右键菜单。

        标记只在“全局条目”这一层打: 它表示**下一次点「A/B 队录制」录到哪一条**上。
        没有任何条目标记时, 点录制按钮会新建一个全局条目。
        """
        ids = self._life_ids_of(item)
        act_show = QAction("在地图上显示", self)
        act_show.triggered.connect(lambda: self._set_lives_visible(ids, True))
        menu.addAction(act_show)
        act_hide = QAction("从地图上隐藏", self)
        act_hide.triggered.connect(lambda: self._set_lives_visible(ids, False))
        menu.addAction(act_hide)
        if kind != "glife":
            gid = item.data(0, _ROLE_ID)
            g = self.store.get_global(gid)
            menu.addSeparator()
            for side in GLOBAL_SIDES:
                # 只有 A / B 能当录制目标 —— AI 组是回放导入算出来的, 8111 录制
                # 没有这个概念(见 `GLOBAL_SIDES` 的说明)。
                if side not in ("A", "B"):
                    continue
                act = QAction(f"标记为 {GLOBAL_SIDE_LABELS[side]}录制目标", self)
                act.setCheckable(True)
                act.setChecked(g is not None and side in g.marks)
                act.setToolTip(
                    f"打上标记后, 点「{side} 队录制」会录到这一条(而不是新建一条)。\n"
                    f"同一队只会有一条记录带标记 —— 打到别处时旧的会自动清掉。")
                act.toggled.connect(
                    lambda checked, gid=gid, s=side: self._toggle_mark(gid, s, checked))
                menu.addAction(act)
            act_clear = QAction("清除标记", self)
            act_clear.setEnabled(g is not None and bool(g.marks))
            act_clear.setToolTip("清除后, 点「A/B 队录制」会新建一个全局录制条目")
            act_clear.triggered.connect(lambda: self._clear_mark(item.data(0, _ROLE_ID)))
            menu.addAction(act_clear)
        menu.addSeparator()
        act_expand = QAction("展开/折叠", self)
        act_expand.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.addAction(act_expand)
        if kind == "global":
            menu.addSeparator()
            act_del = QAction("删除该全局录制…", self)
            act_del.triggered.connect(
                lambda: self._delete_global(item.data(0, _ROLE_ID)))
            menu.addAction(act_del)

    def _toggle_mark(self, gid: str, side: str, on: bool) -> None:
        """打/清“下次录到这一条”的标记。"""
        g = self.store.get_global(gid)
        if g is None:
            return
        self.store.set_mark(gid, side, on)
        self._reload_tree()
        self._set_status(
            f"已把「{g.label}」标记为 {side} 队录制目标"
            f"(点「{side} 队录制」就录到这一条)" if on
            else f"已取消「{g.label}」的 {side} 队标记", sticky=4.0)

    def _clear_mark(self, gid: str) -> None:
        g = self.store.get_global(gid)
        if g is None:
            return
        self.store.clear_marks(gid)
        self._reload_tree()
        self._set_status(
            f"已清除「{g.label}」的标记 —— 下次点「A/B 队录制」会新建一个全局条目",
            sticky=4.0)

    def _delete_global(self, gid: str) -> None:
        g = self.store.get_global(gid)
        if g is None:
            return
        ok = QMessageBox.question(
            self, "删除全局录制",
            f"确定删除这条全局录制吗?\n\n"
            f"{g.label} · {g.map_name} · {g.mode_label}\n"
            f"A 队 {len(g.lives('A'))} 条 / B 队 {len(g.lives('B'))} 条线路\n\n"
            f"删除后无法恢复(单条线路录制的轨迹不受影响)。")
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_global(gid)
        if self._last_global is not None and self._last_global.route_id == gid:
            self._last_global = None
        self._reload_tree()
        self._set_status("已删除全局录制条目", sticky=3.0)

    def _mode_tip(self, map_key: str, mode_label: str) -> str:
        """模式行的提示: 这个模式见过的作战范围(大图/小图)与战区位置。

        用户 2026-10-26 定的结构: 一张地图就 3 个模式, 大图/小图、从哪个战区抽的
        都只是**同一个模式的不同作战范围**, 所以它们列在提示里而不占树上一行。
        """
        layouts = [x for x in self.store.zone_layouts(map_key)
                   if str(x.get("mode") or "") == mode_label]
        lines = [
            f"{self.store.display_name(map_key)} · {mode_label}",
            "模式由 8111 的战区(capture_zone)数量推出: "
            "3 个=战区控制, 2 个=战区攻陷, 1 个=战区争夺",
        ]
        if not layouts:
            lines.append("旧记录(当时还没存战区信息), 无法画战区位置")
            return "\n".join(lines)
        lines.append(f"见过的作战范围: {len(layouts)} 种(大图/小图、不同战区都归在本模式下)")
        for x in layouts[:4]:
            zones = x.get("zones") or []
            where = "、".join(
                f"{z.get('label')}({float(z.get('x') or 0):.0f},"
                f"{float(z.get('z') or z.get('y') or 0):.0f})" for z in zones)
            spread = layout_spread(zones) if zones else 0.0
            lines.append(f"  · {len(zones)} 战区 {where} 跨度 {spread:.3f}")
        if len(layouts) > 4:
            lines.append(f"  · …还有 {len(layouts) - 4} 种")
        return "\n".join(lines)

    def _show_tracks_of(self, item: QTreeWidgetItem, shown: bool) -> None:
        """把某一层(地图/模式)下面的轨迹全部显示或隐藏。

        侧栏只剩三层了, 所以从模式行点就是该模式全部轨迹; 从地图行点则只取
        其中第一个模式(树的顺序 = 最新在前)。
        """
        rows = [it.data(0, _ROLE_ID) for it in self._iter_route_items_of(item)]
        if shown:
            # 这一分支里只保留一个分组(与 _show_all_tracks 同一规则)
            groups: list[tuple[str, str]] = []
            keep: tuple[str, str] | None = None
            for rid in rows:
                rec = self.store.get(rid)
                if rec is None:
                    continue
                grp = self._group_of(rec)
                if grp not in groups:
                    groups.append(grp)
            keep = self._anchor if (self._anchor in groups) else (groups[0] if groups else None)
            ids = [rid for rid in rows
                   if (rec := self.store.get(rid)) is not None
                   and keep is not None and self._group_of(rec) == keep]
            self._visible_ids = set(ids)
            extra = "(该分支含多个模式, 只显示了最新那个)" if len(groups) > 1 else ""
        else:
            ids = rows
            for rid in ids:
                self._visible_ids.discard(rid)
            extra = ""
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        verb = "已显示" if shown else "已隐藏"
        self._set_status(f"{verb}该分支的 {len(ids)} 条轨迹{extra}", sticky=2.0)

    def _iter_route_items_of(self, item: QTreeWidgetItem):
        """递归取出某层下面的全部轨迹条目。"""
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.data(0, _ROLE_KIND) == "route":
                yield ch
            else:
                yield from self._iter_route_items_of(ch)

    def _show_on_map(self, item: QTreeWidgetItem) -> None:
        """右键“在地图上显示”: 点亮该条目的眼睛(而不是进入回放)。"""
        self._set_visible(item, True)

    def _hide_from_map(self, item: QTreeWidgetItem) -> None:
        """右键“从地图上隐藏”: 熄灭该条目的眼睛。"""
        self._set_visible(item, False)

    def _set_visible(self, item: QTreeWidgetItem, shown: bool) -> None:
        """统一的“显示/隐藏”入口(眼睛点击与右键菜单都走这里)。"""
        if item.data(0, _ROLE_KIND) != "route":
            return
        route_id = item.data(0, _ROLE_ID)
        if (route_id in self._visible_ids) == shown:
            return
        rec = self.store.get(route_id)
        if shown:
            self._visible_ids.add(route_id)
            # **同一分组(地图 + 模式)**才能一起显示: 不同模式(战区数量不同)的线路
            # 叠在一起毫无意义。点亮别处时先把其它分组的隐掉。
            if rec is not None:
                grp = self._group_of(rec)
                others = [rid for rid in self._visible_ids
                          if rid != route_id
                          and (o := self.store.get(rid)) is not None
                          and self._group_of(o) != grp]
                if others:
                    self._visible_ids -= set(others)
                    self._set_status(
                        "同一张图同一个模式才能一起显示, 已隐藏其它分组的轨迹",
                        sticky=3.0)
        else:
            self._visible_ids.discard(route_id)
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        if shown:
            self._ensure_map_for(self.map_view.visible_tracks)
        what = f"{rec.vehicle} {fmt_time(rec.duration)}" if rec is not None else "该轨迹"
        self._set_status(
            f"已显示 {what}(当前共 {len(self._visible_ids)} 条)" if shown
            else f"已隐藏 {what}(当前共 {len(self._visible_ids)} 条)", sticky=2.0)
        log.info("轨迹显示切换: %s -> %s (共 %d 条)", route_id, "显示" if shown else "隐藏",
                 len(self._visible_ids))

    def _refresh_eye_icons(self) -> None:
        """按 _visible_ids / _visible_lives 重刷所有眼睛图标(不重建树)。

        单条轨迹看自己的 id; 全局线路看 `_visible_lives`; 全局条目行与 A/B 队行
        是**汇总**: 只要下面有任意一条线亮着, 就点亮。
        """
        def walk(item: QTreeWidgetItem) -> None:
            kind = item.data(0, _ROLE_KIND)
            if kind == "route":
                item.setIcon(_COL_EYE,
                             eye_icon(item.data(0, _ROLE_ID) in self._visible_ids))
            elif kind == "glife":
                item.setIcon(_COL_EYE,
                             eye_icon(item.data(0, _ROLE_ID) in self._visible_lives))
            elif kind in ("global", "gside", "gplayer"):
                ids = self._life_ids_of(item)
                item.setIcon(_COL_EYE,
                             eye_icon(any(i in self._visible_lives for i in ids)))
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

    def _delete_route(self, item: QTreeWidgetItem) -> None:
        route_id = item.data(0, _ROLE_ID)
        rec = self.store.get(route_id)
        if rec is None:
            return
        ok = QMessageBox.question(
            self, "删除线路记录",
            f"确定删除该记录吗?\n\n{rec.map_name} | {rec.vehicle} | 用时 {fmt_time(rec.duration)}",
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(route_id)
        if self._last_record is not None and self._last_record.route_id == route_id:
            self._last_record = None
        self._reload_tree()      # 重建后会自动把“已删除的轨迹”从地图上拿掉
        self._set_status("已删除线路记录", sticky=2.0)

    def _rename_map(self, map_key: str) -> None:
        current = self.store.display_name(map_key)
        name, ok = QInputDialog.getText(self, "重命名地图", "地图名称:", text=current)
        if not ok or not name.strip():
            return
        self.store.rename_map(map_key, name)
        self._reload_tree()

    # ------------------------------------------------------------- 全局录制
    def _import_replay_local(self) -> None:
        """「从录像中获取路线(本地)」: 可选**多个** `.wrpl` 文件, 逐个解析。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择本地回放文件（可多选；每个文件生成一个条目）", "",
            "战争雷霆回放 (*.wrpl);;所有文件 (*)")
        if paths:
            self._import_replay_many(paths, expect_server=False)

    def _import_replay_server(self) -> None:
        """「从录像中获取路线(服务器)」: 可选**多个**分片文件夹, 逐个解析。"""
        dirs = self._pick_directories(
            "选择服务器回放分片所在文件夹（可多选；同一局的 .wrpl 放同一个文件夹）")
        if dirs:
            self._import_replay_many(dirs, expect_server=True)

    def _pick_directories(self, title: str) -> list[str]:
        """多选文件夹。

        `QFileDialog.getExistingDirectory` 一次只能选一个, 所以自己搭一个 **Qt 自带
        (非原生)** 的对话框 —— Windows 的原生对话框不支持多选目录。
        """
        dlg = QFileDialog(self, title)
        dlg.setFileMode(QFileDialog.FileMode.Directory)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
        for view_name, view_type in (("listView", QListView), ("treeView", QTreeView)):
            view = dlg.findChild(view_type, view_name)
            if view is not None:     # Ctrl/Shift 点选多个目录
                view.setSelectionMode(
                    QAbstractItemView.SelectionMode.ExtendedSelection)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return []
        return [p for p in dlg.selectedFiles() if os.path.isdir(p)]

    def _import_replay_many(self, paths: list[str], *, expect_server: bool) -> None:
        """逐个解析 `paths`（回放文件 / 分片文件夹），**每个生成一个全局条目**。

        * 进度框常驻, 每解析一部分就刷新一次并 `processEvents` —— 不然界面会假死
          (2026-10-25 用户反馈: 点服务器录像后卡住 + 内存一直涨);
        * 「取消」随时可点: 抛 `ImportCancelled` 中止当前这局;
        * 单局失败只跳过它, 不影响后面的;
        * **两局以上按局并行**(每局一个进程, 见 `import_pool`): 一局服务器回放
          要解 20~35 秒的位流, 串行导入 10 局就是等 5 分钟。单局仍走下面这条路
          —— 它能给出「分片 3/24」这种细进度, 而且不必为一次导入 spawn 一个解释器。
        """
        from .wrpl_reader import ImportCancelled

        n = len(paths)
        dlg = QProgressDialog("准备解析回放…", "取消", 0, 1000, self)
        dlg.setWindowTitle("从录像中获取路线")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setMinimumWidth(480)
        dlg.show()
        QApplication.processEvents()

        # `i` = 串行时正在解析第几局; `whole` = 并行时 frac 已经是**整体**进度
        state = {"i": 0, "whole": False}

        def progress(msg: str, frac: float | None = None) -> None:
            span = 1.0 / max(1, n)
            if state["whole"]:
                v = frac or 0.0
            else:
                v = state["i"] * span + span * (frac or 0.0)
            dlg.setLabelText(msg)
            dlg.setValue(int(max(0.0, min(1.0, v)) * 1000))
            QApplication.processEvents()
            if dlg.wasCanceled():
                raise ImportCancelled("用户取消")

        done: list[tuple[object, object]] = []
        failed: list[tuple[str, str]] = []
        cancelled = False
        try:
            if n >= 2 and not os.environ.get("WTRT_NO_PARALLEL"):
                done, failed, cancelled = self._import_replay_parallel(
                    paths, expect_server=expect_server, state=state, progress=progress)
            else:
                for i, path in enumerate(paths):
                    state["i"] = i
                    name = os.path.basename(path.rstrip("\\/")) or path
                    dlg.setLabelText(f"[{i + 1}/{n}] 准备解析 {name} …")
                    dlg.setValue(int(i / max(1, n) * 1000))
                    QApplication.processEvents()
                    try:
                        rec, stats = import_replay(
                            path, self.store, progress=progress,
                            expect_server=expect_server)
                    except ImportCancelled:
                        cancelled = True
                        break
                    except Exception as exc:            # noqa: BLE001 - 逐个报错
                        log.warning("回放导入失败 %s: %s", path, exc)
                        failed.append((name, str(exc)))
                        continue
                    done.append((rec, stats))
        finally:
            dlg.close()

        if done:
            self._reload_tree()
            # 新导入的线路默认点亮(否则侧栏里有、地图上一片空, 会以为没导入成功)
            # ⚠ 必须遍历**全部**队别(A/B/**AI**) —— 只点亮 A/B 的话, AI 组那几条线
            #   导入后是隐藏的, 看起来就像"少了几条线"。
            for rec, _st in done:
                for _side in GLOBAL_SIDES:
                    self._visible_lives |= {lf.life_id
                                            for lf in (rec.sides.get(_side) or ())}
            self._refresh_eye_icons()
            first = done[-1][0]
            self._expand_item(f"global|{first.route_id}")
            # 底图: 有缓存就用; 回放推导出的地图键(`wrpl_xxx`)通常没有缓存底图,
            # 而地图控件在“没有底图”时**根本不画线路** → 用网格占位图顶上
            no_map = not self._show_cached_map(first.map_key)
            if no_map:
                self.map_view.set_map_bytes(
                    self._placeholder_map_png(first.map_name), key=first.map_key)
            self._refresh_map_tracks()

        self._report_import(done, failed, cancelled, n)

    def _import_replay_parallel(self, paths: list[str], *, expect_server: bool,
                                state: dict, progress) -> tuple[list, list, bool]:
        """多局一起导入时**按局并行**(见 `import_pool`)。

        取消 = 不再等后面那些局; **已经解析完的照样入库**(不让白跑的活浪费掉)。
        """
        from .import_pool import import_many

        state["whole"] = True              # 这里的 frac 直接就是整体进度
        return import_many(paths, self.store, expect_server=expect_server,
                           progress=progress, log_path=_log_file_path())

    def _report_import(self, done: list, failed: list[tuple[str, str]],
                       cancelled: bool, total: int) -> None:
        """导入结束后汇总: 成功的写状态栏, 有失败/取消才弹框。"""
        lines = []
        uncalibrated: list[str] = []
        for rec, st in done:
            src = "服务器" if st.server else "本地"
            one = (f"· {st.level or '?'}（{st.battle_type or '未知模式'}）"
                   f"[{src}{f' {st.parts} 分片' if st.parts > 1 else ''}] "
                   f"{st.lives} 条线路 / {st.samples:,} 样本 / "
                   f"用时 {fmt_time(st.duration)}")
            if st.players:
                one += f" / {st.players} 位玩家 / {st.kills} 次击杀"
            if st.deaths:
                one += f" / {st.deaths} 辆被击毁"
            if st.failed_packets:
                one += f" / {st.failed_packets} 个包解析失败"
            if st.dropped:
                one += f" / {st.dropped} 段太短已跳过"
            # 位置准不准取决于有没有 8111 的地图校准(世界米 → 底图像素)
            if st.calibrated:
                one += f" / 已按 8111 校准对齐（{st.calib_note}）"
            else:
                one += " / ⚠ 本图未校准, 位置是按本局观测范围估算的"
                if st.level and st.level not in uncalibrated:
                    uncalibrated.append(st.level)
            lines.append(one)
        head = f"共 {total} 个输入: 成功 {len(done)}"
        if failed:
            head += f", 失败 {len(failed)}"
        if cancelled:
            head += ", 已取消剩余"
        tail = ("\n\n失败:\n"
                + "\n".join(f"· {nm}\n  {why}" for nm, why in failed)) if failed else ""
        if uncalibrated:
            tail += ("\n\n⚠ 这些地图还没有校准, 线路位置/比例是估算的:\n"
                     + "\n".join(f"· {lv}" for lv in uncalibrated)
                     + "\n解决办法: 进一局这张图(不用开录制), 软件会自己把底图与外框存下来; "
                       "再到菜单「地图管理」里点「校准地图位置」确认一下即可。")
        if len(done) == 1 and not failed and not cancelled and not uncalibrated:
            self._set_status(f"已导入: {lines[0].lstrip('· ')}", sticky=10.0)
            return
        body = "\n".join(lines) if lines else "(没有成功导入任何一局)"
        QMessageBox.information(
            self, "从录像中获取路线",
            f"{head}\n\n{body}{tail}" if (failed or cancelled or uncalibrated)
            else f"{head}\n\n{body}")
        self._set_status(f"{head} (详情见弹窗)", sticky=10.0)

    def _placeholder_map_png(self, name: str) -> bytes:
        """没有底图时造一张带网格的深色图 —— 只为让线路能看见、能对着比例看。"""
        from PyQt6.QtCore import QBuffer, QRectF
        from PyQt6.QtGui import QImage, QPainter

        side = 1024
        img = QImage(side, side, QImage.Format.Format_RGB32)
        img.fill(QColor("#1b2430"))
        p = QPainter(img)
        try:
            for i in range(0, side + 1, 64):
                p.setPen(QColor("#3d5068") if i % 256 == 0 else QColor("#2c3a4d"))
                p.drawLine(i, 0, i, side)
                p.drawLine(0, i, side, i)
            p.setPen(QColor("#8fa3bd"))
            p.setFont(QFont("Microsoft YaHei", 26))
            p.drawText(QRectF(0, side / 2 - 40, side, 80),
                       Qt.AlignmentFlag.AlignCenter, name or "回放地图")
            p.setPen(QColor("#64748b"))
            p.setFont(QFont("Microsoft YaHei", 12))
            p.drawText(QRectF(0, side - 56, side, 40), Qt.AlignmentFlag.AlignCenter,
                       "暂无底图（空白网格）· 坐标由回放推导，比例仅供参考")
        finally:
            p.end()
        buf = QBuffer()
        buf.open(QBuffer.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    def _start_global(self, side: str) -> None:
        """点「A 队录制 / B 队录制」。

        目标条目 = 侧栏里被标记为这一队的全局条目; 没有标记 → 新建一条。
        如果目标条目这一队**已经录过**, 先问“是否清空重录”(用户要求的确认)。
        """
        if self._in_playback():
            play_error_sound()
            self._set_status("回放中已暂停录制功能, 请先点「■ 结束播放」", sticky=2.0)
            return
        if self.recorder.global_recording:
            self._set_status(
                f"已在全局录制中({self.recorder.global_side} 队), 请先点「停止 A/B 队录制」",
                sticky=2.5)
            return
        if self.recorder.recording:
            self._set_status("正在录制单条轨迹, 请先结束当前录制", sticky=2.5)
            return
        target = self.store.marked_global(side)
        if target is not None and target.lives(side):
            n = len(target.lives(side))
            ok = QMessageBox.question(
                self, f"{side} 队录制",
                f"「{target.label}」的 {side} 队已经录制了 {n} 条玩家线路。\n\n"
                f"要清空这 {n} 条, 重新录制本条目 {side} 队的所有玩家路线吗?\n"
                f"(选“否”则取消, 原有录制保持不变)")
            if ok != QMessageBox.StandardButton.Yes:
                self._set_status("已取消: 保留原有的全局录制", sticky=3.0)
                return
        play_sound("start")
        self.recorder.request_global_start(side, target)

    def _stop_global(self) -> None:
        if not self.recorder.global_recording:
            self._set_status("当前没有正在进行的全局录制", sticky=2.0)
            return
        play_sound("stop")
        self.recorder.request_global_stop()

    def _on_global_armed(self, rec: GlobalRecord, side: str) -> None:
        """已建好条目, 正在等第一名玩家重生(只有此时可以点停止/取消)。"""
        self.player.pause()          # 全局录制与回放互斥
        self.player.load(None)
        self.map_view.set_playback(None)
        self._visible_ids.clear()
        self._visible_lives.clear()
        self._reload_tree()
        self._update_buttons()
        # 把底图切回**游戏当前地图**(用户可能正在预览另一张图的轨迹)
        key = self.recorder.map_key
        if key and key != PLACEHOLDER_MAP_KEY and self.map_view.map_key != key:
            self._show_cached_map(key)
        self._set_status(
            f"全局录制已就绪({side} 队 → 「{rec.label}」): 等待第一名玩家重生后开始计时…\n"
            f"现在请让游戏进入开局/回放, 点「停止 A/B 队录制」可取消", sticky=10.0)

    def _on_global_started(self, rec: GlobalRecord, side: str) -> None:
        """第一次看到可录单位 → 0 点已定。"""
        self._set_status(
            f"{side} 队全局录制中: 已捕获第一名玩家, 计时从 0 开始"
            f"(地图: {rec.map_name})", sticky=6.0)

    def _on_global_finished(self, rec: GlobalRecord, side: str) -> None:
        self._last_global = rec
        self._last_duration = rec.duration
        self.overlay.set_active(False)      # 录完就收掉叠加层
        self._update_buttons()
        # 刚录完的这一队自动点亮, 否则侧栏看起来“什么都没发生”
        self._visible_lives = {life.life_id for life in rec.lives(side)}
        self._reload_tree()
        self._expand_item(rec.route_id)
        self._ensure_map_for([rec])
        self._set_status(
            f"全局录制完成: {rec.label} · {side} 队 {len(rec.lives(side))} 条线路"
            f" | 用时 {fmt_time(rec.duration)}", sticky=10.0)

    def _on_global_cancelled(self) -> None:
        self.overlay.set_active(False)
        self._update_buttons()
        self._reload_tree()
        self._set_status("已取消全局录制(没有录到任何线路, 条目已丢弃)", sticky=4.0)

    # ==================================================================== 动作
    def _hotkey_action(self, action: str) -> None:
        """两套全局快捷键通道的统一入口(先去掉重复命中, 再走按钮那条路)。"""
        now = time.monotonic()
        if now - self._last_hotkey_at.get(action, 0.0) < 0.3:
            return                    # 系统热键与轮询可能同时命中 → 只算一次
        self._last_hotkey_at[action] = now
        self._handle_action(action)

    def apply_hotkey(self, action: str, binding: tuple[str, str]) -> None:
        """改某个动作的快捷键: 两套通道立即生效并存盘(设置对话框调用)。"""
        self._hotkey_bindings[action] = (binding[0] or "", binding[1])
        self.hotkeys.set_bindings(self._hotkey_bindings)
        self.sys_hotkeys.apply(self._hotkey_bindings)
        save_bindings(self._settings, {action: self._hotkey_bindings[action]})
        self._update_buttons()
        self._set_status(f"{ACTION_LABELS[action]} 的快捷键已改为 "
                         f"{describe_binding(self._hotkey_bindings[action])}", sticky=3.0)

    def _on_poll_toggled(self, on: bool) -> None:
        """菜单开关: 是否同时用 GetAsyncKeyState 轮询(系统热键通道始终在跑)。"""
        self._poll_enabled = bool(on)
        self._settings.setValue("hotkey_poll", 1 if on else 0)
        if on:
            self.hotkeys.start()
        else:
            self.hotkeys.stop()
        self._set_status("已启用按键轮询(可区分左右 Shift)" if on
                         else "已关闭按键轮询, 只使用系统热键(不读按键状态)", sticky=3.0)

    def _reset_hotkeys(self) -> None:
        """菜单: 把三个快捷键恢复默认(F5 开始 / F6 结束 / F7 标记)。"""
        for action, binding in DEFAULT_BINDINGS.items():
            self._hotkey_bindings[action] = binding
        self.hotkeys.set_bindings(self._hotkey_bindings)
        self.sys_hotkeys.apply(self._hotkey_bindings)
        reset_bindings(self._settings)
        self._set_status("快捷键已恢复默认: F5 开始 / F6 结束 / F7 标记", sticky=4.0)

    def _open_hotkey_settings(self) -> None:
        """菜单「快捷键 → 快捷键设置」: 改按键/修饰键, 改动立即生效。"""
        dlg = HotkeyDialog(self, self)
        dlg.exec()
        self._set_status("快捷键: " + " · ".join(
            f"{ACTION_LABELS[a]}={describe_binding(b)}"
            for a, b in self._hotkey_bindings.items()), sticky=4.0)
        self._update_buttons()

    def _handle_action(self, action: str) -> None:
        """按钮与全局快捷键的统一入口: 先播放提示音, 再投递录制命令。"""
        if self._in_playback():
            # 回放期间录制功能一律不响应(按钮已禁用, 这里堵的是全局快捷键)
            play_error_sound()
            self._set_status("回放中已暂停录制功能, 请先点「■ 结束播放」", sticky=2.0)
            return
        play_sound(action)
        if action == "start":
            # 选项随命令一起投递到轮询线程(避免共享状态的竞态)
            self.recorder.request_start(self.chk_start_on_move.isChecked())
        elif action == "mark":
            self.recorder.request_mark()
        elif action == "stop":
            self.recorder.request_stop()

    def _on_start_on_move_changed(self) -> None:
        """选项只对本次运行有效(不写配置, 保证每次启动都是默认关闭的)。"""
        on = self.chk_start_on_move.isChecked()
        self._set_status(
            "已开启: 开始录制后等待载具移动, 以速度从零变化的前一时刻作为计时起点"
            if on else "已关闭: 按点下开始录制的时刻计时", sticky=4.0)

    def _on_dead_style_changed(self) -> None:
        """回放的两个“被击毁”显示选项: 应用到地图并记住设置。"""
        dim = self.chk_dead_dim.isChecked()
        mark = self.chk_dead_mark.isChecked()
        self._saved_dead_dim, self._saved_dead_mark = dim, mark
        self._settings.setValue("dead_dim", 1 if dim else 0)
        self._settings.setValue("dead_mark", 1 if mark else 0)
        self.map_view.set_dead_style(dim=dim, mark=mark)
        if dim or mark:
            self._set_status(
                f"回放显示: 阵亡变暗{'开' if dim else '关'} · "
                f"阵亡标记{'开' if mark else '关'}", sticky=3.0)
        else:
            self._set_status("回放显示: 不区分阵亡", sticky=3.0)

    def _build_rate_submenu(self, parent, title: str, saved_ms: int, choices, on_pick,
                            tip: str) -> list[QAction]:
        """在「设置 → 轮询间隔」下建一组互斥单选项(三档轮询率各一个子菜单)。"""
        sub = parent.addMenu(title)
        sub.setToolTip(tip)
        group = QActionGroup(self)
        group.setExclusive(True)
        acts: list[QAction] = []
        for ms in choices:
            text = (f"{ms} ms({1000 // ms} Hz)" if ms < 1000
                    else f"{ms} ms({ms / 1000:g} s)")
            act = QAction(text, self)
            act.setCheckable(True)
            act.setChecked(int(ms) == int(saved_ms))
            act.setToolTip(tip)
            act.triggered.connect(lambda _c=False, v=int(ms): on_pick(v))
            group.addAction(act)
            sub.addAction(act)
            acts.append(act)
        return acts

    @staticmethod
    def _sync_rate_actions(acts, choices, ms: int) -> None:
        """代码路径改值后也要让菜单勾选跟上(`setChecked` 不会触发 `triggered`)。"""
        for act, val in zip(acts or (), choices):
            act.setChecked(int(val) == int(ms))

    def _set_record_rate_ms(self, ms: int) -> None:
        """菜单「设置 → 轮询间隔 → 单条录制轮询率」。"""
        ms = rate_choice_ms(ms, INTERVAL_CHOICES_MS)
        rec = getattr(self, "recorder", None)
        if rec is not None:
            rec.set_interval_ms(float(ms))
        self._settings.setValue("interval_ms", int(ms))
        self._saved_ms = int(ms)
        self._sync_rate_actions(getattr(self, "_rate_actions", ()),
                                INTERVAL_CHOICES_MS, ms)
        self._set_status(
            f"单条录制轮询率已设为 {int(ms)} ms(≈{1000 // int(ms)} Hz)", sticky=2.0)

    def _set_status(self, text: str, sticky: float = 0.0) -> None:
        """写状态栏。sticky>0 时该文字在这么多秒内不会被刷新的状态覆盖
        (否则提示类信息 80ms 后就没了)。"""
        self.lbl_status.setText(text)
        if sticky > 0:
            self._sticky_text = text
            self._sticky_until = time.monotonic() + float(sticky)

    def _in_playback(self) -> bool:
        """是否处于回放状态(已载入回放集合 → 回放绘制生效, 其它功能锁定)。"""
        # 构造早期(_reload_tree)就会调到, 那时 player 还没建
        player = getattr(self, "player", None)
        return bool(player.records) if player is not None else False

    def _update_buttons(self, recording: bool | None = None) -> None:
        """根据“录制中 / 回放中”刷新所有控件的可用状态。

        回放与录制互斥, 且**回放期间其余功能全部禁用**(眼睛、侧栏、录制按钮、
        采样间隔、快捷键入口), 只有点「■ 结束播放」才恢复 —— 否则回放绘制会盖住
        轨迹显示, 用户会以为“点了眼睛没反应”。
        """
        if recording is None:
            recording = self.recorder.recording
        armed = self.recorder.armed
        in_play = self._in_playback()
        g_rec = self.recorder.global_recording
        busy = recording or g_rec          # 两种录制都算“占用主界面”
        self.btn_start.setEnabled(not busy and not in_play)
        self.btn_stop.setEnabled(recording)
        # 等待移动期间还没定下起点, 标记没有意义
        self.btn_mark.setEnabled(recording and not armed)
        self.chk_start_on_move.setEnabled(not busy and not in_play)
        # 轮询率在菜单里: 录制/回放期间同样不允许改
        for act in getattr(self, "_rate_actions", ()):
            act.setEnabled(not busy and not in_play)
        self.tree.setEnabled(not in_play and not busy)   # 含眼睛点击与右键菜单
        # 从录像导入: 录制/回放期间不允许(会抢主界面)
        self.btn_imp_local.setEnabled(not busy and not in_play)
        self.btn_imp_server.setEnabled(not busy and not in_play)
        # 录制期间屏蔽会改变“显示/底图”的入口, 避免与录制抢主界面
        self.act_manage.setEnabled(not busy)
        self.act_refresh.setEnabled(not busy)
        # 回放控制
        has_track = (bool(self.player.records) or self._last_record is not None
                     or bool(self.store.routes)
                     or any(g.count() for g in self.store.globals))
        self.btn_play.setEnabled(not busy and has_track)
        self.btn_play_mark.setEnabled(not busy and has_track)
        self.cmb_speed.setEnabled(not busy)
        self.btn_play_stop.setEnabled(in_play)
        self.btn_play_stop.setStyleSheet("font-weight:bold;" if in_play else "")

    # ---------------------------------------------------------------- 回放
    def _fallback_tracks(self) -> list:
        """侧栏里**既没选中、也没点亮眼睛**时的兜底: 播「当前底图」下最新那一局。

        v1.3.0 起线路都来自「从录像导入」, 不再有“刚录制的那条”(`_last_record`),
        而**眼睛点亮状态不落盘** —— 重开程序后侧栏里的眼睛全是关的, 于是直接点
        「▶ 回放」得到空集合、什么都不发生(用户 2026-10-26 反馈“路线播放器无法播放”)。

        兜底规则:
        * 只取**一个**全局条目 —— 两次导入是两局不同对局, 混在同一条时间轴上没有意义;
        * 优先**当前底图**那张, 没有就退到最新的一个;
        * 每个条目内取全部线路(等价于在侧栏选中整个条目)。
        """
        def lives(g) -> list:
            return [lf for side in GLOBAL_SIDES for lf in g.lives(side)
                    if len(lf.points) >= 2]

        cand = [g for g in self.store.globals if lives(g)]
        if not cand:
            return []
        try:
            key = self.map_view.map_key or ""
        except Exception:        # noqa: BLE001 - 视图还没建好时按“不限定地图”处理
            key = ""
        same = [g for g in cand if not key or (g.map_key or "") == key]
        return lives((same or cand)[-1])      # 最新的那一条

    def _playable_tracks(self) -> list[RouteRecord]:
        """要回放的轨迹 = **侧栏当前选中的条目**(选中一条就播一条, 选中多条就播多条,
        一起从 00:00 开始, 以最长的那条作为结束)。

        没有选中任何条目时依次退回: 眼睛点亮的轨迹 → 刚录制的那条 →
        当前底图下最新那一局的全部线路(见 `_fallback_tracks`)。
        """
        if self.player.records:          # 已在回放中 → 保持原来的集合
            return self.player.records
        picked = self._selection_playables()
        if picked:
            return picked
        shown = self.map_view.visible_tracks
        if not shown and self._last_record is not None:
            shown = [self._last_record]
        if shown:
            return shown
        return self._fallback_tracks()

    def _load_playback(self, recs) -> None:
        """载入回放(单条或列表)并复位到开头。

        - 进入回放后其它功能全部禁用(见 _update_buttons), 需点「■ 结束播放」退出;
        - 先把地图切成这批轨迹所属的那张图(游戏没开时用本地缓存)。
        """
        self._ensure_map_for(list(recs) if isinstance(recs, (list, tuple)) else [recs])
        self.player.load(recs)
        # 战区要跟着这次回放的**对象**换: 全局线路用条目自己的布局/时间轴,
        # 单条轨迹用它录的那套(见 `_apply_zones`) —— 否则会留着上一批线路的战区
        self._apply_zones()
        self._update_buttons()
        self._set_status(
            f"回放模式({len(self.player.records)} 条): 其余功能已暂停使用, "
            f"点「■ 结束播放」返回", sticky=3.0)

    def _toggle_playback(self) -> None:
        if self.recorder.recording:
            self._set_status("录制中无法回放, 请先结束录制", sticky=2.0)
            return
        if not self.player.records:
            recs = self._playable_tracks()
            if not recs:
                self._set_status(
                    "侧栏里还没有任何线路 —— 先点「从录像中获取路线」导入一次",
                    sticky=3.0)
                return
            self._load_playback(recs)
        self.player.toggle()

    def _play_to_next_mark(self) -> None:
        if self.recorder.recording:
            self._set_status("录制中无法回放, 请先结束录制", sticky=2.0)
            return
        if not self.player.records:
            recs = self._playable_tracks()
            if not recs:
                self._set_status(
                    "侧栏里还没有任何线路 —— 先点「从录像中获取路线」导入一次",
                    sticky=3.0)
                return
            self._load_playback(recs)
        self.player.play_to_next_mark()

    def _end_playback(self) -> None:
        """结束回放: 退出回放绘制、恢复眼睛点亮的轨迹, 并解锁其余功能。"""
        self.player.pause()
        self.player.load(None)            # 清空回放集合
        self.map_view.set_playback(None)
        self.lbl_play.setText("回放: --")
        self._update_buttons()
        self._refresh_map_tracks()        # 把眼睛点亮的轨迹重新画 (不透明)
        self._on_tree_selection_changed()  # 恢复“待回放: N 条”提示
        self._set_status("已结束回放, 恢复显示眼睛点亮的轨迹", sticky=2.0)

    def _on_speed_changed(self) -> None:
        x = self.cmb_speed.currentData()
        if x is None:
            return
        self.player.set_speed(float(x))
        self._settings.setValue("playback_speed", float(x))

    def _update_playback_buttons(self) -> None:
        """兼容旧调用点: 实质上重新计算全部控件状态。"""
        self._update_buttons()

    def _sync_playback_zones(self, t: float) -> None:
        """回放全局录制时, 把战区状态跟着时间轴一起推进。

        用户 2026-10-22 要求: 回放 AB 队线路时要能看到当时战区被谁占着、
        哪个正在被占领 —— 而不是一张与录像无关的静态布局(甚至是**另一个区域**
        的布局: 全局录像和单条路线可能根本不在同一片交战区)。
        `set_zones` 自己会去重, 所以每帧调用只是几次列表比较。
        """
        g = self._playback_global()
        if g is None:
            return
        entry = g.zone_state_entry(t, self._zone_state_side())
        if entry:
            self.map_view.set_zones(self._remap_zone_state(
                entry.get("zones") or [], str(entry.get("side") or "")))

    def _on_playback_progress(self, t: float) -> None:
        recs = self.player.records
        self.map_view.set_playback(recs, t)
        self._sync_playback_zones(t)
        if recs:
            n = f"  ({len(recs)} 条)" if len(recs) > 1 else ""
            self.lbl_play.setText(
                f"回放 {fmt_time_ms(t)} / {fmt_time(self.player.end)}{n}")

    def _on_playback_state(self, playing: bool) -> None:
        # 用文本符号而不是 emoji: ⏸/⏭ 在 Windows 上会被 Segoe UI Emoji 渲染成彩色
        # 图标, 与旁边的 ▶/■ 风格不一致(用户 2026-10-24 提的)
        self.btn_play.setText("‖ 暂停" if playing else "▶ 回放")
        self._update_buttons()

    def _on_playback_finished(self) -> None:
        self._set_status(
            f"回放结束({len(self.player.records)} 条轨迹已播完) —— "
            f"点「■ 结束播放」返回查看/切换轨迹", sticky=4.0)

    def _on_playback_mark(self, mk) -> None:
        self._set_status(
            f"已到标记 {fmt_time_ms(mk.t)} 并暂停 —— 点「⏭」继续到下一个标记", sticky=3.0)

    # ==================================================================== 信号
    def _on_tick(self, info: dict) -> None:
        # 地图重绘跟随刷新率(轨迹平滑); 文本按 ~12Hz 节流, 避免高频刷新浪费 CPU
        now = time.monotonic()
        if now - self._last_text_at >= 0.08:
            self._last_text_at = now
            self.lbl_vehicle.setText(f"载具: {info.get('vehicle') or '未知载具'}")
            speed = float(info.get("speed") or 0.0)
            self.lbl_speed.setText(f"速度: {fmt_speed(speed)} km/h")
            duration = (float(info.get("elapsed") or 0.0) if info.get("recording")
                        else self._last_duration)
            self.lbl_timer.setText(f"计时: {fmt_time(duration)}")
            self.lbl_marks.setText(f"标记: {int(info.get('marks') or 0)} 个")

            rate = float(info.get("rate") or 0.0)
            if rate:
                self.lbl_rate.setText(f"轮询 {rate:.0f} Hz")

            ginfo = info.get("global")
            if ginfo:
                # 全局录制期间状态栏只显示录制进度(比“可开始录制”重要得多)
                side = str(ginfo.get("side") or "?")
                elapsed = float(ginfo.get("elapsed") or 0.0)
                lives = int(ginfo.get("lives") or 0)
                self.lbl_timer.setText(f"计时: {fmt_time(elapsed)}")
                self.lbl_marks.setText(f"线路: {lives} 条")
                if ginfo.get("waiting"):
                    self.lbl_status.setText(
                        f"全局录制已就绪({side} 队): 等待第一名玩家重生后开始计时…"
                        f" | 点「停止 A/B 队录制」可取消")
                else:
                    self.lbl_status.setText(
                        f"{side} 队全局录制中 {fmt_time(elapsed)} | 线路 {lives} 条"
                        f"({int(ginfo.get('active') or 0)} 辆进行中) | "
                        f"回到车库自动结束")
            elif now < self._sticky_until:
                # 刚发生的事(回放开始/结束、显示切换、提示)优先显示, 不被刷掉
                self.lbl_status.setText(self._sticky_text)
            elif not info.get("connected"):
                self.lbl_status.setText("未连接到 8111 (请启动游戏)")
            elif self._in_playback():
                self.lbl_status.setText(
                    "回放模式: 其余功能已暂停使用, 点「■ 结束播放」返回")
            elif not info.get("recording"):
                if info.get("player") is None:
                    self.lbl_status.setText("已连接 8111 | 等待进入对局/试车场(未获取到玩家位置)")
                else:
                    mode = str(info.get("mode") or "")
                    own = ownership_text(info.get("zones") or [])
                    tail = " | ".join(x for x in (mode, own) if x)
                    self.lbl_status.setText(
                        f"已连接 8111 | {tail} | 可开始录制" if tail
                        else "已连接 8111 | 可开始录制")

            # 没有底图时(游戏没开/战局外)用本地缓存补上, 这样关掉游戏也能看回放
            if self.map_view.map_key is None and now - self._last_map_try >= 1.0:
                self._last_map_try = now
                self._restore_map_for_current_view()
            # 录制叠加层(与上面那批文本同一个 ~12Hz 节流)
            self._update_overlay(info)

        self.map_view.set_player(info.get("player"), info.get("dir"))
        # 全局录制: 边录边画(新点一多就重画一次模型层, 见 _tracks_key)
        lives = info.get("global_lives")
        if lives:
            self.map_view.set_live_multi(lives)
        elif self.map_view.live_multi:
            self.map_view.set_live_multi(None)   # 录完了 → 交给侧栏眼睛控制显示
        self._apply_zones(info)
        self.map_view.update()

    def _on_map_changed(self, data: bytes) -> None:
        # 用录制线程已经解析好的地图身份(同一张地图的不同分辨率会归到同一个键)
        key = self.recorder.map_key
        # 8111 偶尔只发低分辨率的那份(1024²); 手上有更清晰的同一张图就用它显示
        data = self.map_cache.sharpest(key, data)
        changed = self.map_view.set_map_bytes(data, key=key)
        if not changed:
            return
        self._apply_zones()          # 换了地图 → 换用该地图的战区布局
        if self.recorder.map_key == PLACEHOLDER_MAP_KEY:
            # 战局外 8111 只有占位图: **不要**当换图清理, 而是尽量切回“正在看的轨迹”的缓存底图
            self._restore_map_for_current_view()
            return
        self._drop_tracks_of_other_map()

    def _on_map_cached(self, key: str, created: bool) -> None:
        """地图图片已写入本地缓存(新图才提示)。"""
        if not created:
            return
        self._set_status(
            f"已缓存地图图片「{self.store.display_name(key)}」"
            f"(共 {len(self.map_cache.entries())} 张, 可在菜单「地图」→「地图管理」里命名)",
            sticky=4.0)

    # ------------------------------------------------------- 地图图片(缓存/切换)
    def _current_view_key(self) -> str | None:
        """当前在看的轨迹属于哪张地图(显示的 → 选中的 → 刚录制的)。已归一到图片哈希。"""
        for rec in self.map_view.visible_tracks:
            return self.store.canon(rec.map_key)
        for rec in self._selection_tracks():
            return self.store.canon(rec.map_key)
        if self._last_record is not None:
            return self.store.canon(self._last_record.map_key)
        if self._visible_lives:
            keys = self._life_map_keys()
            for lid in self._visible_lives:
                if lid in keys:
                    return keys[lid]
        return None

    def _show_cached_map(self, key: str) -> bool:
        """把地图切成指定地图的**本地缓存图片**(没有缓存返回 False)。"""
        if not key or key == "unknown":
            return False
        key = self.store.canon(key)         # 回放身份 → 实际那张图的哈希
        if self.map_view.map_key == key:
            return True
        data = self.map_cache.load(key)
        if data is None:
            return False
        return bool(self.map_view.set_map_bytes(data, key=key))

    def _ensure_map_for(self, recs: list[RouteRecord]) -> None:
        """让地图改成这批轨迹所属的那张图(游戏没开时就靠本地缓存)。"""
        keys = {self.store.canon(r.map_key) for r in recs
                if r.map_key and r.map_key != "unknown"}
        if len(keys) != 1:
            return
        key = keys.pop()
        if self.map_view.map_key == key:
            return
        if not self._show_cached_map(key):
            self._set_status(
                f"地图「{self.store.display_name(key)}」还没有缓存底图; "
                f"在游戏里进一次该地图就会自动缓存", sticky=4.0)

    def _restore_map_for_current_view(self) -> None:
        """没有底图时(游戏没开/战局外)用缓存恢复到“正在看的这张地图”。"""
        key = self._current_view_key()
        if key is None:
            entries = self.map_cache.entries()
            key = entries[0].key if entries else None    # 兜底: 最近缓存的那张
        if not key or self.map_view.map_key == key:
            return
        if self._show_cached_map(key):
            self._apply_zones()          # 切了地图 → 换用该地图的战区布局
            self._set_status(f"已用缓存底图显示「{self.store.display_name(key)}」", sticky=2.5)
        elif self.map_view.map_key is None:
            self._set_status(
                f"「{self.store.display_name(key)}」还没有缓存底图"
                f"(在游戏里进一次该地图即可缓存)", sticky=4.0)

    def _drop_tracks_of_other_map(self) -> None:
        """地图变了 → 属于其它地图的轨迹/回放全部清除(否则会画在新地图上, 产生误导)。

        以地图图片指纹(rec.map_key)判定: 同一张地图的轨迹保留,
        换图/退出战局(占位图)后旧轨迹被清掉。
        正在录制时保留实时轨迹(清掉会让轨迹直接消失), 只清回放/历史。
        """
        key = self.recorder.map_key
        recording = self.recorder.recording
        canon = self.store.canon
        dropped = self.map_view.drop_tracks_for_other_map(
            key, keep_live=recording, norm=canon)
        if self._last_record is not None \
                and canon(self._last_record.map_key) != canon(key):
            self._last_record = None
            dropped.append("上次录制")
        recs = self.player.records
        if recs and any(canon(r.map_key) != canon(key) for r in recs):
            self.player.pause()
            self.player.load(None)
            self.lbl_play.setText("回放: --")
        if dropped:
            # 眼睛点亮的轨迹也要跟着换图规则走: 不属于当前地图的一律隐藏
            stale = {rid for rid in self._visible_ids
                     if (r := self.store.get(rid)) is None
                     or canon(r.map_key) != canon(key)}
            if stale:
                self._visible_ids -= stale
                self._reload_tree()      # 重建侧栏(顺便刷新眼睛图标与地图)
        # 全局线路同理: 只保留属于当前地图的那些
        life_keys = self._life_map_keys()
        stale_lives = {lid for lid in self._visible_lives
                       if canon(life_keys.get(lid)) != canon(key)}
        if stale_lives:
            self._visible_lives -= stale_lives
            self._reload_tree()
        if dropped or recording:
            self._update_buttons()
        if recording:
            log.info("录制过程中地图图片发生变化 (key=%s)", key)
            self._set_status(
                "录制中地图已切换: 回放/历史轨迹已清除, 本次录制仍归属开始时的地图",
                sticky=4.0)
        elif dropped:
            log.info("切换地图(%s): 已清除 %s", key, "、".join(dict.fromkeys(dropped)))
            self._set_status(
                "地图已切换, 已清除不属于当前地图的轨迹: "
                + "、".join(dict.fromkeys(dropped)), sticky=4.0)

    def _on_recording_started(self, rec: RouteRecord) -> None:
        self._enter_recording_ui(rec, armed=False)

    def _on_start_armed(self, rec: RouteRecord) -> None:
        """已按下开始, 但在等载具移动(此时还没有计时起点)。"""
        self._enter_recording_ui(rec, armed=True)

    def _enter_recording_ui(self, rec: RouteRecord, armed: bool) -> None:
        self.player.pause()          # 录制与回放互斥
        self.player.load(None)
        self.map_view.set_playback(None)
        self.map_view.set_live(rec)
        # 录制时先**把主界面切回游戏当前地图**: 用户可能正在预览另一张地图/另一个
        # 版本的轨迹, 不切回去就会“录着这张图、看着那张图”, 两边打架。
        # 同时清掉之前预览的点亮状态(录制期间只显示正在录的这条)。
        previewed = len(self._visible_ids) + len(self._visible_lives)
        self._visible_ids.clear()
        self._visible_lives.clear()
        self._refresh_eye_icons()
        key = self.recorder.map_key
        switched = False
        if key and key != PLACEHOLDER_MAP_KEY and self.map_view.map_key != key:
            switched = self._show_cached_map(key)
        self._refresh_map_tracks()
        self._update_buttons(True)
        note = ""
        if previewed or switched:
            note = "\n已切回游戏当前地图并暂停预览(录制期间其余功能已锁定)"
        if armed:
            self.lbl_status.setText(
                f"准备录制… 地图: {rec.map_name} | 载具: {rec.vehicle} "
                f"| 等待载具移动(速度从 0 变为非 0 时开始计时){note}"
            )
        else:
            self.lbl_status.setText(
                f"录制中… 地图: {rec.map_name} | 载具: {rec.vehicle} "
                f"| 起点已标注 (按 "
                f"{describe_binding(self._hotkey_bindings.get('stop'))} 结束){note}"
            )

    def _on_recording_cancelled(self) -> None:
        """等待移动期间被结束 → 本次录制被丢弃(全程速度为 0)。"""
        self.map_view.set_live(None)
        self.overlay.set_active(False)
        self._update_buttons(False)
        self._set_status("已取消录制: 未检测到载具移动(速度全程为 0)", sticky=3.0)

    def _on_mark_added(self, _mk) -> None:
        self.map_view.update()

    def _on_recording_finished(self, rec: RouteRecord) -> None:
        self._last_duration = rec.duration
        self._last_record = rec
        self.overlay.set_active(False)      # 录完就收掉叠加层
        # 刚录完的这条仍然显示在地图上 → 眼睛必须同步点亮(显示状态统一由 _visible_ids 决定)
        self._visible_ids.add(rec.route_id)
        self._update_buttons(False)
        self._reload_tree()          # 内部会 _refresh_map_tracks(): 该条用速度配色高亮
        self._expand_to_route(rec.route_id)   # 分组可能是折叠的 → 展开到刚录的这条
        self._set_status(
            f"录制完成: {rec.map_name} | {rec.vehicle} | 用时 {fmt_time(rec.duration)} "
            f"| 轨迹点 {len(rec.points)} | 标记 {len(rec.marks) - 1} 个", sticky=6.0)

    def _on_notice(self, text: str) -> None:
        self._set_status(text, sticky=3.0)

    # ==================================================================== 收尾
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        try:
            self.hotkeys.stop()
            self.sys_hotkeys.stop()
            self.player.pause()
            self.recorder.stop()
            self.overlay.close()          # 叠加层是独立顶层窗, 不关会留在屏幕上
            # 正常退出 = GPU 画布这一路没问题 → 抹掉"正在尝试 GPU"的标记,
            # 否则用户开机 2 秒就关掉时, 下次启动会被误判成"上次崩了"。
            self._clear_gfx_pending()
            # 写盘在后台线程里做(见 `RouteStore.request_save`), 这里**等它做完** ——
            # 不等的话进程一退, 用户刚录的那条就没了。
            self.store.flush()
        except Exception as exc:  # noqa: BLE001 - 退出时尽力清理
            log.warning("退出清理异常: %s", exc)
        super().closeEvent(event)
