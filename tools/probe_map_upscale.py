"""对比: 地图底图放大到高倍时, 几种插值方式的效果与耗时。

用法(控制台是 GBK, 所以打印只用 ASCII):
    set QT_QPA_PLATFORM=offscreen
    .venv\\Scripts\\python.exe tools\\probe_map_upscale.py

产物写到 %TEMP%\\upscale_*.png:
    - 每个面板: 源图取 160x160 的一块, 放大 4 倍 → 640x640
    - grid.png: 2x3 拼图, 顺序 = nearest / bilinear / catmull / bspline / lanczos3 / area??
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
from PyQt6.QtGui import QGuiApplication, QImage

RECT = (320, 300, 160, 160)      # 源图裁切区域 (x, y, w, h)
SCALE = 4                        # 放大倍数(对应界面上 MAX_ZOOM 附近)


def _argf(i: int, default: float) -> float:
    try:
        return float(sys.argv[i])
    except (IndexError, ValueError):
        return default


RECT = (320, 300, int(_argf(1, 160)), int(_argf(1, 160)))
SCALE = _argf(2, 4.0)
OUT_DIR = Path(os.environ.get("TEMP", "."))


def load_rgb(path: Path) -> np.ndarray:
    img = QImage(str(path))
    if img.isNull():
        raise SystemExit("cannot load %s" % path)
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    bpl = img.bytesPerLine()
    arr = np.frombuffer(ptr, np.uint8).reshape((h, bpl))[:, : w * 3]
    return arr.reshape(h, w, 3).astype(np.float32) / 255.0


def save_rgb(arr: np.ndarray, path: Path) -> None:
    a = np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)
    h, w, _ = a.shape
    img = QImage(a.tobytes(), w, h, w * 3, QImage.Format.Format_RGB888)
    img.copy().save(str(path))


# ----------------------------------------------------------------- 分离式重采样
def _taps(kernel: str, n_out: int, n_in: int, scale: float) -> tuple[np.ndarray, np.ndarray]:
    """返回 (索引 (n_out, k), 权重 (n_out, k)) —— 输出像素 i 对应源坐标 (i+0.5)/scale-0.5。"""
    if kernel == "nearest":
        idx = np.clip(np.floor((np.arange(n_out) + 0.5) / scale).astype(int), 0, n_in - 1)
        return idx[:, None], np.ones((n_out, 1), np.float32)

    if kernel in ("bilinear", "catmull", "bspline"):
        radius = 1.0 if kernel == "bilinear" else 2.0
        k = 2 if kernel == "bilinear" else 4
    elif kernel == "lanczos2":
        radius, k = 2.0, 4
    elif kernel == "lanczos3":
        radius, k = 3.0, 6
    else:
        raise ValueError(kernel)

    centers = (np.arange(n_out) + 0.5) / scale - 0.5
    base = np.floor(centers).astype(int)
    if k == 2:
        offs = np.array([0, 1])
    elif k == 4:
        offs = np.array([-1, 0, 1, 2])
    else:
        offs = np.array([-2, -1, 0, 1, 2, 3])
    t = centers[:, None] - (base[:, None] + offs[None, :])      # 距离(源像素单位)

    if kernel == "bilinear":
        w = np.clip(1.0 - np.abs(t), 0.0, None)
    elif kernel == "catmull":
        a = -0.5
        at = np.abs(t)
        w = np.where(at < 1, (a + 2) * at ** 3 - (a + 3) * at ** 2 + 1,
                     np.where(at < 2, a * at ** 3 - 5 * a * at ** 2 + 8 * a * at - 4 * a, 0.0))
    elif kernel == "bspline":                       # 三次 B 样条(最平滑, 略糊)
        at = np.abs(t)
        w = np.where(at < 1, (4 - 6 * at ** 2 + 3 * at ** 3) / 6,
                     np.where(at < 2, (2 - at) ** 3 / 6, 0.0))
    elif kernel in ("lanczos2", "lanczos3"):
        at = np.abs(t)
        n = 2.0 if kernel == "lanczos2" else 3.0

        def sinc(x):
            x = np.where(x == 0, 1e-9, x)
            return np.sin(np.pi * x) / (np.pi * x)

        w = np.where(at < n, sinc(at) * sinc(at / n), 0.0)
    s = w.sum(axis=1, keepdims=True)
    w = w / np.where(s == 0, 1.0, s)
    idx = np.clip(base[:, None] + offs[None, :], 0, n_in - 1)
    return idx, w.astype(np.float32)


def upscale(src: np.ndarray, out_w: int, out_h: int, kernel: str) -> np.ndarray:
    ih, iw, ch = src.shape
    # “先预滤波再双线性”: 等价于硬件 mipmap 降噪(把像素级噪声抹平, 高倍时不再一格一格)
    for tag, f in (("pre2_", 2), ("pre3_", 3), ("pre4_", 4)):
        if kernel.startswith(tag):
            kernel = kernel[len(tag):]
            h2, w2 = (ih // f) * f, (iw // f) * f
            small = src[:h2, :w2].reshape(h2 // f, f, w2 // f, f, ch).mean(axis=(1, 3))
            return upscale(small.astype(np.float32), out_w, out_h, kernel)
    if kernel.startswith("gauss"):           # gauss06_bilinear / gauss10_bilinear
        sigma = float(kernel.split("_")[0][5:]) / 10.0
        kernel = kernel.split("_", 1)[1]
        r = max(1, int(round(sigma * 3)))
        xs = np.arange(-r, r + 1, dtype=np.float32)
        k1 = np.exp(-(xs ** 2) / (2 * sigma * sigma))
        k1 /= k1.sum()
        pad = np.pad(src, ((0, 0), (r, r), (0, 0)), mode="edge")
        for j, wgt in enumerate(k1):
            if j == 0:
                tmp = pad[:, j:j + iw, :] * wgt
            else:
                tmp += pad[:, j:j + iw, :] * wgt
        pad = np.pad(tmp, ((r, r), (0, 0), (0, 0)), mode="edge")
        blur = np.zeros_like(src)
        for j, wgt in enumerate(k1):
            blur += pad[j:j + ih, :, :] * wgt
        return upscale(blur.astype(np.float32), out_w, out_h, kernel)

    sx, sy = out_w / iw, out_h / ih
    ix, wx = _taps(kernel, out_w, iw, sx)          # (out_w, k)
    iy, wy = _taps(kernel, out_h, ih, sy)
    tmp = np.zeros((ih, out_w, ch), np.float32)
    for j in range(ix.shape[1]):                   # 先横向
        tmp += src[:, ix[:, j], :] * wx[None, :, j][..., None]
    out = np.zeros((out_h, out_w, ch), np.float32)
    for j in range(iy.shape[1]):                   # 再纵向
        out += tmp[iy[:, j], :, :] * wy[None, :, j][..., None]
    return np.clip(out, 0.0, 1.0)


def blockiness(a: np.ndarray) -> float:
    """方块感指标: 相邻像素二阶差分的平均绝对值的**倒数量级**(越大越"块")。"""
    g = a.mean(axis=2)
    d2 = np.abs(np.diff(g, 2, axis=1)).mean() + np.abs(np.diff(g, 2, axis=0)).mean()
    return float(d2)


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    root = Path(__file__).resolve().parent.parent
    maps = sorted((root / "dist" / "WTRouteTester" / "data" / "maps").glob("*.jpg"))
    if not maps:
        maps = sorted((root / "data" / "maps").glob("*.jpg"))
    if not maps:
        raise SystemExit("no map image found")
    full = load_rgb(maps[0])
    print("src map: %s  %dx%d" % (maps[0].name, full.shape[1], full.shape[0]))

    x, y, w, h = RECT
    w, h = min(w, full.shape[1] - x), min(h, full.shape[0] - y)
    src = full[y:y + h, x:x + w]
    out_w, out_h = int(w * SCALE), int(h * SCALE)

    kernels = ["nearest", "bilinear", "bspline", "pre2_bilinear", "pre3_bilinear",
               "gauss06_bilinear", "gauss12_bilinear", "catmull"]
    panels = []
    for k in kernels:
        t0 = time.perf_counter()
        out = upscale(src, out_w, out_h, k)
        dt = (time.perf_counter() - t0) * 1000.0
        # 稳态耗时(算第二遍, 排除首次 numpy 缓冲分配)
        t0 = time.perf_counter()
        out = upscale(src, out_w, out_h, k)
        dt2 = (time.perf_counter() - t0) * 1000.0
        save_rgb(out, OUT_DIR / ("upscale_%s.png" % k))
        print("%-9s %7.2f ms (again %6.2f)  blockiness=%.4f"
              % (k, dt, dt2, blockiness(out)))
        panels.append(out)

    # 拼图: 2 列 x 4 行
    pad = 6
    ph, pw, _ = panels[0].shape
    rows = (len(panels) + 1) // 2
    grid = np.ones((rows * ph + (rows + 1) * pad, 2 * pw + 3 * pad, 3), np.float32) * 0.15
    for i, p in enumerate(panels):
        r, c = divmod(i, 2)
        y0 = pad + r * (ph + pad)
        x0 = pad + c * (pw + pad)
        grid[y0:y0 + ph, x0:x0 + pw] = p
    save_rgb(grid, OUT_DIR / "upscale_grid.png")
    print("order:", " ".join("%s(%d,%d)" % (k, i // 2, i % 2) for i, k in enumerate(kernels)))
    print("saved to", OUT_DIR)


if __name__ == "__main__":
    main()
