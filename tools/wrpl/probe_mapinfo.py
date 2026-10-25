"""探针: 回放里能不能拿到 机场 / 战区 / 重生点 / 地图边界。

用户 2026-10-25 问:
  1. 固定翼机场、直升机机场的位置?
  2. 战区(capture zone)的位置?
  3. 重生点位置(固定翼 / 攻击无人机 / 地面坦克)?
  4. 拿到地图图片后, 怎么确定图片在世界里的位置?

做法: 把整份解压后的包流按**可打印串**扫一遍找关键词, 再结合 ECS 的任务对象名
(`t1_airfield_*` 之类) 与它们在地面轨迹里的实际坐标。

用法:
    python tools/wrpl/probe_mapinfo.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402

KEY = re.compile(r"airfield|airbase|helipad|heli_|runway|airstrip|"
                 r"capture|_zone|cZone|capzone|"
                 r"respawn|spawn_point|spawnpoint|"
                 r"attract|grid", re.I)
STR = re.compile(rb"[\x20-\x7e]{4,64}")


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    hits: collections.Counter = collections.Counter()
    per_part: dict[str, set] = collections.defaultdict(set)
    where: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    samples: dict[str, bytes] = {}
    for idx, (fp, info) in enumerate(zip(files, infos), 1):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        spans = [(s, e, pt, ms) for pt, ms, s, e in packets]
        for m in STR.finditer(payload):
            s = m.group().decode("latin-1")
            if not KEY.search(s):
                continue
            hits[s] += 1
            per_part[s].add(idx)
            pos = m.start()
            for a, b, pt, ms in spans:
                if a <= pos < b:
                    sig = payload[a + 2] | (payload[a + 3] << 8)
                    where[s][(pt, f"0x{sig:04X}", ms)] += 1
                    if s not in samples:
                        lo = max(a, pos - 40)
                        samples[s] = (payload[lo:pos + 80], pos - lo, a, sig, ms)
                    break
            else:
                where[s][(-1, "-", 0)] += 1
        del buf, payload, packets

    print(f"=== 包流里带关键词的串 ({len(hits)} 种) —— 附所属通道 ===")
    for s, n in hits.most_common(20):
        w = list(where[s].items())[:3]
        print(f"  {n:>4}  {s[:42]:<44s} "
              f"{[(k[0], k[1]) for k, _c in w]}  "
              f"t={sorted({round(k[2] / 1000, 1) for k, _c in list(where[s].items())[:6]})}")

    print("\n=== 样例上下文(名字前后各 40/80 字节) ===")
    for s in ("t1_tankSpawn01_zone01", "t1_airfield", "capture_zone_circle",
              "missions/airfield_spawn"):
        if s not in samples:
            continue
        seg, off, pkt_s, sig, ms = samples[s]
        print(f"\n  [{s}] ptype=4 sig=0x{sig:04X} t={ms / 1000.0:.2f}s "
              f"包起 {pkt_s} 名字在包内 +{pkt_s and off}")
        print(f"    hex: {seg.hex()}")
        print(f"    txt: {''.join(chr(c) if 32 <= c < 127 else '.' for c in seg)}")

    # ---- 机场 / 任务对象: 名字 + 实际坐标
    print("\n=== 解析出的载具档案里的任务对象名 ===")
    data = W.load_replay(path, expect_server=None)
    objs: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
    for v in getattr(data, "vehicles", ()):
        if not v.cls or "/" in v.cls:
            continue
        rows = data.units.get(v.uid)
        if rows:
            objs[v.cls].append((rows[0][1], rows[0][3]))
    print(f"  任务对象名 {len(objs)} 种")
    for name in sorted(objs):
        ps = objs[name]
        cx = sum(p[0] for p in ps) / len(ps)
        cy = sum(p[1] for p in ps) / len(ps)
        print(f"    {name:<44s} n={len(ps):<3d} 质心 ({cx:8.0f}, {cy:8.0f})")

    # ---- 地图范围(用全部地面 + 空中点)
    xs, zs = [], []
    for rows in data.units.values():
        for _t, x, _y, z in rows:
            xs.append(x)
            zs.append(z)
    if xs:
        print(f"\n=== 本局观测到的世界坐标范围 ===")
        print(f"  x {min(xs):9.0f} .. {max(xs):9.0f}   (跨度 {max(xs) - min(xs):.0f} m)")
        print(f"  z {min(zs):9.0f} .. {max(zs):9.0f}   (跨度 {max(zs) - min(zs):.0f} m)")
    print(f"  本局上报过 {len(data.units)} 个地面 uid; 机场对象见上表")

    # ---- 已知的 8111 侧数据(录制时才有)
    print("\n=== 8111 侧现有能力(录制时才有, 回放里没有) ===")
    print("  /map_info.json -> map_min / map_max = 世界坐标(米), 就是地图图片的**外框**")
    print("  /map_obj.json  -> capture_zone(战区 A/B/C) + respawn_base_tank(坦克重生点)")
    print("  两者坐标都是 0~1 归一化: u = (x - minx)/(maxx - minx)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
