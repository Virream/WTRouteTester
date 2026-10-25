"""探针: 用击杀关系验证 `tN_playerMM_K` 出生组名的前缀是不是阵营。

思路(全部来自本局回放, 不用 8111, 不用外部数据):
  1. ECS 构造块 -> uid, `unit__className`(型号), 明文 `tN_playerMM_K`(出生组名)
  2. slot 包(F02D) -> 玩家槽位 -> 名字 / 队伍
  3. kill 包(F058) -> 某时刻 killer_uid / killer_pid / victim_uid / victim_pid
     击杀是**确定事实**: 某 uid 在那一刻属于某 pid, 而 pid 有队伍。
  4. 交叉统计: spawnName 的 tN 前缀 vs 实际队伍。

如果 t1_ -> 队伍 A 全部、t2_ -> 队伍 B 全部, 那 uid 的阵营就直接解决了。

用法:
    python tools/wrpl/probe_team.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                                  # noqa: E402
from wt81111g.wrpl_events import (                                    # noqa: E402
    parse_kill_packet, parse_slot_packet)
from probe_ecs_entity import (                                        # noqa: E402
    Templates, parse_create_packet, scanned_strings, CTRL_CREATE, GROUND_NAME)

SPAWN_RE = re.compile(r"^t([12])_player(\d{2})_(\d)$")


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    T = Templates()
    rows: list[dict] = []
    stats: collections.Counter = collections.Counter()
    players: dict[int, object] = {}
    kills: list = []

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype == 6 and e > s and payload[s] in CTRL_CREATE:
                parse_create_packet(payload, s, e, ms, T, rows, stats)
                continue
            if ptype != 4 or e - s < 12:
                continue
            mid = payload[s + 2] | (payload[s + 3] << 8)
            if mid == W.SIG_SLOT:
                try:
                    for plr in parse_slot_packet(payload, s, e, ms / 1000.0):
                        old = players.get(plr.slot)
                        if old is None:
                            players[plr.slot] = plr
                        else:
                            for f in ("player_id", "name", "clan_tag", "team"):
                                v = getattr(plr, f)
                                if v:
                                    setattr(old, f, v)
                except (ValueError, EOFError, IndexError):
                    pass
            elif mid == W.SIG_KILL:
                try:
                    ev = parse_kill_packet(payload, s, e, ms / 1000.0)
                except (ValueError, EOFError, IndexError):
                    continue
                if ev is not None:
                    kills.append(ev)
        del buf, payload, packets

    print(f"构造 {len(rows)} 条; 玩家槽位 {len(players)} 个; 击杀 {len(kills)} 条")
    print("\n=== 玩家槽位 ===")
    for slot in sorted(players):
        p = players[slot]
        print(f"  slot={slot:<3d} pid={getattr(p, 'player_id', None)!s:<10s} "
              f"team={getattr(p, 'team', None)!s:<4s} {getattr(p, 'name', '')}")

    # ---- tank 块: uid / 型号 / 出生组名
    urel = 28
    ents: dict[int, dict] = {}
    for r in rows:
        if "tank" not in r["name"]:
            continue
        blk, vs = r["block"], r["vstart"]
        if vs + urel + 2 > len(blk):
            continue
        uid = (blk[vs + urel] | (blk[vs + urel + 1] << 8)) & 0x7FF
        spawn = None
        cls = None
        for _o, s in scanned_strings(blk, 3, 60):
            m = SPAWN_RE.match(s)
            if m and spawn is None:
                spawn = s
            elif GROUND_NAME.match(s) and cls is None:
                cls = s
        e = ents.setdefault(uid, {"cls": cls, "spawn": spawn, "tick": r["tick"]})
        e["cls"] = e["cls"] or cls
        e["spawn"] = e["spawn"] or spawn

    print(f"\n=== 地面载具实体 {len(ents)} 个 uid ===")
    print("  有出生组名:", sum(1 for e in ents.values() if e["spawn"]),
          " 有型号:", sum(1 for e in ents.values() if e["cls"]))

    # ---- 击杀把 uid 绑到 pid(带时刻)
    # 只用 **killer_uid -> killer_pid**: 击杀者一定是那个 uid 当时的拥有者;
    # victim_uid 是被杀的载具, 它属于**对方**玩家, 混进来会污染归属。
    uid_pid: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for k in kills:
        if k.killer_uid != W.EXT_MASK_UID and k.killer_pid:
            uid_pid[k.killer_uid][k.killer_pid] += 1

    pid2slot = {getattr(p, "player_id", None): s for s, p in players.items()}

    def slot_from_spawn(sp: str) -> int | None:
        """出生组名 -> 玩家槽位。t1 组占 slot 0..15, t2 组占 slot 16..31。"""
        m = SPAWN_RE.match(sp)
        if not m:
            return None
        tn, mm = int(m.group(1)), int(m.group(2))
        return mm - 1 if tn == 1 else mm + 15

    # ---- 公式 vs 击杀事实
    agree = disagree = 0
    detail: list[tuple] = []
    for uid, e in sorted(ents.items()):
        sp = e["spawn"]
        if not sp:
            continue
        slot = slot_from_spawn(sp)
        p = players.get(slot) if slot is not None else None
        team = getattr(p, "team", None)
        name = getattr(p, "name", "")
        cand = uid_pid.get(uid)
        # kill 包里的 Killer/VictimPid 就是**玩家槽位号**(与 slot 包同空间)
        kslot = cand.most_common(1)[0][0] if cand else None
        if kslot is not None:
            if kslot == slot:
                agree += 1
            else:
                disagree += 1
            detail.append((uid, sp, slot, kslot, team, name, e["cls"]))
        else:
            detail.append((uid, sp, slot, None, team, name, e["cls"]))

    tot = agree + disagree
    print("\n=== 公式(出生组->槽位) vs 击杀事实 ===")
    print(f"  可对比 {tot} 个 uid: 一致 {agree}, 不一致 {disagree}"
          f" = {100.0 * agree / tot if tot else 0:.1f}%")

    print("\n=== 逐条(前 70) ===")
    print("  uid    出生组名         公式slot  击杀slot team  玩家名                    型号")
    for uid, sp, slot, kslot, team, name, cls in detail[:70]:
        flag = "" if kslot is None or kslot == slot else "  <<< 不一致"
        print(f"  {uid:<5d} {sp:<16s} {slot!s:<8s} {kslot!s:<8s} {team!s:<5s} "
              f"{name[:22]:<24s} {(cls or '-').split('/')[-1]}{flag}")

    # ---- 最终: 每个 uid 的阵营(直接用公式, 不需要击杀)
    print("\n=== uid -> 阵营(纯公式) ===")
    cnt = collections.Counter()
    for uid, e in sorted(ents.items()):
        slot = slot_from_spawn(e["spawn"] or "")
        p = players.get(slot) if slot is not None else None
        cnt[(getattr(p, "team", None), slot is not None)] += 1
    for (team, ok), n in sorted(cnt.items(), key=lambda kv: str(kv[0])):
        print(f"  team={team!s:<5s} 绑定成功={ok}: {n} 个 uid")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
