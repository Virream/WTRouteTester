"""探测本机 GPU 适配器能否满足 **pygfx** 的硬要求(排查"为什么退到软件渲染")。

    python tools\\probe_gpu_info.py

只读系统信息(枚举图形适配器 + 查询特性), 不碰游戏文件、不建窗口。

为什么要它: pygfx 在 import 时就 `preconfigure_default_device("pygfx",
required_features={"float32-filterable"})` —— 拿不到这个特性的适配器**根本建不出 device**。
所以"某块卡能不能用 GPU 画布"= "它支不支持 float32-filterable", 与年份/型号无直接关系。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import wgpu
except Exception as exc:                 # noqa: BLE001
    print(f"wgpu 不可用: {type(exc).__name__}: {exc}")
    sys.exit(1)

# pygfx 硬性要求的特性(见 pygfx/renderers/wgpu/engine/shared.py)
PYGFX_REQUIRED = "float32-filterable"

print(f"wgpu-py / wgpu-native : {getattr(wgpu, '__version__', '?')}")
print(f"pygfx 必需的特性      : {PYGFX_REQUIRED}")


def _features_of(adapter):
    """拿适配器支持的特性名集合(不同版本 API 名称不同)。"""
    for attr in ("features", "get_features"):
        obj = getattr(adapter, attr, None)
        if obj is None:
            continue
        try:
            val = obj() if callable(obj) else obj
        except Exception:                # noqa: BLE001
            continue
        if val:
            return {str(x) for x in val}
    return set()


adapters = wgpu.gpu.enumerate_adapters_sync() if hasattr(
    wgpu.gpu, "enumerate_adapters_sync") else []
print(f"\n找到 {len(adapters)} 个适配器:\n")

ok_any = False
for i, ad in enumerate(adapters, 1):
    info = getattr(ad, "info", {}) or {}
    summary = getattr(ad, "summary", "?")
    feats = _features_of(ad)
    has = PYGFX_REQUIRED in feats
    print(f"  [{i}] {summary}")
    print(f"      adapter_type = {info.get('adapter_type')}  "
          f"backend = {info.get('backend_type')}  "
          f"vendor_id = {info.get('vendor_id')}  device_id = {info.get('device_id')}")
    print(f"      特性数 = {len(feats)}    {PYGFX_REQUIRED} = "
          f"{'✅ 有' if has else '❌ 没有（pygfx 起不来）'}")
    ok_any = ok_any or has
    print()

print("--- 真建一次 device(逐块试, 失败不算错) ---")
for i, ad in enumerate(adapters, 1):
    summary = getattr(ad, "summary", f"#{i}")
    try:
        dev = ad.request_device_sync(required_features={PYGFX_REQUIRED})
        print(f"  [{i}] {summary} → ✅ device 建出来了")
        try:
            dev.destroy()
        except Exception:                # noqa: BLE001
            pass
    except Exception as exc:             # noqa: BLE001
        print(f"  [{i}] {summary} → ❌ {type(exc).__name__}: {str(exc)[:160]}")

print(f"\n结论: {'至少有一块可用' if ok_any else '没有一块满足 pygfx 要求 —— 只能用软件渲染'}")
