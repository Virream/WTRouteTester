"""探针: 解 `0xD039`(00025839) 单位定义包里的 机场 / 直升机坪 / 重生点 / 战区 坐标。

**方法照搬上游 ShioUmiusi/WTACRP 的 `extract_runway_markers()`**(只取方法, 代码自写):
  1. 在包里按「长度前缀串」精确匹配名字(不能用 find, 否则 `t1_airfield` 会命中
     `t1_airfield_top_reload` 的前缀);
  2. `tail = 名字后 260 字节`;
  3. **航向**: 跳过一个长度前缀串(模型名)后, 3×f32 且模长 ≈ 1;
  4. **位置**: tail 里所有「像位置」的三连, 然后**聚类取最大簇** ——
     因为同一标记点在包里重复采样, 而 tail 里也混着大量无关浮点数,
     直接平均会把质心拉到几公里外(上游原话: "我第一版就踩了")。

本局实测结构(ptype=4, 开局 6~8 秒):
    条目  0x0B "t1_airfield"
          0x1D "dynaf_pg_1line_3000_universal"
          36B 3x3 旋转矩阵(三行都是单位向量)
          12B 平移(3×f32)
包里还有 `t1_tankSpawn01_zone01..20` 这类**纯名字列表**、`capture_zone_circle`。

用法:
    python tools/wrpl/probe_units.py "回放文件\\服务器"
"""
from __future__ import annotations

import math
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402

MID_DEF = 0xD039                    # 0x00025839 = 单位定义
NAME_RE = re.compile(r"^[A-Za-z0-9_./@+\-]{3,64}$")
# 上游的 RUNWAY_MARKERS 是 `t1_airfield_top[_reload]`; 本局名字更短, 所以按前缀认
WANT = re.compile(r"^(t[12]_(airfield|helipad|tankSpawn)|missions/|capture_zone)", re.I)
TAIL = 300


def plausible(x: float, y: float, z: float) -> bool:
    return (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)
            and abs(x) < 45000 and abs(z) < 45000
            and -500 <= y <= 12000 and max(abs(x), abs(z)) > 500)


def read_str(pl: bytes, i: int) -> tuple[str, int] | None:
    if i >= len(pl):
        return None
    L = pl[i]
    if not (2 <= L <= 96) or i + 1 + L > len(pl):
        return None
    try:
        s = pl[i + 1:i + 1 + L].decode("ascii")
    except UnicodeDecodeError:
        return None
    return (s, i + 1 + L) if NAME_RE.match(s) else None


def read_entry(pl: bytes, j: int) -> tuple[str, tuple[float, float, float] | None]:
    """j = 名字之后。若紧跟长度前缀串(模型名)则跳过, 然后 **36B 旋转矩阵 + 12B 平移**。

    实测本局全部条目都是这个布局(2026-10-25, avg_finland):
        t1_airfield  model=dynaf_pg_1line_3000_universal  平移 (-6289, 70, 12528)
        t2_airfield  model=dynaf_pg_1line_3000_universal  平移 (11859, 154, -7977)
        t1_helipad   model=dynHelipad_beton_a_ussr        平移 ( 1059, 26,  5127)
        capture_zone_circle  **无模型名**                 平移 ( 1832,  5,  2456)
    平移紧接在矩阵之后, **不是**靠扫 tail 猜的 —— 上游那个聚类法只是兵底。
    """
    k = j
    model = ""
    s = read_str(pl, k)
    if s is not None:
        model, k = s
    o = k + 36
    if o + 12 > len(pl):
        return model, None
    x, y, z = struct.unpack_from("<3f", pl, o)
    return model, ((x, y, z) if plausible(x, y, z) else None)


def cluster_positions(tail: bytes) -> tuple[tuple[float, float, float] | None, int, int]:
    """tail 里所有「像位置」的三连, 聚类(30 m)取最大簇。返回 (质心, 簇大小, 候选数)。"""
    pts: list[tuple[float, float, float]] = []
    last = -99
    for o in range(0, max(0, len(tail) - 12)):
        if o < last + 12:              # 跳过重叠窗口, 否则同一个三连会被数很多次
            continue
        v = struct.unpack_from("<3f", tail, o)
        if plausible(*v):
            pts.append((round(v[0], 2), round(v[1], 2), round(v[2], 2)))
            last = o
    if not pts:
        return None, 0, 0
    clusters: list[dict] = []
    for p in pts:
        for c in clusters:
            q = c["c"]
            if math.dist((p[0], p[2]), (q[0], q[2])) < 30:
                c["m"].append(p)
                c["c"] = (sum(x[0] for x in c["m"]) / len(c["m"]),
                          sum(x[1] for x in c["m"]) / len(c["m"]),
                          sum(x[2] for x in c["m"]) / len(c["m"]))
                break
        else:
            clusters.append({"c": p, "m": [p]})
    clusters.sort(key=lambda c: -len(c["m"]))
    best = clusters[0]
    return best["c"], len(best["m"]), len(pts)


def heading_after_model(tail: bytes) -> tuple[str, tuple[float, float, float]] | None:
    """跳过一个长度前缀串(模型名), 其后 3×f32 且模长≈1 = 航向。"""
    for skip in range(0, 6):
        if skip >= len(tail):
            return None
        L = tail[skip]
        if not (3 <= L <= 80) or skip + 1 + L + 12 > len(tail):
            continue
        try:
            model = tail[skip + 1:skip + 1 + L].decode("ascii")
        except UnicodeDecodeError:
            continue
        if not NAME_RE.match(model):
            continue
        v = struct.unpack_from("<3f", tail, skip + 1 + L)
        n = math.sqrt(sum(x * x for x in v))
        if 0.97 < n < 1.03:
            return model, v
    return None


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    n_pkt = 0
    rows: list[tuple] = []
    seen: set[str] = set()
    dumped: set[str] = set()

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != 4 or e - s < 8:
                continue
            if (payload[s + 2] | (payload[s + 3] << 8)) != MID_DEF:
                continue
            n_pkt += 1
            pl = payload[s:e]
            i = 0
            while i < len(pl) - 4:
                a = read_str(pl, i)
                if a is None:
                    i += 1
                    continue
                name, j = a
                if not WANT.search(name):
                    i = j
                    continue
                tail = pl[j:j + TAIL]
                model, pos = read_entry(pl, j)
                if pos is None:                       # 兜底: 扫 tail 聚类
                    cen, ncl, nall = cluster_positions(tail)
                    pos = cen
                    conf = f"{ncl}/{nall}"
                else:
                    cen, conf = pos, "精确"
                hd = heading_after_model(tail)
                if name.startswith("t1_tankSpawn01_zone01") and \
                        "tankdump" not in dumped:
                    dumped.add("tankdump")
                    print(f"\n  [{name}] 名字后 120 字节:")
                    print(f"    hex: {pl[j:j + 120].hex()}")
                    print(f"    txt: {''.join(chr(c) if 32 <= c < 127 else '.' for c in pl[j:j + 120])}")
                if name in ("t1_airfield", "t2_airfield", "t1_helipad",
                            "capture_zone_circle") and name not in dumped:
                    dumped.add(name)
                    print(f"\n  [{name}] 名字后 {TAIL} 字节:")
                    print(f"    hex: {tail[:96].hex()}")
                    fv = []
                    for k in range(0, min(len(tail), 96) - 4, 4):
                        fv.append(round(struct.unpack_from("<f", tail, k)[0], 2))
                    print(f"    f32(4B 对齐): {fv}")
                key = f"{name}@{ms}@{cen}"
                if key in seen:
                    i = j
                    continue
                seen.add(key)
                rows.append((name, ms / 1000.0, cen, conf,
                             hd[0] if hd else "", hd[1] if hd else None))
                i = j
        del buf, payload, packets

    print(f"=== 0xD039 单位定义包 {n_pkt} 个; 命中条目 {len(rows)} 条 ===")
    print("  名字                               t      位置(质心)              精度  模型  航向")
    for name, t, cen, conf, model, hd in rows:
        cs = (f"({cen[0]:8.0f},{cen[1]:6.0f},{cen[2]:8.0f})" if cen else " " * 26)
        print(f"  {name:<34s} {t:5.2f}  {cs}  {conf:<8s} "
              f"{model[:26]:<28s} "
              f"{('(' + ','.join(f'{v:+.2f}' for v in hd) + ')') if hd else ''}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
