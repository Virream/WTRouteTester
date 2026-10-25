"""诊断: 某玩家名下为什么会有多条线路(甚至从敌人重生点开始)。

路径: `build_record` 里 `_who` 的兜底是 `names.get(data.uid_pid.get(uid))`,
而 `data.uid_pid` 来自击杀包 —— 只要"这些 uid 参与过他的击杀/被击杀", 线路就会挂到
他名下。要看的是: 这些 uid 各自的采样窗口在什么时间段、起点在哪、与他自己的
ECS 档案能不能对得上。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    who = sys.argv[2] if len(sys.argv) > 2 else "WineLover"
    data = load_replay(path)

    slots = [p.slot for p in data.players.values() if who in (p.display or "")]
    print(f"匹配「{who}」的玩家槽位: {slots}")
    if not slots:
        return
    slot = slots[0]
    print(f"  {data.players[slot].display}  team={data.players[slot].team}")

    my_uids = sorted(u for u, p in data.uid_pid.items() if p == slot)
    print(f"\n击杀包给出的「他的 uid」: {my_uids}")

    print("\n各 uid 的采样情况:")
    for u in my_uids:
        rows = data.units.get(u) or []
        if not rows:
            print(f"  uid{u:<5d} 无采样")
            continue
        print(f"  uid{u:<5d} {len(rows):5d} 点  t {rows[0][0]:7.1f}~{rows[-1][0]:7.1f}"
              f"  首点 ({rows[0][1]:8.1f}, {rows[0][3]:8.1f})")
        # 这个 uid 在击杀包里的角色
        for ev in data.kills:
            if ev.killer_uid == u and ev.killer_pid == slot:
                print(f"      ✦ 他开这台车击杀 t={ev.t:.1f} {ev.vehicle}")
            if ev.victim_uid == u:
                print(f"      ✝ 这台车被击毁 t={ev.t:.1f} killer_pid={ev.killer_pid}")

    print("\n他自己的 ECS 档案(按出生时间):")
    for v in sorted((v for v in data.vehicles if v.player and who in v.player),
                    key=lambda v: v.born_t):
        has = "有采样" if v.uid in data.units else "无采样"
        print(f"  uid{v.uid:<5d}({has}) {v.short_cls:28s} born {v.born_t:7.1f}"
              f" died {('%.1f' % v.died_t) if v.died_t is not None else 'None':>7s}"
              f" bind={v.bind} spawn={v.spawn}")

    # 玩家槽位 0 是不是真的他? 打印槽位表核对
    print("\n槽位表里 team1/team2 各有多少人:")
    for t in (1, 2):
        n = sum(1 for p in data.players.values() if p.team == t)
        print(f"  team{t}: {n} 人")


if __name__ == "__main__":
    main()
