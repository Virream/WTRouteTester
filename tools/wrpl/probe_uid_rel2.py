"""对「在标定偏移上读不出合法 uid」的那批块, 单独再标定一个偏移 rel2。

假设: 这批块的组件值布局与主流差几个字节(实测像是多了 1 字节), 所以
**用同一套规则再标定一次**就能对齐, 不需要猜。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g import wrpl_entities as we                        # noqa: E402
from wt81111g.wrpl_reader import load_replay                    # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
KEEP: list = []


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    orig = we.EcsCollector.build

    def patched(self, **kw):
        KEEP.extend(self._creations)
        return orig(self, **kw)

    we.EcsCollector.build = patched
    try:
        data = load_replay(path)
    finally:
        we.EcsCollector.build = orig
    known = set(data.units)
    rel = data.ecs_uid_offset or 28
    print(f"标定偏移 rel={rel}; 位置流 uid {len(known)} 个")

    bad: list = []
    good = 0
    for c in KEEP:
        if "player_unit" not in c.template or "aircraft" in c.template:
            continue
        spawn = ""
        for _o, s in we.scanned_strings(c.block, 3, 60):
            if we.SPAWN_RE.match(s):
                spawn = s
                break
        if not spawn:
            continue
        v = we.EcsCollector._uid_at(c, rel)
        if v in known:
            good += 1
        else:
            bad.append(c)
    print(f"玩家载具块: 在 rel 上合法 {good} / 不合法 {len(bad)}")

    # 对 bad 子集单独标定
    print("\nbad 子集上各偏移的命中数(取前 8):")
    score = []
    for d in range(0, 60):
        vals = [we.EcsCollector._uid_at(c, d) for c in bad]
        if any(v is None for v in vals):
            break
        hit = sum(1 for v in vals if v in known)
        uniq = len(set(vals))
        score.append((hit, uniq, d))
    score.sort(reverse=True)
    for hit, uniq, d in score[:8]:
        print(f"  rel2={d:3d}  命中 {hit}/{len(bad)}  互异 {uniq}")

    if score:
        hit, uniq, d = score[0]
        print(f"\n用 rel2={d} 时各块的取值:")
        vals = [we.EcsCollector._uid_at(c, d) for c in bad]
        print(f"  {vals}")
        print(f"  落在 known 里的: {[v for v in vals if v in known]}")
        print(f"  重复值: {[v for v, n in collections.Counter(vals).items() if n > 1]}")


if __name__ == "__main__":
    main()
