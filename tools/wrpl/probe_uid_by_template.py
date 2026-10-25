"""对照: uid 读错的那批创建包是不是都来自 0x25(LZ4), 且模板是否不同。

如果错误集中在某一种块类型 / 某个模板, 说明 uid 的相对偏移**按模板/块类型**不一样 ——
那就该分开标定(而不是再猜)。
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
    orig_build = we.EcsCollector.build

    def patched(self, **kw):
        KEEP.extend(self._creations)
        return orig_build(self, **kw)

    we.EcsCollector.build = patched
    try:
        data = load_replay(path)
    finally:
        we.EcsCollector.build = orig_build

    known = set(data.units)
    print(f"创建记录 {len(KEEP)}  位置流 uid {len(known)}")

    # 按 (ctrl, 模板) 分组统计: 该组里 uid 落进位置流的比例
    stat: dict[tuple, list[int]] = collections.defaultdict(list)
    for c in KEEP:
        if not we.is_unit_template(c.template):
            continue
        uid = we.EcsCollector._uid_at(c, 27)
        if uid is None:
            continue
        stat[(c.ctrl, c.template)].append(1 if uid in known else 0)

    print("\n(块类型, 模板)                     数量   命中位置流")
    for key, vals in sorted(stat.items(), key=lambda kv: -len(kv[1])):
        hit = sum(vals)
        print(f"  {key[0]:#4x} {key[1][:26]:28s} {len(vals):5d}   {hit:5d}"
              f"  ({hit / len(vals) * 100:5.1f}%)")

    # 只看 player_unit 的: 非字节对齐的比例
    un = collections.Counter()
    for c in KEEP:
        if "player_unit" in c.template:
            un[(c.ctrl, c.vbits % 8)] += 1
    print("\nplayer_unit 创建包 (块类型, vbits%8) 分布:")
    for k, n in sorted(un.items()):
        print(f"  {k[0]:#4x} bit%8={k[1]}  {n}")


if __name__ == "__main__":
    main()
