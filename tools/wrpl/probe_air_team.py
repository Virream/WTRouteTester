"""探针: 空战单位(ptype=2 FlightModel)能不能也绑上阵营。

现状: `build_record` 把飞机全部硬编码成 A 队(`side="A"`), 因为 ptype=2 的 uid
曾被判断为「独立 uid 空间」。这里验证 ECS 的 `aircraft+player_unit` 构造记录
(带出生组名 `tN_playerMM_K` 与型号)能不能和 ptype=2 的 uid 对上。

如果对得上 -> 飞机也能拿到 玩家名 / 阵营 / 型号 / 出现消失时刻。

用法:
    python tools/wrpl/probe_air_team.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402
from wt81111g.wrpl_entities import EcsCollector                   # noqa: E402


def main(path: str) -> None:
    print("第一遍: 常规解析(拿 ptype=2 的 air_units / 玩家 / 击杀) …", flush=True)
    data = W.load_replay(path, expect_server=None)
    print(f"  地面 uid {len(data.units)} · 空中 uid {len(data.air_units)} · "
          f"玩家 {len(data.players)}")

    print("第二遍: 带飞机一起解 ECS 实体档案 …", flush=True)
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]
    ecs = EcsCollector()
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype == 6 and e > s:
                ecs.feed(payload, s, e, ms)
        del buf, payload, packets
    res = ecs.build(known_uids=set(data.units) | set(data.air_units),
                    players=data.players, kills=data.kills,
                    include_air=True, t_end=data.t_last)
    print(f"  实体 {len(res.vehicles)} 条 · uid 偏移 {res.uid_offset} · "
          f"每组人数 K={res.group_size}")

    air = [v for v in res.vehicles if "aircraft" in v.template]
    print(f"\n=== ECS 飞机实体 {len(air)} 条 ===")
    uid_cnt = collections.Counter(v.uid for v in air)
    print(f"  互异 uid {len(uid_cnt)} 个; 与 ptype=2 的 air_units 交集: "
          f"{len(set(uid_cnt) & set(data.air_units))} 个")
    print(f"  与地面 units 的交集: {len(set(uid_cnt) & set(data.units))} 个")
    print(f"  绑到玩家(有 slot)的: {sum(1 for v in air if v.slot is not None)} 条")
    print(f"  有出生组名的: {sum(1 for v in air if v.spawn)} 条")

    print("\n=== 逐条(前 40) ===")
    print("  uid   ptype2?  slot team  玩家                     型号")
    for v in sorted(air, key=lambda x: x.uid)[:40]:
        print(f"  {v.uid:<5d} {'是' if v.uid in data.air_units else '否':<7s} "
              f"{v.slot!s:<5s} {v.team!s:<4s} {v.player[:22]:<24s} "
              f"{v.short_cls:<24s} born {v.born_t:6.1f} died "
              f"{(f'{v.died_t:6.1f}' if v.died_t is not None else '   未定')} "
              f"({v.died_by}) {v.spawn}")

    # ptype=2 的 uid 里有几个能在 ECS 里找到
    print(f"\n=== ptype=2 的 {len(data.air_units)} 个 uid 在 ECS 飞机里的命中 ===")
    hit = [u for u in data.air_units if u in uid_cnt]
    print(f"  命中 {len(hit)} / {len(data.air_units)}: {hit[:30]}")
    miss = [u for u in data.air_units if u not in uid_cnt]
    print(f"  未命中: {miss[:30]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
