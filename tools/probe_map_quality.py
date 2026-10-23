"""对比: 界面里实际看到的底图放大效果(软件层 vs GPU 层), 以及候选插值方案。

用法:
    set QT_QPA_PLATFORM=offscreen
    .venv\\Scripts\\python.exe tools\\probe_map_quality.py [zoom]

产物写到 %TEMP%:
    quality_sw_<zoom>.png   软件层真实截图
    quality_gl_<zoom>.png   GPU 层真实截图(装了 pygfx 才有)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path(os.environ.get("TEMP", "."))


def pick_map() -> bytes:
    for d in (ROOT / "dist" / "WTRouteTester" / "data" / "maps", ROOT / "data" / "maps"):
        hits = sorted(d.glob("*.jpg"), key=lambda p: -p.stat().st_size)
        if hits:
            print("map:", hits[0].name)
            return hits[0].read_bytes()
    raise SystemExit("no map image")


def main() -> None:
    zoom = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    app = QApplication(sys.argv[:1])
    data = pick_map()

    from wt81111g import route_map_view as rmv

    v = rmv.RouteMapView()
    v.resize(900, 700)
    v.set_map_bytes(data, "probe")
    v._zoom = zoom
    v._pan_x = 0.0
    v._pan_y = 0.0
    v.repaint()
    app.processEvents()
    v.grab().save(str(OUT / ("quality_sw_%d.png" % int(zoom))))
    print("software shot: %s" % (OUT / ("quality_sw_%d.png" % int(zoom))))

    try:
        from wt81111g import route_map_gl as rmg
    except Exception as exc:                                  # noqa: BLE001
        print("GL not available:", exc)
        return

    g = rmg.RouteMapGlView(offscreen=True)
    g.resize(900, 700)
    g.set_map_bytes(data, "probe")
    g._zoom = zoom
    g._pan_x = 0.0
    g._pan_y = 0.0
    g.update()
    for _ in range(4):
        g._sync_objects()
        g._sync_camera()
        g._renderer.render(g._scene, g._camera)
        app.processEvents()
    img = g.snapshot()
    h, w = img.shape[:2]
    from PyQt6.QtGui import QImage

    q = QImage(img.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888)
    q.copy().save(str(OUT / ("quality_gl_%d.png" % int(zoom))))
    print("gpu shot: %s" % (OUT / ("quality_gl_%d.png" % int(zoom))))


if __name__ == "__main__":
    main()
