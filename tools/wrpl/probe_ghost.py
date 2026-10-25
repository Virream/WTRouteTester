"""诊断「残骸被当成第二条线路」(用户 2026-10-25: A26_2 不应存在)。

做法: 不做任何过滤, 直接把每一台 ECS 载具档案和它对应的原始采样点按时间列出来,
看**`died_t` 之后是否还有采样**, 以及那段采样被切成了第几段。

用法:
    python tools\\wrpl\\probe_ghost.py <回放文件或分片目录> [A26]
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.replay_import import GAP_SPLIT_SEC, JUMP_MIN_M, JUMP_MAX_MPS  # noqa: E402
from wt81111g.wrpl_reader import load_replay                                # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器", "芬兰单点模式")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)
    print(f"level={data.level} battle={data.battle_type}")

    def name_of(v) -> str:
        return f"uid{v.uid:<4d} {v.short_cls:26s} {v.player or '-':16s}" \
               f" t{v.team or '-'} born {v.born_t:7.1f} died " \
               f"{('%.1f' % v.died_t) if v.died_t is not None else '  end':>7s} ({v.died_by})"

    print("\n== 所有玩家地面载具的存活窗口 ==")
    for v in data.vehicles:
        print("  " + name_of(v))

    print("\n== 采样点落在 died_t 之后的载具(残骸上继续上报) ==")
    for v in data.vehicles:
        rows = data.units.get(v.uid) or []
        if not rows or v.died_t is None:
            continue
        after = [r for r in rows if r[0] > v.died_t + 2.0]
        if not after:
            continue
        for a, b in zip(after, after[1:]):
            dt = b[0] - a[0]
            d = ((b[1] - a[1]) ** 2 + (b[3] - a[3]) ** 2) ** 0.5
            if dt > GAP_SPLIT_SEC or (d > JUMP_MIN_M and dt > 0 and d / dt > JUMP_MAX_MPS):
                print(f"  ⚠ 会被切成两段: t={a[0]:.1f} -> {b[0]:.1f} dt={dt:.1f}s d={d:.0f}m")
        print(f"  {name_of(v)}")
        print(f"     died_t 之后还有 {len(after)} 个点, "
              f"t {after[0][0]:.1f}~{after[-1][0]:.1f}, "
              f"位置 ({after[0][1]:.0f}, {after[0][3]:.0f}) → ({after[-1][1]:.0f}, {after[-1][3]:.0f})")

    # 同一 uid 的多台车(uid 复用) / 同一玩家同型号两台
    print("\n== 同一个 uid 有多个 ECS 档案(uid 复用) ==")
    by_uid: dict[int, list] = {}
    for v in data.vehicles:
        by_uid.setdefault(v.uid, []).append(v)
    for uid, lst in sorted(by_uid.items()):
        if len(lst) < 2:
            continue
        print(f"  uid {uid}: {len(lst)} 台")
        for v in lst:
            print("    " + name_of(v))

    print("\n== 同一玩家同型号的多台车(可能被当成'第三/第四条命') ==")
    by_key: dict[tuple, list] = {}
    for v in data.vehicles:
        if v.player:
            by_key.setdefault((v.player, v.short_cls), []).append(v)
    for (who, cls), lst in sorted(by_key.items()):
        if len(lst) > 1:
            print(f"  {who} · {cls}: {len(lst)} 台")
            for v in lst:
                print(f"     born {v.born_t:7.1f} died "
                      f"{('%.1f' % v.died_t) if v.died_t is not None else '  end':>7s}"
                      f"  ({v.died_by})  uid {v.uid}")

    # 击杀时刻 vs ECS 销毁时刻(残骸窗口)
    print("\n== 击杀时刻与 ECS 销毁时刻的差(>3s 的都是残骸在继续上报) ==")
    kills: dict[int, list[float]] = {}
    for ev in data.kills:
        if ev.victim_uid != 0x7FF and ev.t > 0:
            kills.setdefault(ev.victim_uid, []).append(ev.t)
    for v in data.vehicles:
        if v.died_t is None or v.died_by == "end":
            continue
        near = [t for t in kills.get(v.uid, ()) if v.born_t - 1 <= t <= v.died_t + 30]
        if not near:
            continue
        gap = v.died_t - near[-1]
        if abs(gap) > 3.0:
            print(f"  uid {v.uid} {v.short_cls:26s} {v.player or '-':16s}"
                  f" 击杀 {near[-1]:7.1f}  销毁 {v.died_t:7.1f}  差 {gap:+.1f}s")


if __name__ == "__main__":
    main()
