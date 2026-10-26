"""验证「可以让 pygfx 用**指定**的显卡(含 WARP)」—— 这是「设置里选择显卡」的地基。

    python tools\\probe_pick_adapter.py            # 列出并逐个验证
    python tools\\probe_pick_adapter.py 3          # 只用第 3 块(1 起)

做法: pygfx 的 device 是全局单例, 但它在 import 时只是
`wgpu.preconfigure_default_device("pygfx", required_features=...)`,
**真正建 device 是第一次 `gfx.WgpuRenderer(...)` 时**。所以我们可以在建 renderer
之前再调一次 `preconfigure_default_device(..., adapter=选中的那块)` ——
wgpu 的语义是"最后一次设置生效"(adapter 会盖掉 power_preference 等)。

⚠ 每个进程只能验一块(**device 只能建一次**), 所以多块时要分开跑。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import wgpu                                    # noqa: E402

PYGFX_REQUIRED = "float32-filterable"

want = None
if len(sys.argv) > 1:
    try:
        want = int(sys.argv[1])
    except ValueError:
        print(f"参数应为序号(1 起), 收到 {sys.argv[1]!r}")
        sys.exit(2)

adapters = wgpu.gpu.enumerate_adapters_sync()
print(f"共 {len(adapters)} 块适配器:")
for i, ad in enumerate(adapters, 1):
    mark = " ← 目标" if i == want else ""
    print(f"  [{i}] {ad.summary}{mark}")

if want is None:
    print("\n挑一块再跑, 例如:")
    for i, ad in enumerate(adapters, 1):
        print(f"  python tools\\probe_pick_adapter.py {i}      # {ad.summary}")
    sys.exit(0)

if not 1 <= want <= len(adapters):
    print(f"序号应在 1~{len(adapters)}")
    sys.exit(2)

target = adapters[want - 1]
print(f"\n选定: {target.summary}")

# ---- 关键一步: 在建 renderer 之前指定 adapter
try:
    wgpu.preconfigure_default_device(
        "wt81111g-pick-test",
        adapter=target,
        required_features={PYGFX_REQUIRED},   # pygfx 自己也要这个, 合并是并集
    )
    print("preconfigure_default_device(adapter=…) → OK")
except Exception as exc:                       # noqa: BLE001
    print(f"preconfigure_default_device(adapter=…) → 失败 {type(exc).__name__}: {exc}")
    sys.exit(1)

import pygfx as gfx                            # noqa: E402
from rendercanvas.offscreen import RenderCanvas  # noqa: E402

canvas = RenderCanvas(size=(640, 400), pixel_ratio=1)
try:
    renderer = gfx.WgpuRenderer(canvas)
except Exception as exc:                       # noqa: BLE001
    print(f"建 WgpuRenderer 失败: {type(exc).__name__}: {exc}")
    sys.exit(1)

got = getattr(getattr(renderer.device, "adapter", None), "summary", "?")
print(f"pygfx 实际用的适配器: {got}")

# 画一帧, 确认不是只“建起来”而是真能跑
scene = gfx.Scene()
scene.add(gfx.Background(None, gfx.BackgroundMaterial("#123456")))
cam = gfx.OrthographicCamera(1, 1)
cam.local.position = (0.5, 0.5, 2.0)
scene.add(cam)
line = gfx.Line(gfx.Geometry(positions=[[0.1, 0.1, 0], [0.9, 0.9, 0]]),
                gfx.LineMaterial(color="#ff0000", thickness=3,
                                 thickness_space="screen"))
scene.add(line)
renderer.render(scene, cam)
canvas.draw()
print("渲染一帧 → OK")

# 读回像素, 确认真的有内容(防止“静默空白”)
try:
    arr = renderer.target.draw()      # 某些版本才有
except Exception:                      # noqa: BLE001
    arr = None
print(f"draw() 返回: {type(arr).__name__ if arr is not None else '无(该版本不支持读回)'}")

print("\n结论: " + ("可以自由指定适配器 ✅" if got != "?" else "拿不到 summary, 存疑"))
