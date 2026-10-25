"""位置流里有没有「一个 ECS 档案都没有」的 uid?

如果有, 这些 uid 就没法用存活窗口裁剪, 只能靠启发式(残骸/位移)。
没有的话, `strip_corpses` 可以整块删掉 —— 少一层猜测。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                    # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    for label, path in (
        ("三点图", DEFAULT),
        ("单点图", os.path.join(DEFAULT, "芬兰单点模式")),
        ("本地", os.path.join(ROOT, "回放文件", "本地", "#2026.10.24 22.45.13.wrpl")),
    ):
        if not os.path.exists(path):
            continue
        data = load_replay(path)
        have = {v.uid for v in list(data.vehicles) + list(data.air_vehicles)}
        orphan = sorted(u for u in data.units if u not in have)
        orphan_air = sorted(u for u in data.air_units if u not in have)
        pts = sum(len(data.units[u]) for u in orphan)
        print(f"{label}: 地面 uid {len(data.units)} 个, 无档案 {len(orphan)} 个"
              f" ({pts} 个采样)  {orphan[:12]}")
        print(f"        空中 uid {len(data.air_units)} 个, 无档案 {len(orphan_air)} 个"
              f"  {orphan_air[:12]}")


if __name__ == "__main__":
    main()
