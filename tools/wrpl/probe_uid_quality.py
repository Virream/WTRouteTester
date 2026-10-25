"""诊断 ECS 的 uid 到底能不能用: 多少档案的 uid 在位置流里出现过 / 一个 uid 上挤了多少台车。

如果大量档案的 uid 在 `data.units` 里根本没采样, 说明「按 uid 分组」的前提不成立
(窗口裁剪 / 链式截断 / 段归属全会错), 这正是「残骸被切成第二条线路」的根源。
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
    print(f"level={data.level} battle={data.battle_type}")
    print(f"ECS 载具 {len(data.vehicles)} 台, 位置流 uid {len(data.units)} 个, "
          f"飞机档案 {len(getattr(data, 'air_vehicles', ()))} 台, "
          f"飞机 uid {len(data.air_units)} 个, uid_offset={data.ecs_uid_offset}")

    known = set(data.units)
    hit = [v for v in data.vehicles if v.uid in known]
    miss = [v for v in data.vehicles if v.uid not in known]
    print(f"\n地面档案: uid 有采样 {len(hit)} / 无采样 {len(miss)}")
    print("  无采样的样例:")
    for v in miss[:15]:
        print(f"    uid{v.uid:<4d} {v.short_cls:26s} {v.player or '-':20s}"
              f" born {v.born_t:7.1f} died "
              f"{('%.1f' % v.died_t) if v.died_t is not None else 'None':>7s}"
              f" ({v.died_by})  spawn={v.spawn}")

    cnt = collections.Counter(v.uid for v in data.vehicles)
    print("\n一个 uid 上挤了多台车(按数量降序, 只列 >1):")
    for uid, n in cnt.most_common(15):
        if n < 2:
            break
        have = "有采样" if uid in known else "无采样"
        players = {v.player or "-" for v in data.vehicles if v.uid == uid}
        print(f"    uid{uid:<4d} {n} 台 ({have}) 玩家: {', '.join(sorted(players))[:90]}")

    # 重叠检测: 同一 uid 上窗口互相重叠 = 绝不可能
    overlap = 0
    for uid, n in cnt.items():
        if n < 2:
            continue
        lst = sorted((v for v in data.vehicles if v.uid == uid),
                     key=lambda v: v.born_t)
        for a, b in zip(lst, lst[1:]):
            if a.died_t is None or b.born_t < a.died_t - 0.5:
                overlap += 1
                break
    print(f"\n窗口互相重叠的 uid: {overlap} 个 / 共 {sum(1 for n in cnt.values() if n > 1)} 个多档案 uid")

    print("\nUID 值分布(前 12 个最常见的):")
    for uid, n in cnt.most_common(12):
        print(f"    {uid:5d}: {n}")


if __name__ == "__main__":
    main()
