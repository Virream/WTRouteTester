"""列出未映射 uid 对应的线路(给用户到软件里定位用)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g.replay_import import import_replay                     # noqa: E402
from wt81111g.route_store import RouteStore                           # noqa: E402

tmp = tempfile.mkdtemp(prefix="wrpl_u4_")
store = RouteStore(os.path.join(tmp, "r.json"))
rec, st = import_replay(sys.argv[1], store, expect_server=None)

lives = [lf for sd in ("A", "B") for lf in rec.sides[sd]]
want = {"uid165", "uid177", "uid182", "uid183"}
print(f"共 {len(lives)} 条线路, 其中未映射的 4 个 uid:")
for lf in lives:
    if lf.icon not in want:
        continue
    short = lf.life_id.split(":")[-1]
    p0, p1 = lf.points[0], lf.points[-1]
    xs = [p.x for p in lf.points]
    ys = [p.y for p in lf.points]
    print(f"\n  侧栏名字: {lf.icon}    线路号: {short}    {lf.side} 队")
    print(f"    点数 {len(lf.points)}  时长 {lf.t0:.1f}~{lf.t1:.1f}s  "
          f"被击毁={lf.dead}")
    print(f"    起点 归一化({p0.x:.3f}, {p0.y:.3f})  终点({p1.x:.3f}, {p1.y:.3f})")
    print(f"    x 范围 {min(xs):.3f}~{max(xs):.3f}  y 范围 {min(ys):.3f}~{max(ys):.3f}")
