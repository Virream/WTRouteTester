"""验证: 同一张地图的**不同子模式**是否共用同一套世界坐标 / 同一份校准。

判据(不依赖任何猜测):
  * 8111 的 `map_min`/`map_max` 是**地图图片覆盖的世界矩形**, 图片按关卡名缓存;
  * 如果两种模式的回放里, **地面单位(排除机场 AI)的坐标分布落在同一个世界坐标
    区间**, 说明世界坐标系没变 → `map_min/map_max` 也没变 → 一张图校准一次就够;
  * 反过来若整体平移/缩放, 就必须逐个子模式校准。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                    # noqa: E402

BASE = os.path.join(ROOT, "回放文件")
CASES = [
    ("三点图(24 分片)", os.path.join(BASE, "服务器")),
    ("单点图", os.path.join(BASE, "服务器", "芬兰单点模式")),
]


def pct(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    for label, path in CASES:
        if not os.path.exists(path):
            print(f"{label}: 缺 {path}")
            continue
        print(f"\n===== {label} =====")
        data = load_replay(path)
        print(f"  level={data.level}  battle={data.battle_type}")
        # 只取玩家地面载具(ECS 有玩家绑定的), 排除机场 AI
        ground = [v for v in getattr(data, "vehicles", ())
                  if v.player and v.team and "aircraft" not in v.template]
        print(f"  玩家地面载具 {len(ground)} 台")
        xs, zs = [], []
        for v in ground:
            rows = data.units.get(v.uid) or []
            for t, x, _y, z in rows:
                if v.born_t - 2 <= t <= (v.died_t + 2 if v.died_t else 1e9):
                    xs.append(x)
                    zs.append(z)
        if not xs:
            print("  没有样本")
            continue
        print(f"  x p0.1% {pct(xs, 0.001):9.1f}  p50 {pct(xs, 0.5):9.1f}"
              f"  p99.9% {pct(xs, 0.999):9.1f}   min {min(xs):9.1f} max {max(xs):9.1f}")
        print(f"  z p0.1% {pct(zs, 0.001):9.1f}  p50 {pct(zs, 0.5):9.1f}"
              f"  p99.9% {pct(zs, 0.999):9.1f}   min {min(zs):9.1f} max {max(zs):9.1f}")
        # 坦克重生点簇: 首点应聚在 8111 报的 tank spawn 附近(实测 x 1051~1194 三点图)
        firsts = []
        for v in ground:
            rows = data.units.get(v.uid) or []
            if rows:
                firsts.append((v.born_t, rows[0][1], rows[0][3]))
        firsts.sort()
        print("  最早重生的 5 台车辆首点:")
        for bt, x, z in firsts[:5]:
            print(f"    t={bt:7.1f}s  ({x:9.1f}, {z:9.1f})")


if __name__ == "__main__":
    main()
