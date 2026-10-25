"""用 ECS 档案(出生组名 → 槽位)当裁判, 核对击杀包里的 killer_pid / victim_pid。

ECS 的「出生组名 → 槽位」公式已经用击杀 32/32 验证过, 所以可以反过来当标准答案:
  * 某 uid 的档案给了 spawn 名 → 槽位(以及队伍) 是确定的;
  * 于是 "killer_uid 的槽位 应当 == killer_pid"、"victim_uid 的槽位 应当 == victim_pid"。
对不上的那个字段就是解错了 —— 这比猜字段编号可靠。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import EXT_MASK_UID, load_replay        # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)

    # uid -> (slot, team) 来自 ECS(只取有出生组名、且已经绑到玩家的档案)
    uid_slot: dict[int, int] = {}
    for v in data.vehicles:
        if v.spawn and v.player:
            for p in data.players.values():
                if (p.display or p.name or "") == v.player:
                    uid_slot[v.uid] = p.slot
                    break

    print(f"ECS 能给出槽位的 uid: {len(uid_slot)} 个; 击杀事件 {len(data.kills)} 条")

    def check(label: str, got_pid: int, uid_attr: str) -> tuple[int, int, int]:
        ok = bad = unknown = 0
        examples = []
        for ev in data.kills:
            uid = getattr(ev, uid_attr)
            if uid == EXT_MASK_UID or uid not in uid_slot:
                unknown += 1
                continue
            want = uid_slot[uid]
            if want == got_pid:
                ok += 1
            else:
                bad += 1
                if len(examples) < 6:
                    examples.append((ev.t, uid, want, got_pid))
        print(f"\n{label}: 对得上 {ok} / 对不上 {bad} / 无档案 {unknown}")
        for t, uid, want, got_pid in examples:
            print(f"    反例 t={t:7.1f} uid{uid} ECS 槽位 {want}  包里给的 {got_pid}")
        return ok, bad, unknown

    check("killer_pid vs ECS(killer_uid)", 0, "killer_uid")
    # victim 单独走: 需要逐条拿 ev.victim_pid
    ok = bad = unknown = 0
    ex = []
    for ev in data.kills:
        uid = ev.victim_uid
        if uid == EXT_MASK_UID or uid not in uid_slot:
            unknown += 1
            continue
        want = uid_slot[uid]
        if want == ev.victim_pid:
            ok += 1
        else:
            bad += 1
            if len(ex) < 10:
                ex.append((ev.t, uid, want, ev.victim_pid))
    print(f"\nvictim_pid vs ECS(victim_uid): 对得上 {ok} / 对不上 {bad} / 无档案 {unknown}")
    for t, uid, want, got_pid in ex:
        print(f"    反例 t={t:7.1f} uid{uid} ECS 槽位 {want}  包里给的 {got_pid}")

    # victim_pid 到底像什么? 按"killer 的队伍"分组看看
    print("\nvictim_pid 的取值 by killer 队伍:")
    teams = {p.slot: p.team for p in data.players.values()}
    by_team = collections.defaultdict(collections.Counter)
    for ev in data.kills:
        by_team[teams.get(ev.killer_pid)][ev.victim_pid] += 1
    for t, cnt in by_team.items():
        print(f"  killer team{t}: {cnt.most_common(8)}")


if __name__ == "__main__":
    main()
