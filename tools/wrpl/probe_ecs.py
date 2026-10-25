"""探针: ECS 包(ptype=6 增量 / 7 快照)里能直接读到什么。

目标: 看 `unit__className` / `unit__playerId` 是不是明文，能不能绕开组件哈希表。
"""
from __future__ import annotations

import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402

PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")
UNIT_FIELD = re.compile(rb"unit__[A-Za-z_]{3,40}")
VEHICLE = re.compile(r"^(us|ussr|uk|germ|jp|cn|it|fr|sw|il)_[a-z0-9_]{3,40}$")


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    fields: collections.Counter = collections.Counter()
    vehicles: collections.Counter = collections.Counter()
    grids: collections.Counter = collections.Counter()
    n6 = n7 = 0
    sample6 = sample7 = b""

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype not in (6, 7):
                continue
            body = payload[s:e]
            if ptype == 6:
                n6 += 1
                if not sample6:
                    sample6 = body[:160]
            else:
                n7 += 1
                if not sample7:
                    sample7 = body[:160]
            for m in UNIT_FIELD.finditer(body):
                fields[m.group().decode("latin-1")] += 1
            for m in PRINTABLE.finditer(body):
                t = m.group().decode("latin-1")
                if VEHICLE.match(t):
                    vehicles[t] += 1
                elif t.endswith("_grid") or t.startswith("sync_"):
                    grids[t] += 1

    print(f"ptype=6 {n6:,} 包, ptype=7 {n7:,} 包")
    print(f"\nunit__* 字段名 {len(fields)} 种:")
    for k, v in fields.most_common(20):
        print(f"  {v:6,}x  {k}")
    print(f"\n载具型号字符串 {len(vehicles)} 种:")
    for k, v in vehicles.most_common(15):
        print(f"  {v:6,}x  {k}")
    print(f"\ngrid / sync 名字 {len(grids)} 种:")
    for k, v in grids.most_common(15):
        print(f"  {v:6,}x  {k}")
    print(f"\nptype=6 头: {sample6.hex()}")
    print(f"ptype=7 头: {sample7.hex()}")


if __name__ == "__main__":
    main(sys.argv[1])
