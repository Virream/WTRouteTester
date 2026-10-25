"""按 ptype 分组统计签名，找飞机/直升机的位置通道。

用法: python tools/wrpl/probe_signatures.py <回放文件或分片目录>
"""
from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    by_ptype: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    sizes: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    head: dict[tuple[int, int], bytes] = {}
    tms: dict[tuple[int, int], list[int]] = collections.defaultdict(list)

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            key = (ptype, -1)
            if ptype == W.PACKET_MPI and e - s >= 12:
                key = (ptype, payload[s + 2] | (payload[s + 3] << 8))
            by_ptype[ptype][key[1]] += 1
            sizes[key].append(e - s)
            tms[key].append(ms)
            if key not in head:
                head[key] = payload[s:s + 40]

    for ptype in sorted(by_ptype, key=lambda t: -sum(by_ptype[t].values())):
        total = sum(by_ptype[ptype].values())
        if total < 300:
            continue
        print(f"\n===== ptype={ptype}  共 {total:,} 包 =====")
        for mid, n in by_ptype[ptype].most_common(8):
            sz = sorted(sizes[(ptype, mid)])
            ts = sorted(tms[(ptype, mid)])
            span = (ts[-1] - ts[0]) / 1000.0 if len(ts) > 1 else 0
            hz = n / span if span > 0 else 0
            tag = "MPI签名 0x%04X" % mid if mid >= 0 else "非 MPI"
            mark = "  <- 地面(已解)" if mid in (W.SIG_GROUND_CLIENT, W.SIG_GROUND_SERVER) else ""
            print(f"  {tag:16s} {n:7,} 包  {hz:5.2f} Hz  负载 p50={sz[len(sz)//2]:4d} "
                  f"min={sz[0]:4d} max={sz[-1]:5d}{mark}")
            print(f"        头: {head[(ptype, mid)][:28].hex()}")


if __name__ == "__main__":
    main(sys.argv[1])
