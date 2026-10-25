"""探针: 无人侦察机(UCAV)到底属于哪一队。

用户 2026-10-25: 游戏回放里能看见红色/蓝色的 UCAV, 说明它一定带阵营。
ECS 的 `aircraft+player_unit` 里 `ucav_recon_micro_flir` **没有出生组名**
(`tN_playerMM_K`), 所以公式用不上。这里有两条线索:

  A. **击杀/伤害包**: UCAV 被击落时 `victim_uid` 就是它, 而 `victim_pid` 是击杀者?
     不对 —— victim_pid 是**受害者**的玩家槽位, 正好就是放飞 UCAV 的那个人。
     同理 UCAV 自己击杀时 `killer_uid` 也能给出主人。
  B. **ECS 块里的阵营字段**: 拿「已知 team 的坦克」做对照, 逐偏移扫 u16/u32,
     找哪个偏移的值与 team 一一对应; 找到后直接读 UCAV 块。

用法:
    python tools/wrpl/probe_ucav.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402
from wt81111g.wrpl_entities import EcsCollector, UID_BITS         # noqa: E402


def main(path: str) -> None:
    print("解析回放 …", flush=True)
    data = W.load_replay(path, expect_server=None)
    names = {p.slot: (getattr(p, "display", "") or getattr(p, "name", ""))
             for p in data.players.values()}
    teams = {p.slot: getattr(p, "team", None) for p in data.players.values()}

    # ---- 线索 A: 击杀/伤害包
    print("\n=== 空中实体清单 ===")
    air = getattr(data, "air_vehicles", ())
    print(f"air_vehicles {len(air)} 条; air_units(pty2) {len(data.air_units)} 个 uid")
    ucav = [v for v in air if "ucav" in (v.cls or "")]
    print(f"其中 UCAV {len(ucav)} 条")

    # 击杀包: killer_uid / victim_uid 都可能指向 UCAV
    hits: dict[int, list[str]] = collections.defaultdict(list)
    for ev in data.kills:
        for role, u, pid in (("killer", ev.killer_uid, ev.killer_pid),
                             ("victim", ev.victim_uid, ev.victim_pid)):
            if u == W.EXT_MASK_UID:
                continue
            hits[u].append(f"{role} pid={pid} team={teams.get(pid)} "
                           f"@ {ev.t:.1f}s")

    print("\n=== UCAV: 击杀/伤害证据 ===")
    for v in sorted(ucav, key=lambda x: x.uid):
        print(f"\n  uid={v.uid}  {v.short_cls}  born {v.born_t:.1f} "
              f"died {v.died_t if v.died_t is None else round(v.died_t, 1)} "
              f"({v.died_by})  eid=0x{v.eid:x}")
        print(f"    ptype=2 里有: {'是' if v.uid in data.air_units else '否'}")
        print(f"    uid_pid(击杀/伤害推出): {data.uid_pid.get(v.uid)} "
              f"-> team {teams.get(data.uid_pid.get(v.uid))}")
        for line in hits.get(v.uid, ())[:6]:
            print(f"    事件: {line}")
        if not hits.get(v.uid):
            print("    （没有击杀/伤害事件）")

    # ---- 线索 B: 找「阵营字段」
    print("\n=== 线索 B: 用已知 team 的坦克标定阵营字段 ===")
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
    res = ecs.build(known_uids=set(data.units), players=data.players,
                    kills=data.kills, include_air=True, t_end=data.t_last)

    by_eid = {c.eid: c for c in ecs._creations}
    known = []      # (block, vstart, team)
    for v in res.vehicles:
        if "aircraft" in v.template or v.team is None:
            continue
        c = by_eid.get(v.eid)
        if c is not None:
            known.append((c.block, c.vstart, v.team))
    print(f"  已知阵营的地面实体 {len(known)} 条 (team1 "
          f"{sum(1 for _b, _v, t in known if t == 1)} / team2 "
          f"{sum(1 for _b, _v, t in known if t == 2)})")

    best = []
    for width, name in ((2, "u16"), (4, "u32")):
        for rel in range(0, 120):
            m: dict[int, set[int]] = collections.defaultdict(set)
            ok = True
            for blk, vs, team in known:
                o = vs + rel
                if o + width > len(blk):
                    ok = False
                    break
                val = int.from_bytes(blk[o:o + width], "little")
                m[val].add(team)
            if not ok:
                break
            # 每个值只能对应一个 team, 且两个 team 都要出现
            if any(len(v) > 1 for v in m.values()):
                continue
            if len({t for s in m.values() for t in s}) < 2:
                continue
            best.append((rel, name, dict(sorted(m.items())[:6]), len(m)))
    for rel, name, sample, nval in best[:12]:
        print(f"  +{rel:<4d} {name}: 命中(值->team) {sample} … 共 {nval} 个值")

    # 用找到的偏移看 UCAV
    ucav_eids = {(v.uid, v.eid) for v in res.vehicles if "ucav" in (v.cls or "")}
    print(f"\n=== 用候选偏移读 UCAV 块 ({len(ucav_eids)} 条) ===")
    for v in res.vehicles:
        if "ucav" not in (v.cls or ""):
            continue
        c = by_eid.get(v.eid)
        if c is None:
            continue
        blk, vs = c.block, c.vstart
        vals = []
        for rel, name, _s, _n in best[:12]:
            width = 2 if name == "u16" else 4
            o = vs + rel
            if o + width <= len(blk):
                vals.append((rel, int.from_bytes(blk[o:o + width], "little")))
        print(f"  uid={v.uid:<5d} eid=0x{v.eid:x} 值: {vals}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
