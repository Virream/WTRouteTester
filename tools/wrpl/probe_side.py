"""探针: 验证 A/B 真实分边与玩家名。用完可留。"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g.replay_import import import_replay                     # noqa: E402
from wt81111g.route_store import RouteStore                           # noqa: E402


def main(path: str) -> None:
    tmp = tempfile.mkdtemp(prefix="wrpl_side_")
    store = RouteStore(os.path.join(tmp, "routes.json"))
    rec, st = import_replay(path, store, expect_server=None)
    print(f"{st.level} {st.battle_type}: 样本={st.samples:,} 线路={st.lives} "
          f"丢弃={st.dropped}")
    print(f"玩家={st.players} 击杀={st.kills} 能映射到玩家的车={st.mapped}")
    print(f"A 队 {len(rec.sides['A'])} 条 / B 队 {len(rec.sides['B'])} 条")

    for side in ("A", "B"):
        lives = rec.sides[side]
        if not lives:
            continue
        print(f"\n{side} 队前 8 条:")
        for lf in sorted(lives, key=lambda x: -len(x.points))[:8]:
            print(f"  {lf.icon[:52]:52s} 点={len(lf.points):5d} "
                  f"t={lf.t0:7.1f}~{lf.t1:7.1f}s")

    named = sum(1 for s in ("A", "B") for lf in rec.sides[s]
                if not lf.icon.startswith("uid"))
    total = len(rec.sides["A"]) + len(rec.sides["B"])
    print(f"\n有名字的线路 {named}/{total} ({100*named/max(1,total):.0f}%)")


if __name__ == "__main__":
    main(sys.argv[1])
