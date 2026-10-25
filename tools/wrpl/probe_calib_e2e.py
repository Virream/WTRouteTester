"""端到端: 真回放 + 8111 校准 → 归一化坐标是否落在底图正确位置。

跑两遍同一局回放(有校准 / 没校准), 比较:
  * `stats.calibrated` 与 `rec.norm_rect`;
  * 地面载具首点的归一化坐标 —— 有校准时应该落在**坦克重生点那条走廊**上
    (实测 8111 真值 x 1051~1194 / z 371~1725 → 归一化 x ≈ 0.51~0.58,
    y ≈ 0.16~0.82), 没校准则是“观测范围铺满 0~1”那种凑出来的比例。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.map_calib import load_from_capture                  # noqa: E402
from wt81111g.replay_import import import_replay                  # noqa: E402

CAP = os.path.join(ROOT, "回放文件", "8111抓取", "20261025_201545")
SERVER = os.path.join(ROOT, "回放文件", "服务器", "芬兰单点模式")


class FakeStore:
    """只提供 import_replay 需要的那两个方法。"""

    def __init__(self, calib=None):
        self._calib = calib
        self.rec = None

    def calib_of(self, map_key):
        return self._calib

    def add_global(self, rec):
        self.rec = rec


def run(calib, label: str):
    store = FakeStore(calib)
    rec, st = import_replay(SERVER, store)
    print(f"\n---- {label} ----")
    print(f"  地图键: {rec.map_key}   记录 {rec.count()} 条"
          f"  (A {len(rec.lives('A'))} / B {len(rec.lives('B'))})")
    print(f"  calibrated={st.calibrated}  note={st.calib_note or '-'}")
    print(f"  norm_rect={[round(v, 1) for v in rec.norm_rect]}")
    g = [lf for lf in rec.lives("A") + rec.lives("B")
         if lf.obj_type == "ground_model" and lf.points]
    print(f"  地面线路 {len(g)} 条")
    if not g:
        return rec, st
    xs = [p.x for lf in g for p in lf.points]
    ys = [p.y for lf in g for p in lf.points]
    print(f"  归一化 x {min(xs):.3f}~{max(xs):.3f}   y {min(ys):.3f}~{max(ys):.3f}")
    out = sum(1 for p in (q for lf in g for q in lf.points)
              if not (-0.05 <= p.x <= 1.05 and -0.05 <= p.y <= 1.05))
    print(f"  框外点 {out} / {len(xs)}")
    # 首点(重生)应当成团, 而不是铺满整张图
    firsts = [lf.points[0].x for lf in g]
    print(f"  首点 x 中位数 {sorted(firsts)[len(firsts) // 2]:.3f}")
    return rec, st


def main() -> None:
    calib = load_from_capture(CAP)
    print("校准:", calib.describe())
    rec1, st1 = run(calib, "有校准")
    assert st1.calibrated, "有校准但 stats.calibrated 是 False"
    assert [round(v, 1) for v in rec1.norm_rect] == [0.0, 2048.0, 0.0, 2048.0], rec1.norm_rect
    assert rec1.map_key.startswith("wrpl_"), rec1.map_key

    rec0, st0 = run(None, "无校准(旧行为)")
    assert not st0.calibrated
    assert rec0.norm_rect != rec1.norm_rect, "有无校准的矩形居然一样?"

    print("\n全部通过")


if __name__ == "__main__":
    main()
