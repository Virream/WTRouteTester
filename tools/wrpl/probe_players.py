"""探针: 验证 Slot / Kill 包解析（玩家名、队伍、击杀）。

用法: python tools/wrpl/probe_players.py <回放文件或分片目录>
"""
from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_events as E                                 # noqa: E402
from wt81111g import wrpl_reader as W                                  # noqa: E402


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    players: dict[int, E.PlayerSlot] = {}
    kills: list[E.KillEvent] = []
    sigs: collections.Counter = collections.Counter()
    n_slot = n_kill = 0
    bad_slot = 0

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != W.PACKET_MPI or e - s < 12:
                continue
            mid = payload[s + 2] | (payload[s + 3] << 8)
            if mid == E.SIG_SLOT:
                n_slot += 1
                try:
                    for plr in E.parse_slot_packet(payload, s, e, ms / 1000):
                        old = players.get(plr.slot)
                        if old is None:
                            players[plr.slot] = plr
                        else:                     # 增量合并（后面的包带不同字段）
                            for f in ("player_id", "name", "clan_tag", "title",
                                      "team", "real_nick"):
                                v = getattr(plr, f)
                                if v:
                                    setattr(old, f, v)
                except Exception as exc:          # noqa: BLE001
                    bad_slot += 1
                    if bad_slot <= 3:
                        print(f"  slot 解析失败 t={ms/1000:.1f}s: {exc}")
            elif mid == E.SIG_KILL:
                n_kill += 1
                try:
                    ev = E.parse_kill_packet(payload, s, e, ms / 1000)
                    if ev:
                        kills.append(ev)
                except Exception as exc:          # noqa: BLE001
                    if n_kill <= 3:
                        print(f"  kill 解析失败 t={ms/1000:.1f}s: {exc}")

    print(f"Slot 包 {n_slot:,} 个（失败 {bad_slot}）, Kill 包 {n_kill:,} 个")
    real = [p for p in players.values() if p.name or p.player_id]
    print(f"\n玩家 {len(real)} 个:")
    for p in sorted(real, key=lambda x: x.slot):
        print(f"  槽{p.slot:3d}  team={p.team}  pid={p.player_id:<12} "
              f"{p.display!r}  realNick={p.real_nick!r} title={p.title!r}")

    print(f"\n击杀 {len(kills)} 条:")
    for k in kills[:25]:
        print(f"  t={k.t:7.1f}s  杀手pid={k.killer_pid:<11} uid={k.killer_uid:4d} "
              f"{k.vehicle!r} {k.weapon!r} → 受害pid={k.victim_pid:<11} "
              f"uid={k.victim_uid:4d} {k.destroyed!r}")


if __name__ == "__main__":
    main(sys.argv[1])
