"""探针: 没映射到玩家的 uid 到底是什么单位（看轨迹特征）。

判据:
* AI 防空/机场单位: 蹲在机场附近、几乎不动、开局就在、活到最后
* 玩家载具: 从重生点出发、在战区附近移动、时长通常 < 整局
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g.wrpl_reader import load_replay                          # noqa: E402


def main(path: str) -> None:
    d = load_replay(path)
    mapped = set(d.uid_pid)
    print(f"uid 总数 {len(d.units)}, 已映射 {len(mapped & set(d.units))}, "
          f"未映射 {len(set(d.units) - mapped)}\n")

    print(f"{'uid':>5} {'点':>6} {'时长':>7} {'位移':>8} {'x范围':>20} {'z范围':>20}")
    for tag, cond in (("已映射", lambda u: u in mapped),
                      ("未映射", lambda u: u not in mapped)):
        rows_sel = [(u, r) for u, r in sorted(d.units.items()) if cond(u)]
        print(f"\n===== {tag} ({len(rows_sel)} 个) =====")
        for uid, rows in rows_sel[:40]:
            xs = [r[1] for r in rows]
            zs = [r[3] for r in rows]
            dur = rows[-1][0] - rows[0][0]
            span = max(max(xs) - min(xs), max(zs) - min(zs))
            print(f"{uid:5d} {len(rows):6d} {dur:7.1f} {span:8.0f} "
                  f"[{min(xs):8.0f},{max(xs):8.0f}] [{min(zs):8.0f},{max(zs):8.0f}]")


if __name__ == "__main__":
    main(sys.argv[1])
