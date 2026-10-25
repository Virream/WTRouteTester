"""验证猜: 45 台"uid 无采样"的档案其实是**位/字节错位**读出来的, 把值移一位就对了。

如果 `uid>>1`(或 `>>2`/`<<1`)落在位置流的 uid 集合里, 就说明那些创建包的 uid 字段
起点与标定出的偏移差 1 位 —— 而这些错位的档案正是"残骸被切成第二段"的根源
(它们的存活窗口是垃圾, 窗口裁剪/链式截断全失效)。
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
    known = set(data.units)
    miss = [v for v in data.vehicles if v.uid not in known]
    print(f"无采样档案 {len(miss)} 台; 位置流 uid {len(known)} 个")

    cands = {
        "uid>>1": lambda u: u >> 1,
        "uid>>2": lambda u: u >> 2,
        "uid<<1": lambda u: (u << 1) & 0x7FF,
        "uid//2": lambda u: u // 2,
    }
    score: collections.Counter = collections.Counter()
    for v in miss:
        for name, fn in cands.items():
            if fn(v.uid) in known:
                score[name] += 1
    print("\n候选变换命中位置流 uid 的数量:", dict(score))

    print("\n前 20 个无采样档案, 看它们与『偶数化』的关系:")
    even = sum(1 for v in miss if v.uid % 2 == 0)
    print(f"  uid 为偶数: {even} / {len(miss)}")
    for v in miss[:20]:
        m = cands["uid>>1"](v.uid) in known
        print(f"  uid{v.uid:<5d} (>>1 = {v.uid >> 1:<5d}"
              f"{' ✓在位置流' if m else '        '})  {v.short_cls:26s}"
              f" {v.player or '-':22s} born {v.born_t:7.1f}")

    print("\n位置流里存在但没有任何档案的 uid(说明这些位置点无法归属):")
    used = {v.uid for v in data.vehicles}
    orphan = sorted(k for k in known if k not in used)
    print(f"  {len(orphan)} 个: {orphan[:40]}")


if __name__ == "__main__":
    main()
