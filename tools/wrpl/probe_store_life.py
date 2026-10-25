"""直接读用户当前 data\\routes.json, 把指定 life_id 找出来(不重新解析回放)。

用法: python tools\\wrpl\\probe_store_life.py <A26_2>
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.route_store import RouteStore                      # noqa: E402

CANDIDATES = [
    os.path.join(ROOT, "dist", "WTRouteTester", "data", "routes.json"),
    os.path.join(ROOT, "data", "routes.json"),
]


def main() -> None:
    want = sys.argv[1] if len(sys.argv) > 1 else "A26_2"
    path = next((p for p in CANDIDATES if os.path.exists(p)), None)
    if path is None:
        print("找不到 routes.json")
        return
    print("读到:", path, f"{os.path.getsize(path) / 1024:.0f} KB")
    store = RouteStore(path)
    print(f"全局条目 {len(store.globals)} 个, 地图 {len(store.known_maps)} 个\n")
    for rec in store.globals:
        hits = []
        for side in ("A", "B"):
            for lf in rec.lives(side):
                if want in lf.life_id:
                    hits.append(lf)
        print(f"[{rec.route_id}] {rec.map_key}  {rec.map_name}"
              f"  {rec.created_at}  {rec.count()} 条  norm_rect={rec.norm_rect}")
        if not hits:
            continue
        for lf in hits:
            print(f"    {lf.life_id}")
            print(f"      icon={lf.icon}  player={lf.player}  cls={lf.vehicle_cls}")
            print(f"      {len(lf.points)} 点  t {lf.points[0].t:.1f}~{lf.t1:.1f}s"
                  f"  dead={lf.dead} death_t={lf.death_t:.1f}")
            print(f"      首 ({lf.points[0].x:.4f}, {lf.points[0].y:.4f})"
                  f"  末 ({lf.points[-1].x:.4f}, {lf.points[-1].y:.4f})")
        # 同一玩家的全部线路
        who = hits[0].player
        print(f"    —— 玩家「{who}」的全部线路 ——")
        for side in ("A", "B"):
            for lf in rec.lives(side):
                if lf.player != who:
                    continue
                d = lf.t1 - lf.points[0].t
                print(f"      {lf.life_id:22s} {len(lf.points):5d} 点"
                      f"  t {lf.points[0].t:7.1f}~{lf.t1:7.1f}s ({d:6.1f}s)"
                      f"  dead={lf.dead}  {lf.icon[:34]}")


if __name__ == "__main__":
    main()
