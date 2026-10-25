"""探针: 端到端验证「载具档案」—— 玩家 / 阵营 / 型号 / 出现与消失 / 生存时长。

走的是正式链路（`load_replay` -> `build_record`），不是另写一套解析。

用法:
    python tools/wrpl/probe_entities.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from wt81111g import wrpl_reader as W                                  # noqa: E402
from wt81111g.replay_import import build_record                       # noqa: E402


def main(path: str) -> None:
    data = W.load_replay(path, expect_server=None)
    print(f"地图 {data.level} / {data.battle_type} · 分片 {data.parts} · "
          f"包 {data.packets:,}")
    print(f"地面 uid {len(data.units)} 个 · 样本 {data.sample_count:,}")
    print(f"玩家 {len(data.players)} 位 · 击杀 {len(data.kills)} 条")
    print(f"ECS: 载具 {len(data.vehicles)} 个 · 每组人数 K={data.ecs_group_size} · "
          f"uid 偏移 +{data.ecs_uid_offset}")
    print(f"ECS 模板: {data.ecs_templates}")

    vans = data.vehicles
    if not vans:
        print("\n!! ECS 没有解出任何载具")
        return

    print("\n=== 绑定质量 ===")
    print("  " + str(dict(collections.Counter(v.bind for v in vans))))
    print("  " + str(dict(collections.Counter(
        f"team{v.team}" for v in vans))))

    # ---- 按玩家 / 阵营分组
    by_player: dict[tuple, list] = collections.defaultdict(list)
    for v in vans:
        by_player[(v.team, v.player or "?", v.slot)].append(v)

    print(f"\n=== 按玩家分组（{len(by_player)} 人）===")
    for (team, player, slot), lst in sorted(
            by_player.items(), key=lambda kv: (kv[0][0] or 9, -len(kv[1]))):
        lst.sort(key=lambda v: v.born_t)
        alive = sum(((v.died_t if v.died_t is not None else 0.0) - v.born_t)
                    for v in lst)
        print(f"  [{'A' if team == 1 else 'B'}] slot={slot!s:<4s} {str(player)[:16]:<18s}"
              f" 载具 {len(lst)} 台  累计存活 {alive:7.0f}s")
        for v in lst:
            end = f"{v.died_t:7.1f}s {v.died_by:<4s}" if v.died_t is not None else "     ?     "
            print(f"       uid={v.uid:<5d} {v.born_t:7.1f}s -> {end}  "
                  f"{v.short_cls:<28s} bind={v.bind}")

    # ---- 走一遍 build_record, 看线路侧
    rec, stats = build_record(data, path if os.path.isfile(path) else "")
    print("\n=== build_record ===")
    print(f"  线路 A {len(rec.sides['A'])} 条 / B {len(rec.sides['B'])} 条")
    print(f"  有玩家名 {stats.mapped} 条 · 判死 {stats.deaths} 条 · "
          f"丢弃 {stats.dropped} 条")
    print(f"  ECS: 载具 {stats.ecs_vehicles} · 绑定 {stats.ecs_bound} · "
          f"K={stats.ecs_group}")
    print("\n  --- B 队前 12 条 ---")
    for life in rec.sides["B"][:12]:
        print(f"    {life.life_id:<22s} {life.icon[:34]:<36s} "
              f"{life.t0:7.1f}->{life.t1:7.1f}s  dead={life.dead}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
