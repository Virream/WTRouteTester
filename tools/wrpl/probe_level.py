"""探针: 把关卡初始化阶段(ptype=4 / 0xD039 单位定义, 开局 5~10 秒)的**所有对象名**
全部列出来, 看还有什么我们没解析的。

用户 2026-10-25 补充:
  * 重生点有两类无人机: 微型侦察(UCAV, 无攻击) + **大型武装无人机**(带地狱火/AKD10),
    后者的重生位置和 Su-25/F-18 这种正经固定翼**不一样**;
  * 这一局是 **3 个战区**;
  * 坦克重生点分成**两组**(两队各一组, 每组多个点)。

用法:
    python tools/wrpl/probe_level.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from wt81111g import wrpl_reader as W                             # noqa: E402

MID_DEF = 0xD039
NAME_RE = re.compile(r"^[A-Za-z0-9_./@+\-]{2,64}$")


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


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    names: collections.Counter = collections.Counter()
    order: list[str] = []
    per_pkt: dict[int, list[str]] = collections.defaultdict(list)
    packets_info: list[tuple] = []

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
            if ms > 12000:               # 只看开局初始化
                continue
            pl = payload[s:e]
            got = []
            i = 0
            while i < len(pl) - 2:
                a = read_str(pl, i)
                if a is None:
                    i += 1
                    continue
                nm, j = a
                got.append(nm)
                names[nm] += 1
                if nm not in order:
                    order.append(nm)
                i = j
            if got:
                per_pkt[ms].extend(got)
                packets_info.append((ms, len(pl), len(got)))
        del buf, payload, packets

    print(f"=== 开局 0~12 秒的 0xD039 包 {len(packets_info)} 个 ===")
    for ms, ln, n in packets_info[:12]:
        print(f"  t={ms / 1000:6.2f}s  包长 {ln:6d}  串 {n}")
    print(f"\n=== 对象名 {len(names)} 种(按首次出现顺序) ===")
    for nm in order:
        print(f"  x{names[nm]:<3d} {nm}")

    # 按前缀分组统计
    print("\n=== 按前缀分组 ===")
    grp: collections.Counter = collections.Counter()
    for nm in names:
        key = nm.split("_")[0] + "_" + (nm.split("_")[1] if "_" in nm else "")
        grp[key] += 1
    for k, v in grp.most_common(40):
        print(f"  {k:<24s} {v} 种")

    # ---- 这些名字在**所有通道**里出现的位置(找坐标可能藏在别的包里)
    print("\n=== 目标名字在全部通道里的出现位置 ===")
    targets = (b"cz_circle_01", b"cz_circle_02", b"cz_circle_03",
               b"dom_t1_tank_spawn_01_arcade", b"t1_tankSpawn01_zone01",
               b"streak_aircraft01_0")
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        spans = [(s, e, pt, ms) for pt, ms, s, e in packets]
        for tgt in targets:
            pos = payload.find(tgt)
            seen2 = set()
            while pos >= 0:
                for a, b, pt, ms in spans:
                    if a <= pos < b:
                        mid = payload[a + 2] | (payload[a + 3] << 8)
                        k = (pt, mid, b - a)
                        if k not in seen2:
                            seen2.add(k)
                            print(f"  {tgt.decode():<30s} ptype={pt} mid=0x{mid:04X} "
                                  f"包长={b - a:<6d} t={ms / 1000:.2f}s 包内+{pos - a}")
                        break
                pos = payload.find(tgt, pos + 1)
        del buf, payload, packets


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
