"""用 ECS(出生组名→槽位)当裁判, 逐个核对三种"身份来源"是否可信:
  ① 击杀包的 killer_pid(killer_uid)
  ② 击杀包的 victim_pid(victim_uid)   ← 怀疑解错了
  ③ 严重伤害包的 pid(dv.uid)
顺便量一下: 只用 ①+③ 能覆盖多少 uid。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g import wrpl_events as we                          # noqa: E402
from wt81111g.wrpl_reader import EXT_MASK_UID, load_replay        # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
DMG: list = []


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    orig = we.parse_damage_packet

    def patched(payload, s, e, t=0.0):
        ev = orig(payload, s, e, t)
        if ev is not None:
            DMG.append(ev)
        return ev

    we.parse_damage_packet = patched
    try:
        data = load_replay(path)
    finally:
        we.parse_damage_packet = orig

    slot_of_name = {(p.display or p.name or ""): p.slot for p in data.players.values()}
    uid_slot = {}
    for v in data.vehicles:
        if v.spawn and v.player and v.player in slot_of_name:
            uid_slot[v.uid] = slot_of_name[v.player]

    def judge(label, pairs):
        ok = bad = 0
        ex = []
        for uid, pid in pairs:
            want = uid_slot.get(uid)
            if want is None:
                continue
            if want == pid:
                ok += 1
            else:
                bad += 1
                if len(ex) < 5:
                    ex.append((uid, want, pid))
        print(f"{label}: 对 {ok} / 错 {bad}")
        for uid, want, pid in ex:
            print(f"    反例 uid{uid} ECS 槽位 {want}  包里 {pid}")

    print(f"ECS 给出槽位的 uid: {len(uid_slot)};  击杀 {len(data.kills)};  伤害 {len(DMG)}\n")
    judge("① killer_pid",
          [(ev.killer_uid, ev.killer_pid) for ev in data.kills
           if ev.killer_uid != EXT_MASK_UID])
    judge("② victim_pid",
          [(ev.victim_uid, ev.victim_pid) for ev in data.kills
           if ev.victim_uid != EXT_MASK_UID])
    judge("③ damage pid",
          [(ev.uid, ev.pid) for ev in DMG if ev.uid != EXT_MASK_UID])

    # 覆盖率: 只用 ①+③ 时, 位置流里多少 uid 能拿到玩家
    only = {}
    for ev in data.kills:
        if ev.killer_uid != EXT_MASK_UID and ev.killer_pid:
            only.setdefault(ev.killer_uid, ev.killer_pid)
    for ev in DMG:
        if ev.uid != EXT_MASK_UID and ev.pid:
            only.setdefault(ev.uid, ev.pid)
    print(f"\n只用 ①+③ 覆盖的 uid: {len(only)} / {len(data.units)}"
          f"  ({len(only) / max(1, len(data.units)) * 100:.0f}%)")
    print(f"现在(含 ②)覆盖的 uid: {len(data.uid_pid)} / {len(data.units)}"
          f"  ({len(data.uid_pid) / max(1, len(data.units)) * 100:.0f}%)")

    # ② 与 ① 的差异: 有多少 uid 只靠 victim_pid 才有主人
    extra = [u for u in data.uid_pid if u not in only]
    print(f"只靠 victim_pid 才拿到主人的 uid: {len(extra)} 个 {sorted(extra)[:20]}")
    print(f"其中 ECS 也有档案的: "
          f"{sorted(u for u in extra if u in uid_slot)}")


if __name__ == "__main__":
    main()
