"""探针: (1) 机场防空/工事的阵营  (2) 核弹机去哪了。

用户 2026-10-25:
  * 机场防空现在全归到 A 队 —— 它们的名字形如 `t1_airfield_aaa_small_01` /
    `t2_airfield_aaa_small_01`, 看名字里就带 t1/t2;
  * 一位叫 `______dma` 的玩家中途打够 2500 分起了一架核弹机, 但侧栏里没有这条记录。

用法:
    python tools/wrpl/probe_nuke.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402
from wt81111g.wrpl_entities import EcsCollector, scanned_strings  # noqa: E402

NUKE_KEY = ("nuke", "b_29", "tu_4", "atomic", "atom_bomb", "nbomb", "hvar")


def main(path: str) -> None:
    print("解析 …", flush=True)
    data = W.load_replay(path, expect_server=None)
    names = {p.slot: (getattr(p, "display", "") or getattr(p, "name", ""))
             for p in data.players.values()}
    teams = {p.slot: getattr(p, "team", None) for p in data.players.values()}

    # ---- 1. 机场防空: 这些名字从哪来
    print("\n=== 1. 名字像机场防空的 uid ===")
    air_field = [u for u, n in data.uid_vehicle.items()
                 if "airfield" in str(n).lower() or "aaa" in str(n).lower()]
    print(f"uid_vehicle 里有 airfield/aaa 的: {len(air_field)} 个")
    for u in sorted(air_field)[:10]:
        print(f"  uid={u:<5d} {data.uid_vehicle[u]}  "
              f"在 units: {'是' if u in data.units else '否'}  "
              f"uid_pid={data.uid_pid.get(u)} team={teams.get(data.uid_pid.get(u))}")

    # 击杀/伤害里带型号名的
    from_v = [u for u, n in data.uid_vehicle.items()]
    print(f"uid_vehicle 共 {len(from_v)} 个 uid")

    # ---- 2. 全部空中航迹
    print("\n=== 2. ptype=2 的空中航迹 ===")
    air = {v.uid: v for v in getattr(data, "air_vehicles", ())}
    print(f"air_units {len(data.air_units)} 个; air_vehicles {len(air)} 条")
    for auid, rows in sorted(data.air_units.items()):
        t0, t1 = rows[0][0], rows[-1][0]
        v = air.get(auid)
        tag = ""
        if v is not None:
            tag = (f"{v.short_cls or '?':<28s} team={v.team!s:<5s} "
                   f"{v.player[:14]:<16s} born {v.born_t:6.1f} died "
                   f"{(f'{v.died_t:6.1f}' if v.died_t is not None else '  未定')} "
                   f"({v.died_by})")
        else:
            tag = "**ECS 里没有**"
        print(f"  uid={auid:<5d} {len(rows):>6} 点  {t0:7.1f}..{t1:7.1f}  {tag}")

    # ---- 3. dma 名下所有实体
    print("\n=== 3. 全部 aircraft 实体(按出生时刻) ===")
    for v in sorted(getattr(data, "air_vehicles", ()), key=lambda x: x.born_t):
        print(f"  uid={v.uid:<5d} {(v.short_cls or '-'):<42s} "
              f"born {v.born_t:7.1f} died "
              f"{(f'{v.died_t:7.1f}' if v.died_t is not None else '   未定')} "
              f"({v.died_by:<5s}) slot={v.slot!s:<5s} team={v.team!s:<5s} "
              f"bind={v.bind:<8s} {v.player[:14]:<16s} {v.spawn}")

    print("\n=== 4. dma 名下的全部实体 ===")
    mine = {s for s, n in names.items() if n and "dma" in n.lower()}
    print(f"  匹配槽位 {mine} -> {[names[s] for s in mine]}")
    for v in sorted(getattr(data, "vehicles", ()) + getattr(data, "air_vehicles", ()),
                    key=lambda x: x.born_t):
        if v.slot in mine:
            print(f"  uid={v.uid:<5d} {v.template:<24s} "
                  f"{(v.short_cls or '-'):<28s} born {v.born_t:6.1f} died "
                  f"{(f'{v.died_t:6.1f}' if v.died_t is not None else '  未定')} "
                  f"({v.died_by}) bind={v.bind}")

    # ---- 4. 全库扫 nuke 相关明文
    print("\n=== 4. 全库扫 nuke/b_29/tu_4 相关明文 ===")
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]
    ecs = EcsCollector()
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype == 6 and e > s:
                ecs.feed(payload, s, e, ms)
        del buf, payload, packets
    hits = collections.Counter()
    for c in ecs._creations:
        for _o, s in scanned_strings(c.block, 3, 80):
            low = s.lower()
            if any(k in low for k in NUKE_KEY):
                hits[(c.template, s)] += 1
    if hits:
        for (tpl, s), n in hits.most_common(30):
            print(f"  {n:>3}  [{tpl}] {s}")
    else:
        print("  （构造块里没有 nuke 相关字样）")
    print(f"\n  模板分布: {dict(collections.Counter(c.template for c in ecs._creations).most_common())}")
    print(f"  解析统计: {dict(ecs.stats)}")

    # ---- 5. 末尾的击杀事件(核弹机会有大动静)
    print("\n=== 5. 最后 20 条击杀事件 ===")
    print("   t       killer_uid  kpid  victim_uid  vpid  型号/武器")
    for ev in sorted(data.kills, key=lambda x: x.t)[-20:]:
        print(f"  {ev.t:7.1f}  {ev.killer_uid:<10d} {ev.killer_pid:<5d} "
              f"{ev.victim_uid:<10d} {ev.victim_pid:<5d} "
              f"{str(getattr(ev, 'vehicle', ''))[:24]:<26s} "
              f"{str(getattr(ev, 'weapon', ''))[:20]}")

    # ---- 6. 机场防空的型号名从哪来
    print("\n=== 6. 名字里带 airfield/aaa 的 uid(含击杀事件来源) ===")
    vo: dict[int, str] = dict(data.uid_vehicle)
    for ev in data.kills:
        if ev.killer_uid != W.EXT_MASK_UID and ev.vehicle:
            vo.setdefault(ev.killer_uid, ev.vehicle)
    af = {u: n for u, n in vo.items()
          if "airfield" in str(n).lower() or "_aaa" in str(n).lower()}
    print(f"  共 {len(af)} 个 uid; 样例:")
    for u, n in sorted(af.items())[:12]:
        print(f"    uid={u:<5d} {n:<32s} 在 units: {'是' if u in data.units else '否'}")
    pref = collections.Counter(n.split("_")[0] for n in af.values())
    print(f"  名字前缀分布: {dict(pref)}")

    # ---- 7. 直接在全量解压流里搜核弹机型号
    print("\n=== 7. 全量包流里搜核弹机型号 ===")
    keys = (b"nuke", b"b_29", b"B-29", b"tu_4", b"Tu-4", b" lancaster",
            b"atomic_bomb", b"nbomb")
    found = collections.Counter()
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        low = payload.lower()
        for k in keys:
            c = low.count(k.lower())
            if c:
                found[k] += c
        del buf, payload
    if found:
        for k, n in found.most_common():
            print(f"  {k.decode('latin-1')}: {n} 次")
    else:
        print("  （整份包流里一次都没出现）")

    # ---- 9. "nuke" 出现的位置与上下文
    print("\n=== 9. nuke 出现位置上下文 ===")
    for idx, (fp, info) in enumerate(zip(files, infos), 1):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        low = payload.lower()
        pos = low.find(b"nuke")
        while pos >= 0:
            a, b = max(0, pos - 90), min(len(payload), pos + 90)
            seg = payload[a:b]
            print(f"  分片 {idx} 偏移 {pos}:")
            print(f"    hex : {seg.hex()}")
            print(f"    txt : {''.join(chr(c) if 32 <= c < 127 else '.' for c in seg)}")
            # 这段字节落在哪个包里 -> 拿到时刻
            pk, _u = W.parse_packets(payload)
            for ptype_, ms_, s_, e_ in pk:
                if s_ <= pos < e_:
                    print(f"    -> ptype={ptype_} 时刻 {ms_ / 1000.0:.1f}s")
                    break
            else:
                # 落在包外(比如结算 BLK 区)
                print(f"    -> 不在任何数据包里(offset {pos} / 共 {len(payload)})")
            pos = low.find(b"nuke", pos + 1)
        del buf, payload

    # ---- 10. dma(slot 1)相关的全部击杀
    print("\n=== 10. dma(slot=1) 相关的击杀 ===")
    for ev in sorted(data.kills, key=lambda x: x.t):
        if ev.killer_pid == 1 or ev.victim_pid == 1:
            print(f"  {ev.t:7.1f}  killer uid={ev.killer_uid:<5d} pid={ev.killer_pid:<3d} "
                  f"victim uid={ev.victim_uid:<5d} pid={ev.victim_pid:<3d} "
                  f"{str(getattr(ev, 'vehicle', ''))[:26]:<28s} "
                  f"{str(getattr(ev, 'weapon', ''))[:22]}")

    print("\n=== 11. 牵涉空中 uid 的击杀 ===")
    airu = set(data.air_units) | {v.uid for v in getattr(data, "air_vehicles", ())
                                  if v.cls}
    for ev in sorted(data.kills, key=lambda x: x.t):
        if ev.killer_uid in airu or ev.victim_uid in airu:
            kk = "空" if ev.killer_uid in airu else "  "
            vk = "空" if ev.victim_uid in airu else "  "
            print(f"  {ev.t:7.1f}  {kk}{ev.killer_uid:<5d}(pid {ev.killer_pid:<3d}) -> "
                  f"{vk}{ev.victim_uid:<5d}(pid {ev.victim_pid:<3d})  "
                  f"{str(getattr(ev, 'vehicle', ''))[:24]:<26s} "
                  f"{str(getattr(ev, 'weapon', ''))[:20]}")

    print("\n=== 12. 没有 ECS 档案的空中 uid ===")
    for auid, rows2 in sorted(data.air_units.items()):
        if any(v.uid == auid for v in getattr(data, "air_vehicles", ())):
            continue
        print(f"  uid={auid}  {len(rows2)} 点  {rows2[0][0]:.1f}..{rows2[-1][0]:.1f}s "
              f"首点 {[round(x, 1) for x in rows2[0][1:]]} "
              f"末点 {[round(x, 1) for x in rows2[-1][1:]]}")

    # ---- 13. 用「起飞位置离哪个机场近」判断空中单位的阵营
    print("\n=== 13. 机场聚类 + 空中单位首点归属 ===")
    aaa_pos: dict[int, list[tuple[float, float]]] = {1: [], 2: []}
    for v in getattr(data, "vehicles", ()):
        if not v.cls or "airfield" not in v.cls.lower() and "_aaa" not in v.cls.lower():
            continue
        m = __import__("re").match(r"^t([12])_", v.cls)
        rows_ = data.units.get(v.uid)
        if not m or not rows_:
            continue
        aaa_pos[int(m.group(1))].append((rows_[0][1], rows_[0][3]))
    for tm in (1, 2):
        ps = aaa_pos[tm]
        if ps:
            cx = sum(p[0] for p in ps) / len(ps)
            cy = sum(p[1] for p in ps) / len(ps)
            print(f"  team{tm} 机场群 {len(ps)} 个防空点, 质心 ({cx:.0f}, {cy:.0f})")

    def guess_team(auid: int) -> tuple[int | None, float, float]:
        rows_ = data.air_units.get(auid)
        if not rows_ or not aaa_pos[1] or not aaa_pos[2]:
            return None, 0.0, 0.0
        x, z = rows_[0][1], rows_[0][3]
        d = {}
        for tm in (1, 2):
            ps = aaa_pos[tm]
            d[tm] = min(((x - p[0]) ** 2 + (z - p[1]) ** 2) ** 0.5 for p in ps)
        tm = 1 if d[1] < d[2] else 2
        return tm, d[1], d[2]

    print("  uid   已知team  首点推测  d(机场1)  d(机场2)")
    ok = bad = 0
    for auid in sorted(data.air_units):
        v = next((x for x in getattr(data, "air_vehicles", ()) if x.uid == auid), None)
        gt = v.team if v is not None else None
        g, d1, d2 = guess_team(auid)
        mark = ""
        if gt is not None and g is not None:
            if gt == g:
                ok += 1
            else:
                bad += 1
                mark = "  <<< 不符"
        print(f"  {auid:<5d} {gt!s:<8s} {g!s:<8s} {d1:9.0f} {d2:9.0f}{mark}")
    print(f"  已知阵营的可验证样本: 一致 {ok} / 不符 {bad}")

    # ---- 14. 独立验证: 地面载具(已知 team)的出生点离哪个机场群近
    print("\n=== 14. 地面载具出生点 vs 机场群(独立验证) ===")
    if aaa_pos[1] and aaa_pos[2]:
        cross2 = collections.Counter()
        for v in getattr(data, "vehicles", ()):
            if v.team is None or "tank" not in v.template:
                continue
            rows_ = data.units.get(v.uid)
            if not rows_:
                continue
            # 只取出生窗口附近的第一个点
            near = [r for r in rows_ if r[0] >= v.born_t - 2.0]
            if not near:
                continue
            x, z = near[0][1], near[0][3]
            d = {tm: min(((x - p[0]) ** 2 + (z - p[1]) ** 2) ** 0.5
                         for p in aaa_pos[tm]) for tm in (1, 2)}
            near_tm = 1 if d[1] < d[2] else 2
            cross2[(v.team, near_tm)] += 1
        for (tm, near_tm), n in sorted(cross2.items()):
            print(f"  team={tm} 的坦克出生点更靠近 机场群{near_tm}: {n} 辆")


    aaa_name = {v.uid: v.cls for v in getattr(data, "vehicles", ())
                if v.cls and ("airfield" in v.cls.lower() or "_aaa" in v.cls.lower())}
    print(f"  AAA uid {len(aaa_name)} 个; 前缀分布 "
          f"{dict(collections.Counter(n.split('_')[0] for n in aaa_name.values()))}")
    cross = collections.Counter()
    for ev in data.kills:
        if ev.killer_uid in aaa_name:
            nm = aaa_name[ev.killer_uid]
            cross[(nm.split("_")[0], teams.get(ev.victim_pid))] += 1
    if cross:
        for (pref_, tm), n in sorted(cross.items(), key=lambda kv: str(kv[0])):
            print(f"    {pref_} 击落 team={tm} 的玩家: {n} 次")
    else:
        print("    （没有 AAA 的击杀记录，前缀无法用击杀验证）")




if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
