"""探针: 同一个 uid 会不会对应**多辆**载具(uid 复用), 尤其同型号的情况。

用户问题(2026-10-25): 一位玩家第一辆车被击毁后, 第二次又出了**相同型号**的车,
                      会不会出错?

关键怀疑: 侧栏的「台」是按 **uid + 队** 分配基号的, 所以同一个 uid 先后对应的
两辆车会被合并成"1 台"。这里先看数据里到底有多少这种情况。

用法:
    python tools/wrpl/probe_dup.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402


def main(path: str) -> None:
    data = W.load_replay(path, expect_server=None)
    vans: dict[int, list] = collections.defaultdict(list)
    for v in getattr(data, "vehicles", ()):
        vans[v.uid].append(v)
    for lst in vans.values():
        lst.sort(key=lambda v: v.born_t)

    print(f"载具档案 {len(getattr(data, 'vehicles', ()))} 条, "
          f"互异 uid {len(vans)} 个")
    dist = collections.Counter(len(v) for v in vans.values())
    print("\n=== 每个 uid 对应几辆车 ===")
    for k in sorted(dist):
        print(f"  {k} 辆: {dist[k]} 个 uid")

    print("\n=== uid 对应多辆车的情况 ===")
    n_same_slot = n_same_cls = n_both = 0
    rows = []
    for uid, lst in sorted(vans.items()):
        if len(lst) < 2:
            continue
        slots = {v.slot for v in lst}
        clss = {v.short_cls for v in lst}
        if len(slots) == 1 and None not in slots:
            n_same_slot += 1
            if len(clss) == 1:
                n_same_cls += 1
                n_both += 1
        rows.append((uid, lst))
    print(f"  同一玩家(同 slot)的: {n_same_slot} 个 uid")
    print(f"  其中**两辆同型号**的: {n_same_cls} 个 uid  <-- 用户问的场景")

    for uid, lst in rows[:25]:
        print(f"\n  uid={uid}")
        for v in lst:
            d = f"{v.died_t:7.1f}" if v.died_t is not None else "   未定"
            print(f"    born {v.born_t:7.1f} -> {d}  slot={v.slot!s:<5s} "
                  f"{v.player[:16]:<18s} {v.short_cls:<28s} eid=0x{v.eid:x} "
                  f"({v.died_by})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
