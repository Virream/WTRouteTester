"""验证: 那 18 台「uid 对不上」的玩家载具, 正确 uid 是不是 `uid >> 1`。

发现: 它们的 uid 全是**偶数**(880/1392/1136/112/1904/624/368…), 像是字段起点比标定
偏移晚/早 1 位 —— 那么 `uid >> 1` 应当落在位置流的 uid 集合里。
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
    data = load_replay(path)
    known = set(data.units)
    bad = [v for v in data.vehicles if v.spawn and v.uid not in known]
    print(f"uid 对不上的玩家载具 {len(bad)} 台")
    hit = 0
    for v in bad:
        cand = v.uid >> 1
        got = cand in known
        hit += got
        extra = ""
        if got:
            rows = data.units[cand]
            # 采样起点应当与 born_t 对得上(生成时刻 ≈ 第一个采样)
            dt = rows[0][0] - v.born_t
            extra = (f"  ← uid{cand:<5d} 有 {len(rows):5d} 点"
                     f"  首采样 t {rows[0][0]:7.1f} (出生 {v.born_t:.1f}, 差 {dt:+.1f}s)")
        print(f"  uid{v.uid:<5d} ({'偶' if v.uid % 2 == 0 else '奇'})"
              f"{' ✓' if got else ' ✗'} {v.short_cls:26s}"
              f" {v.player or '-':22s}{extra}")
    print(f"\nuid>>1 命中位置流: {hit} / {len(bad)}")


if __name__ == "__main__":
    main()
