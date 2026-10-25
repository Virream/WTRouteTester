"""验证「用时间做钥匙」修复错读的 uid:
对 uid 对不上的玩家载具, 找「首采样时刻 ≈ born_t」且**没被别的档案占用**的 uid。

先量一下: uid 正确的那批, (首采样时刻 - born_t) 分布有多紧。紧就说明时间能当钥匙。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                      # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)
    known = set(data.units)

    good = [v for v in data.vehicles if v.spawn and v.uid in known]
    diffs = [data.units[v.uid][0][0] - v.born_t for v in good]
    print(f"uid 正确的玩家载具 {len(good)} 台, (首采样 - 出生) 分布:")
    for p in (0.0, 0.05, 0.1, 0.5, 0.9, 0.95, 1.0):
        print(f"  p{p * 100:5.1f}%  {pct(diffs, p):+8.2f}s")
    inside = sum(1 for d in diffs if -2.0 <= d <= 30.0)
    print(f"  落在 [-2, +30]s 的: {inside} / {len(diffs)}"
          f"  ({inside / max(1, len(diffs)) * 100:.0f}%)")

    # 已被"正确 uid"占用的档案 → 那些 uid 不许再分给别人
    claimed = {v.uid for v in good}
    bad = [v for v in data.vehicles if v.spawn and v.uid not in known]
    print(f"\n待修复 {len(bad)} 台, 用「首采样时刻接近 born_t」找候选:")

    # uid → 首个采样时刻
    first_t = {uid: rows[0][0] for uid, rows in data.units.items()}
    fixed = 0
    for v in sorted(bad, key=lambda x: x.born_t):
        cands = [u for u, t in first_t.items()
                 if u not in claimed and -2.0 <= t - v.born_t <= 30.0]
        tag = ""
        if len(cands) == 1:
            fixed += 1
            tag = "  ← 唯一候选 ✓"
            claimed.add(cands[0])
        print(f"  uid{v.uid:<5d} {v.short_cls:26s} {v.player or '-':22s}"
              f" born {v.born_t:7.1f}  候选 {cands}{tag}")
    print(f"\n能唯一确定的: {fixed} / {len(bad)}")


if __name__ == "__main__":
    main()
