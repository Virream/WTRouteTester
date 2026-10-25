"""端到端验证: 抓取目录 → MapCalib → 回放记录归一化 → 底图像素。

不是正式测试, 只是把「闭环」跑一遍看数字对不对:
  * 战区世界坐标是否落回 map.jpg 上合理的位置(1 像素 = 1 米);
  * 载具轨迹归一化后是否落在 0~1 的战场区域内(而不是挤成一团)。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))       # tools/wrpl -> 项目根
sys.path.insert(0, ROOT)

from wt81111g import map_calib                                    # noqa: E402
from wt81111g.map_calib import load_from_capture                  # noqa: E402

CAP = os.path.join(ROOT, "回放文件", "8111抓取", "20261025_201545")


def main() -> None:
    print("== 1. 读抓取目录 ==")
    c = load_from_capture(CAP)
    print("  ", c.describe())
    print("   source:", c.source, c.captured_at)
    print("   世界矩形:", c.min_x, c.max_x, c.min_z, c.max_z)
    print("   对象种类:", c.kinds)
    assert c.valid, "校准无效"
    assert c.img_w and c.img_h, "没读到图片尺寸"
    px_per_m = c.img_w / c.span_x
    print(f"   1 像素 = {c.span_x / c.img_w:.3f} 米 (x) / {c.span_z / c.img_h:.3f} 米 (y)")

    print("\n== 2. 战区 ==")
    for z in c.zones:
        u, v = c.norm(z["x"], z["z"])
        px, py = c.pixel(z["x"], z["z"])
        print(f"   {z['label']}  世界({z['x']:8.1f}, {z['z']:8.1f})"
              f"  → 归一化({u:.4f}, {v:.4f})  → 像素({px:.0f}, {py:.0f})")
        assert 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0, "战区跑出图框了"
    assert len(c.zones) == 3, f"战区数量应为 3, 实得 {len(c.zones)}"

    print("\n== 3. 重生点 ==")
    by_kind: dict[str, int] = {}
    for s in c.spawns:
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
    print("   计数:", by_kind)
    for kind in ("tank", "fighter", "ucav", "airfield"):
        pts = [s for s in c.spawns if s["kind"] == kind and s["x"] is not None]
        if not pts:
            print(f"   {kind:8s}: 无坐标条目 {sum(1 for s in c.spawns if s['kind'] == kind)}")
            continue
        xs = [p["x"] for p in pts]
        zs = [p["z"] for p in pts]
        print(f"   {kind:8s}: {len(pts)} 个  x {min(xs):.0f}~{max(xs):.0f}"
              f"  z {min(zs):.0f}~{max(zs):.0f}")

    print("\n== 4. 往返换算 ==")
    for x, z in ((0.0, 0.0), (1024.0, 1024.0), (c.max_x, c.max_z)):
        u, v = c.norm(x, z)
        bx, bz = c.world(u, v)
        assert abs(bx - x) < 1e-6 and abs(bz - z) < 1e-6, (x, z, bx, bz)
    print("   norm/world 互逆 OK")

    print("\n== 5. 序列化 ==")
    import json
    back = map_calib.MapCalib.from_dict(json.loads(json.dumps(c.to_dict())))
    assert back.to_dict() == c.to_dict()
    print("   to_dict/from_dict 一致 OK")
    print("\n全部通过")


if __name__ == "__main__":
    main()
