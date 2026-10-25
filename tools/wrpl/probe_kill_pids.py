"""检查击杀包解析: killer_pid / victim_pid 的分布是否合理。

如果一个玩家名下有 26 个 uid, 说明 victim_pid(或 killer_pid)被解错了 ——
正常一局一个人最多换 3~5 台车。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)
    ks = data.kills
    print(f"击杀事件 {len(ks)} 条, 玩家槽位 {len(data.players)} 个")

    kp = collections.Counter(ev.killer_pid for ev in ks)
    vp = collections.Counter(ev.victim_pid for ev in ks)
    print(f"\nkiller_pid 分布(前 10): {kp.most_common(10)}")
    print(f"victim_pid 分布(前 10): {vp.most_common(10)}")
    print(f"killer_pid==0 的: {kp.get(0, 0)}   victim_pid==0 的: {vp.get(0, 0)}")
    print(f"不同 killer_pid: {len([p for p in kp if p])}"
          f"   不同 victim_pid: {len([p for p in vp if p])}")

    print("\n前 25 条击杀(看 pid 与昵称对不对得上):")
    name = {p.slot: p.display for p in data.players.values()}
    for ev in ks[:25]:
        print(f"  t={ev.t:7.1f}  killer uid{ev.killer_uid:<5d} pid{ev.killer_pid:<3d}"
              f" {(name.get(ev.killer_pid) or '?')[:22]:24s}"
              f" -> victim uid{ev.victim_uid:<5d} pid{ev.victim_pid:<3d}"
              f" {(name.get(ev.victim_pid) or '?')[:22]:24s} [{ev.vehicle}]")

    # 自检: killer 的 uid 是否在他自己的存活窗口里, 且 uid 的采样起点是否接近击杀时刻
    print("\n自检: 每个 killer_pid 对应的 uid 数量(>6 就很可能解错了):")
    per = collections.defaultdict(set)
    for ev in ks:
        if ev.killer_pid and ev.killer_uid != 0x7FF:
            per[ev.killer_pid].add(ev.killer_uid)
    for pid, uids in sorted(per.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"  pid {pid:<3d} {(name.get(pid) or '?')[:26]:28s} uids {len(uids)}:"
              f" {sorted(uids)[:12]}")


if __name__ == "__main__":
    main()
