"""探针: 路线解析质量基线 —— **改切段逻辑之前先量一遍, 改完再量一遍对比**。

答一个问题: 「现在的切割到底错在哪、错多少?」全部指标都在**不改生产代码**的
前提下算出来, 所以可以反复跑。

指标:
  1. **阵营一致率** —— 每条线的 `side` 与结算表的 `team` 是否一致(结算表是权威值)
  2. **人均条数**   —— 每玩家的线数 vs `deaths + 1`(结算表给 `deaths`)
  3. **单线内瞬移** —— 相邻点折算 >200 m 且 >60 m/s 的次数(同一台车里不该出现)
  4. **线尾冻结**   —— 末尾 ≥120 s 且位移 <5 m 的线(疑似残骸拖尾 / 幽灵线)
  5. **空名线条数** —— `player` 为空的线(侧栏会显示 uidN)
  6. **丢弃段数**   —— `ImportStats.dropped`

用法:
    python tools\\probe_route_quality.py "<回放路径>" ["<回放路径>" …]

⚠ 会完整解析每一局(单局 20~40 秒), 串行跑, 别一次塞十几个。
"""
from __future__ import annotations

import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import wrpl_reader as RD                       # noqa: E402
from wt81111g.replay_import import build_record              # noqa: E402
from probe_results_blk import Blk                            # noqa: E402

JUMP_M = 200.0          # 与 replay_import 的判据保持一致
JUMP_MPS = 60.0
FREEZE_SEC = 120.0      # 尾部"几乎不动"持续多久算可疑残骸
FREEZE_M = 5.0
SIDE_TEAM = {"A": 1, "B": 2}


def norm_name(s: str) -> str:
    """名字归一: 去掉所有空白 + 折叠大小写。

    必须归一 —— 结算表里的名字与 Slot 包的显示名会差在**空格数量/尾部空白**上
    (实测 `俄     联     邦` 这种中间带多个空格的), 不归一会把同一人算成两个,
    于是出现"26 人没有线"这种假象。
    """
    return re.sub(r"\s+", "", s or "").casefold()


def resolve_key(rp: dict[str, dict], who: str) -> str | None:
    """归一名字 → 结算表里的键(不含军团前缀)。

    `life.player` 带**军团前缀**(`[-AresX-] 白给传说 哈士奇`), 而结算表只有
    `白给传说 哈士奇` —— 所以先精确匹配, 再退一步用"结算表名字是它的后缀"。
    取最长匹配, 避免 `白给传说 哈士奇` / `白给传说 墨染` 互相误匹配。
    """
    key = norm_name(who)
    if key in rp:
        return key
    best = None
    for k in rp:
        if k and key.endswith(k) and (best is None or len(k) > len(best)):
            best = k
    return best


def find_player(rp: dict[str, dict], who: str) -> dict | None:
    k = resolve_key(rp, who)
    return rp[k] if k else None


# ----------------------------------------------------------------- 结算表
def load_results(path: str):
    """取最后一分片的结算表; 没有(本地录像)就返回 None。"""
    if os.path.isdir(path):
        files, infos = RD._scan_parts(path)
    else:
        files, infos = [path], [RD.read_header_file(path)]
    off = getattr(infos[-1], "results_offset", 0)
    if not off:
        return None
    with open(files[-1], "rb") as fh:
        buf = fh.read()
    if off >= len(buf):
        return None
    try:
        return Blk(buf[off:])
    except (ValueError, IndexError):
        return None


def results_players(blk) -> dict[str, dict]:
    """结算表 → {归一名字: {team, deaths, kills, score, …, _name: 原名}}。"""
    out: dict[str, dict] = {}
    if blk is None:
        return out
    for b in blk.blocks():
        if b["name"] != "player":
            continue
        d = {f["name"]: f["value"] for f in blk.fields(b)}
        nm = d.get("name") or ""
        if nm:
            d["_name"] = nm
            out[norm_name(nm)] = d
    return out


# ----------------------------------------------------------------- 单条线
def scan_life(life, spanx: float, spanz: float) -> tuple[int, float]:
    """返回 (瞬移次数, 尾部冻结秒数)。"""
    pts = life.points
    if len(pts) < 2:
        return 0, 0.0
    jumps = 0
    for a, b in zip(pts, pts[1:]):
        dt = b.t - a.t
        if dt <= 0:
            continue
        dx = (b.x - a.x) * spanx
        dy = (b.y - a.y) * spanz
        d = math.hypot(dx, dy)
        if d > JUMP_M and d / dt > JUMP_MPS:
            jumps += 1
    # 尾部冻结: 从最后一个点往前扫, 直到离开 FREEZE_M 半径
    last = pts[-1]
    fx, fy = last.x * spanx, last.y * spanz
    t_start = last.t
    for p in reversed(pts[:-1]):
        if math.hypot(p.x * spanx - fx, p.y * spanz - fy) > FREEZE_M:
            break
        t_start = p.t
    freeze = last.t - t_start
    return jumps, (freeze if freeze >= FREEZE_SEC else 0.0)


# ----------------------------------------------------------------- 主流程
def measure(path: str) -> None:
    name = os.path.basename(os.path.normpath(path)) or path
    data = RD.load_replay(path)
    rec, st = build_record(data)
    lives = list(rec.sides.get("A", ())) + list(rec.sides.get("B", ()))
    ground = [x for x in lives if x.obj_type == "ground_model"]
    air = [x for x in lives if x.obj_type != "ground_model"]

    rect = getattr(rec, "norm_rect", None) or [0.0, 1.0, 0.0, 1.0]
    spanx = max(1e-6, rect[1] - rect[0])
    spanz = max(1e-6, rect[3] - rect[2])

    print(f"\n=== {name} ===")
    print(f"  日志: 样本 {data.sample_count} / 线 {len(lives)}"
          f"(地面 {len(ground)} / 空中 {len(air)}) / 丢 {st.dropped} / "
          f"映射 {st.mapped} / ECS 载具 {st.ecs_vehicles}(绑 {st.ecs_bound}) "
          f"K={st.ecs_group} / 击杀 {st.kills}")
    # 覆盖: 位置流里有多少 uid 真的有 ECS 档案? 档案里有多少能绑到玩家?
    vans = {v.uid for v in getattr(data, "vehicles", ())}
    uids = set(data.units)
    with_spawn = sum(1 for v in getattr(data, "vehicles", ()) if v.spawn)
    named = sum(1 for v in getattr(data, "vehicles", ()) if v.player)
    print(f"  覆盖: 位置流 uid {len(uids)} / 有档案 {len(uids & vans)}"
          f" / 孤儿 {len(uids - vans)} / 档案条目 {len(getattr(data, 'vehicles', ()))}"
          f"(带出生组名 {with_spawn}, 绑到玩家 {named})")
    # 孤儿 uid 是什么? 统计"整局几乎没动"的比例 —— 静态道具(火车车厢/工事)就是这个特征
    static = moving = tiny = 0
    big = []
    for u in sorted(uids - vans):
        rows_ = data.units.get(u) or []
        if len(rows_) < 2:
            tiny += 1
            continue
        xs = [r[1] for r in rows_]
        zs = [r[3] for r in rows_]
        span = max(max(xs) - min(xs), max(zs) - min(zs))
        if span < 5.0:
            static += 1
        else:
            moving += 1
        if len(rows_) >= 500:
            big.append((len(rows_), u, round(span, 1)))
    big.sort(reverse=True)
    print(f"  孤儿: 静止(<5m) {static} / 移动 {moving} / 采样<2 {tiny}"
          f"; 采样≥500 的 {len(big)} 个: "
          + ", ".join(f"uid{u}({n}点,跨度{s}m)" for n, u, s in big[:6]))

    rp = results_players(load_results(path))
    if not rp:
        print("  (没有结算表: 本地录像或解析失败 —— 阵营/条数两项跳过)")
        return

    # ---- 1. 阵营一致率
    ok = bad = miss = extra = 0
    bad_rows, miss_rows, extra_names = [], [], set()
    seen_keys: set[str] = set()
    for life in lives:
        who = life.player or ""
        if not who:
            continue
        key = resolve_key(rp, who)
        if key is None:
            extra += 1
            extra_names.add(who)
            continue
        seen_keys.add(key)
        d = rp[key]
        want = d.get("team")
        got = SIDE_TEAM.get(life.side)
        if want == got:
            ok += 1
        else:
            bad += 1
            bad_rows.append(f"{who}(线={life.side}→team{got}, 表={want})")
    for key, d in rp.items():
        if key not in seen_keys:
            miss += 1
            miss_rows.append(d.get("_name") or key)
    print(f"  [阵营] 线里 {len(seen_keys)} 人 / 结算表 {len(rp)} 人 → "
          f"一致 {ok}, 不一致 {bad}, 表里有但没线 {miss}, "
          f"有线但表里没有 {extra}({len(extra_names)} 人)")
    for row in bad_rows[:6]:
        print(f"         ✗ {row}")
    if miss_rows:
        print(f"         无线的玩家: {', '.join(miss_rows[:8])}"
              + (" …" if len(miss_rows) > 8 else ""))
    if extra_names:
        print(f"         表里没有的名字: {', '.join(sorted(extra_names)[:8])}"
              + (" …" if len(extra_names) > 8 else ""))

    # ---- 2. 人均条数 vs deaths + 1
    by_player: dict[str, list] = {}
    for life in lives:
        if life.player:
            by_player.setdefault(norm_name(life.player), []).append(life)
    fit = over = under = 0
    off_rows = []
    for key, lst in by_player.items():
        d = find_player(rp, lst[0].player)
        if d is None:
            continue
        deaths = int(d.get("deaths") or 0)
        # 合理解区间: 死光后退出 = deaths 条, 最后一条还活着 = deaths + 1 条。
        # 超出 +1 才是真的可疑(多切/残骸成线); 少于是"没看见"或"提前退出"
        got = len(lst)
        if deaths <= got <= deaths + 1:
            fit += 1
        elif got > deaths + 1:
            over += 1
            off_rows.append(f"{lst[0].player}(死了 {deaths}, 却有 {got} 条, +{got - deaths - 1})")
        else:
            under += 1
            off_rows.append(f"{lst[0].player}(死了 {deaths}, 只 {got} 条, -{deaths - got})")
    print(f"  [条数] 落在 deaths~deaths+1: {fit}/{len(by_player)}; "
          f"偏多 {over} / 偏少 {under}")
    for row in off_rows[:6]:
        print(f"         ~ {row}")

    # ---- 3/4. 瞬移 / 尾部冻结
    tot_jump = 0
    jump_rows, freeze_rows = [], []
    for life in lives:
        j, fz = scan_life(life, spanx, spanz)
        if j:
            tot_jump += j
            jump_rows.append((j, life.player or "?", life.vehicle_cls or "?",
                              life.life_id))
        if fz:
            freeze_rows.append((fz, life.player or "?", life.vehicle_cls or "?",
                                life.life_id))
    jump_rows.sort(reverse=True)
    freeze_rows.sort(reverse=True)
    print(f"  [瞬移] 单线内 >{JUMP_M:.0f}m 且 >{JUMP_MPS:.0f}m/s 共 {tot_jump} 次, "
          f"涉及 {len(jump_rows)} 条线")
    for j, who, car, lid in jump_rows[:5]:
        print(f"         ! {j:>3} 次  {who} / {car}  ({lid})")
    print(f"  [残骸] 尾部位移 <{FREEZE_M:.0f}m 持续 ≥{FREEZE_SEC:.0f}s: "
          f"{len(freeze_rows)} 条")
    for fz, who, car, lid in freeze_rows[:5]:
        print(f"         ? 冻结 {fz:>6.0f}s  {who} / {car}  ({lid})")

    # ---- 5. 空名
    blank = sum(1 for x in lives if not x.player)
    print(f"  [空名] {blank} 条 (占 {blank / max(1, len(lives)) * 100:.0f}%)")


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print("用法: python tools\\probe_route_quality.py <回放路径> [更多…]")
        return 2
    for p in paths:
        try:
            measure(p)
        except Exception as exc:                       # noqa: BLE001 - 探针不中断
            print(f"\n=== {p} ===\n  解析失败: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
