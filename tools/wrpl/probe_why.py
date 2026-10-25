"""查: 这 4 个玩家在 uid_pid 里已经映射到哪些载具? 为什么他们晚期的车没映射?"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g.wrpl_reader import load_replay                          # noqa: E402

# 用户从游戏里比对出来的真实身份
TARGETS = {
    165: ("想当教皇的sensei", "T-80U", "A69"),
    177: ("南瓜大菠萝", "M1128 狼群", "A67"),
    182: ("Chiang Kai-shek1", "M1A2 SEP/战利品-HV", "A63"),
    183: ("_299727614", "豹2 PSO", "A68"),
}


def main(path: str) -> None:
    d = load_replay(path)
    by_slot = {p.slot: p for p in d.players.values()}
    slot_of_name: dict[str, int] = {}
    for p in d.players.values():
        slot_of_name[p.name] = p.slot

    print("=== 目标玩家在 Slot 表里的槽位 ===")
    for uid, (name, veh, line) in TARGETS.items():
        slot = slot_of_name.get(name)
        print(f"  uid={uid:4d} ({line})  {name:22s} → slot={slot}  "
              f"真实载具={veh}")

    print("\n=== 每个槽位已映射到哪些 uid ===")
    for uid, (name, veh, line) in TARGETS.items():
        slot = slot_of_name.get(name)
        if slot is None:
            print(f"  {name}: Slot 表里没有")
            continue
        mine = sorted(u for u, pid in d.uid_pid.items() if pid == slot)
        print(f"\n  {name} (slot {slot}) → 已映射 uid {mine}")
        for u in mine:
            rows = d.units.get(u, [])
            if rows:
                print(f"      uid={u:4d} 点={len(rows):6d} "
                      f"t={rows[0][0]:6.1f}~{rows[-1][0]:6.1f}s")
        for u in [uid]:
            rows = d.units.get(u, [])
            print(f"      ⚠ 未映射 uid={u:4d} 点={len(rows):6d} "
                  f"t={rows[0][0]:6.1f}~{rows[-1][0]:6.1f}s")

    print("\n=== 这 4 个玩家参与过的事件 ===")
    slots = {slot_of_name[n]: n for n, _v, _l in TARGETS.values() if n in slot_of_name}
    for ev in d.kills:
        if ev.killer_pid in slots or ev.victim_pid in slots:
            print(f"  t={ev.t:7.1f}s killer_pid={ev.killer_pid} "
                  f"(uid {ev.killer_uid}) → victim_pid={ev.victim_pid} "
                  f"(uid {ev.victim_uid})  {ev.vehicle}/{ev.weapon}")


if __name__ == "__main__":
    main(sys.argv[1])
