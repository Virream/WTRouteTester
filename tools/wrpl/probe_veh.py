"""按玩家名查 ECS 载具档案(存活窗口 / died_by), 用来查"残骸被切成第二段"。

用法: python tools\\wrpl\\probe_veh.py <回放路径> <玩家名片段>
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
    who = sys.argv[2] if len(sys.argv) > 2 else ""
    data = load_replay(path)
    print(f"level={data.level} battle={data.battle_type}  t_last={data.t_last:.1f}")

    hits = [v for v in data.vehicles if who and who in (v.player or "")]
    print(f"\n匹配「{who}」的地面档案 {len(hits)} 台:")
    uids = set()
    for v in hits:
        uids.add(v.uid)
        print(f"  uid{v.uid:<4d} {v.short_cls:26s} t{v.team} bind={v.bind:11s}"
              f" born {v.born_t:7.1f} died "
              f"{('%.1f' % v.died_t) if v.died_t is not None else '  None':>7s}"
              f"  died_by={v.died_by}")

    print(f"\n这些 uid 上的全部档案(同 uid 可能有别人/别的车):")
    for uid in sorted(uids):
        rows = data.units.get(uid) or []
        pts = f"{len(rows)} 点 t {rows[0][0]:.1f}~{rows[-1][0]:.1f}" if rows else "无采样"
        print(f"  uid{uid}: {pts}")
        for v in data.vehicles:
            if v.uid != uid:
                continue
            print(f"     {v.short_cls:26s} {v.player or '-':18s}"
                  f" born {v.born_t:7.1f} died "
                  f"{('%.1f' % v.died_t) if v.died_t is not None else '  None':>7s}"
                  f" ({v.died_by})")
        # 该 uid 的击杀事件
        for ev in data.kills:
            if ev.victim_uid == uid:
                print(f"     ✝ 被击杀 t={ev.t:.1f}  killer_pid={ev.killer_pid}"
                      f"  weapon={ev.weapon}")

    print("\n== ECS 销毁表里这些 eid 有没有记录 ==")
    print("  (died_by=None 就是没找到销毁包, 这种车不会被窗口裁掉)")


if __name__ == "__main__":
    main()
