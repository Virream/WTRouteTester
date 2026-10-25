"""统计: 残骸到底是不是"每辆车都有" / ECS 的 uid 到底有多可信。

两件事分开算, 免得把 AI 单位混进"玩家载具"里把结论带歪:
  A) 只看**玩家载具**(有出生组名 `tN_playerNN_K` 的), uid 在位置流里有没有采样;
  B) 位置流里每个 uid: 最后一次击杀之后还剩多少采样、这些采样是不是**一动不动**
     (= 残骸), 以及它们持续到什么时候。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import EXT_MASK_UID, load_replay        # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)
    known = set(data.units)
    print(f"level={data.level} battle={data.battle_type} t_last={data.t_last:.1f}")
    print(f"ECS 地面档案 {len(data.vehicles)} / 位置流 uid {len(known)}")

    player = [v for v in data.vehicles if v.spawn]
    ai = [v for v in data.vehicles if not v.spawn]
    ok_p = [v for v in player if v.uid in known]
    ok_a = [v for v in ai if v.uid in known]
    print("\n===== A) ECS uid 可信度 =====")
    print(f"  玩家载具(有出生组名) {len(player)} 台: uid 有采样 {len(ok_p)}"
          f"  ({len(ok_p) / max(1, len(player)) * 100:.1f}%)")
    print(f"  AI / 工事 {len(ai)} 台: uid 有采样 {len(ok_a)}"
          f"  ({len(ok_a) / max(1, len(ai)) * 100:.1f}%)  ← 它们本来就不上报位置")
    bad = [v for v in player if v.uid not in known]
    print(f"  玩家载具里 uid 对不上的 {len(bad)} 台:")
    for v in bad:
        print(f"    uid{v.uid:<5d} {v.short_cls:26s} {v.player or '-':22s}"
              f" born {v.born_t:7.1f} spawn={v.spawn}")

    kills: dict[int, list[float]] = {}
    for ev in data.kills:
        if ev.victim_uid != EXT_MASK_UID and ev.t > 0:
            kills.setdefault(ev.victim_uid, []).append(ev.t)

    print("\n===== B) 残骸: 击杀之后的静态尾巴 =====")
    tails = []
    for uid, rows in sorted(data.units.items()):
        ks = kills.get(uid)
        if not ks or not rows:
            continue
        k = ks[-1]
        after = [r for r in rows if r[0] > k]
        if not after:
            tails.append((uid, k, 0, 0.0, 0.0, rows[-1][0]))
            continue
        base = after[0]
        dx = max(abs(r[1] - base[1]) for r in after)
        dz = max(abs(r[3] - base[3]) for r in after)
        tails.append((uid, k, len(after), max(dx, dz),
                      after[-1][0] - after[0][0], rows[-1][0]))
    tails.sort(key=lambda t: -t[2])
    print(f"  有击杀记录的 uid: {len(tails)} 个")
    n_tail = sum(1 for t in tails if t[2] > 0)
    n_static = sum(1 for t in tails if t[2] > 0 and t[3] < 5.0)
    print(f"  击杀之后还有采样的: {n_tail} / {len(tails)}"
          f"   其中**完全不动**(<5 m)的: {n_static}")
    if n_tail:
        dur = [t[4] for t in tails if t[2] > 0]
        cnt = [t[2] for t in tails if t[2] > 0]
        dur.sort()
        cnt.sort()
        print(f"  尾巴时长 p50 {dur[len(dur) // 2]:.0f}s  p90 {dur[int(len(dur) * .9)]:.0f}s"
              f"  最长 {dur[-1]:.0f}s")
        print(f"  尾巴点数 p50 {cnt[len(cnt) // 2]}  p90 {cnt[int(len(cnt) * .9)]}"
              f"  最多 {cnt[-1]}")
    print("  明细(前 12 个, 按尾巴点数排序):")
    for uid, k, n, move, dur, last in tails[:12]:
        print(f"    uid{uid:<5d} 击杀 {k:7.1f}s  尾巴 {n:5d} 点 / {dur:6.1f}s"
              f"  最大位移 {move:7.2f} m  报到 {last:7.1f}s")

    # 反例: 击杀之后**有位移**的(说明是玩家重生在同一 uid 上, 不是残骸)
    print("\n  ❗ 击杀后尾巴**有位移**(>5 m)的 —— 这些才是「同一 uid 换了车」:")
    for uid, k, n, move, dur, last in tails:
        if n and move >= 5.0:
            print(f"    uid{uid:<5d} 击杀 {k:7.1f}s  尾巴 {n:5d} 点 / {dur:6.1f}s"
                  f"  最大位移 {move:7.1f} m")


if __name__ == "__main__":
    main()
