"""显卡/适配器的**纯数据**部分 —— 不 import wgpu, 谁都可以安全地顶层导入。

为什么单独拆一个文件:
------------------------------------------------------------------
`route_map_gl.py` 一 import 就会加载 `wgpu-native` 的 DLL, 而无头(offscreen)平台下
那会跟 Qt 打架(实测整个进程 0xC000041D 崩掉, 连测试都跑不了), 所以主程序一直
**惰性导入**它。但「设置 → 图形」的菜单、启动时的提示弹窗都要用
`AdapterOption` / `GpuNotice` 这些类型 —— 它们不该把 wgpu 拖进来。

于是: **类型与常量**留在这里(零依赖), **真正碰 wgpu 的枚举与建 device** 留在
`route_map_gl.py`(只有函数体里才 `import wgpu`)。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("gfx_info")

# ---------------------------------------------------------------- 偏好值
# 「设置 → 图形」里存进 QSettings 的值: 适配器 key(= "<device>|<backend>")或下面两个。
PREF_AUTO = "auto"              # 自动选择(默认): 只认真显卡, 优先 D3D12
PREF_SOFTWARE = "software"      # 强制软件绘制(完全不用 GPU)

# pygfx 在 import 的时候就 `preconfigure_default_device(required_features=...)`
# 写死了这一条 —— 拿不到的适配器**根本建不出 device**。
# 所以"某块卡能不能用"= "它支不支持这个特性", 与型号/年份没有直接关系
# (实测判据工具: `tools/probe_gpu_caps.py`)。
PYGFX_REQUIRED = "float32-filterable"

# 后端偏好: 同一个"自动"选项下挑哪块。D3D12 在 Windows 上最稳(驱动质量最好),
# Vulkan 特性最全但踩过驱动的坑, OpenGL 是尽力而为的兜底。
BACKEND_RANK = {"D3D12": 0, "Vulkan": 1, "Metal": 2, "OpenGL": 3, "Noop": 9}

TYPE_LABEL = {
    "DiscreteGPU": "独立显卡",
    "IntegratedGPU": "集成显卡",
    "CPU": "CPU 软件模拟",
    "VirtualGpu": "虚拟显卡",
    "Unknown": "未知类型",
}


@dataclass(frozen=True)
class AdapterOption:
    """一块可选适配器(不含 wgpu 对象, 以便跨模块/存盘安全传递)。"""

    key: str                # 存盘用: "<device>|<backend>"
    label: str              # 菜单显示
    detail: str             # 悬停说明(类型 / 后端 / 是否 CPU 模拟)
    device: str
    backend: str
    adapter_type: str       # DiscreteGPU / IntegratedGPU / CPU / Unknown / VirtualGpu
    is_fallback: bool       # True = WARP 这类 CPU 软件光栅化

    @property
    def is_gpu(self) -> bool:
        """真显卡(而不是 WARP / CPU / 虚拟适配器)。"""
        return not self.is_fallback and self.adapter_type not in ("CPU", "VirtualGpu", "")


@dataclass
class GpuNotice:
    """GPU 画布没能用上的原因 —— 主程序拿它在启动后**弹一次窗**。

    以前是静默回退软件渲染, 用户只会觉得"为什么这么卡", 完全不知道原因。
    """

    kind: str               # missing / no-adapter / driver / crashed / adapter-gone
    title: str
    body: str
    detail: str = ""        # 日志里那些技术细节
    extra: dict = field(default_factory=dict)

    def as_text(self) -> str:
        return f"{self.title}\n\n{self.body}"


def _adapter_attr(adapter, name: str, default=""):
    """适配器信息兼容读取: 新版本在 `info` 字典里, 老版本是属性。"""
    info = getattr(adapter, "info", None)
    if isinstance(info, dict) and name in info:
        val = info.get(name)
    else:
        val = getattr(adapter, name, default)
    return default if val is None else val


def adapter_option(adapter) -> AdapterOption:
    """把 wgpu 适配器对象折成可安全传出去的 `AdapterOption`。"""
    device = str(_adapter_attr(adapter, "device", "") or "")
    backend = str(_adapter_attr(adapter, "backend_type",
                                _adapter_attr(adapter, "backend", "")) or "")
    atype = str(_adapter_attr(adapter, "adapter_type", "") or "")
    if not device:                       # 极老的 wgpu 只给 description
        device = str(_adapter_attr(adapter, "description", "") or "未知适配器")
    is_fb = atype == "CPU" or "Basic Render Driver" in device
    bits = [TYPE_LABEL.get(atype, atype or "未知类型"), backend or "未知后端"]
    if is_fb:
        bits.append("软件模拟, 很慢")
    return AdapterOption(
        key=f"{device}|{backend}",
        label=f"{device} ({backend})" if backend else device,
        detail=" · ".join(b for b in bits if b),
        device=device, backend=backend, adapter_type=atype, is_fallback=is_fb)


def describe_adapter(adapter) -> str:
    """一行摘要(写日志/显示用), 如 `NVIDIA RTX 3060 Laptop (DiscreteGPU) via Vulkan`。"""
    return str(getattr(adapter, "summary", "") or adapter_option(adapter).label)


def enumerate_adapters() -> tuple[list[AdapterOption], str]:
    """枚举本机适配器; 返回 `(选项列表, 出错说明)`(出错时列表为空)。

    真显卡排前面, 同类型里按后端偏好(WARP 这类 CPU 模拟永远排最后)。
    """
    try:
        import wgpu
    except Exception as exc:            # noqa: BLE001
        return [], f"wgpu 组件不可用: {type(exc).__name__}: {exc}"
    try:
        raw = wgpu.gpu.enumerate_adapters_sync()
    except Exception as exc:            # noqa: BLE001 - 驱动坏掉时这里也会抛
        return [], f"枚举显卡失败: {type(exc).__name__}: {exc}"
    opts = [adapter_option(a) for a in raw]
    opts.sort(key=lambda o: (o.is_fallback, BACKEND_RANK.get(o.backend, 5), o.device))
    return opts, ""


def gpu_usable(force_software: bool = False) -> tuple[bool, str]:
    """能不能安全地创建 GPU 画布; 返回 `(可用, 不可用的原因)`。

    **必须在创建 canvas 之前判断**: 在 `QT_QPA_PLATFORM=offscreen`/`minimal` 这类没有
    真实窗口系统的平台上, wgpu 配置 surface 会在 **Rust 层直接 panic**
    (`wgpuSurfaceConfigure: Surface does not support the adapter's queue family`),
    那是 try/except 抓不住的进程级崩溃 —— 所以测试/无头环境一律走软件渲染。
    """
    if force_software:
        return False, "设置里选了「强制软件绘制」"
    if os.environ.get("WT_MAP_SOFTWARE", "") not in ("", "0"):
        return False, "环境变量 WT_MAP_SOFTWARE 指定了软件绘制"
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:                   # noqa: BLE001 - 没有 Qt 就当无头
        return False, "没有 Qt"
    app = QApplication.instance()
    if app is not None and app.platformName().lower() in ("offscreen", "minimal", "vnc"):
        return False, "没有真实窗口系统(无头/离屏平台)"
    return True, ""
