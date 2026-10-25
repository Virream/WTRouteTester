"""探针: 验证 load_replay 收集的玩家/击杀/uid→玩家映射覆盖率。用完可留。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g.wrpl_reader import load_replay                          # noqa: E402


def main(path: str) -> None:
    t0 = time.perf_counter()
    d = load_replay(path)
    dt = time.perf_counter() - t0
    print(f"{d.level} 分片={d.parts} 包={d.packets:,} 失败={d.failed_packets} "
          f"样本={d.sample_count:,} uid={len(d.units)}  耗时 {dt:.2f}s")
    print(f"玩家 {len(d.players)} 个, 击杀 {len(d.kills)} 条, "
          f"uid→玩家映射 {len(d.uid_pid)} 条")

    teams = {p.slot: p.team for p in d.players.values()}
    covered = 0
    by_team: dict[int, int] = {}
    for uid in d.units:
        pid = d.uid_pid.get(uid)
        tm = teams.get(pid) if pid is not None else None
        if tm:
            covered += 1
            by_team[tm] = by_team.get(tm, 0) + 1
    print(f"\n载具 {len(d.units)} 个: 能映射到玩家的 {covered} 个 "
          f"({100*covered/max(1,len(d.units)):.0f}%)  按队伍 {by_team}")
    print("\n映射到的前 12 个:")
    for uid in sorted(d.units)[:12]:
        pid = d.uid_pid.get(uid)
        plr = d.players.get(pid) if pid is not None else None
        print(f"  uid={uid:4d} pid={pid} team={plr.team if plr else '-'} "
              f"{(plr.display if plr else '(未映射)')}")


if __name__ == "__main__":
    main(sys.argv[1])
