"""探针: ptype=6(ECS) 的 control 字节分布, 找 0x26(实体销毁)/0x22(复制) 这类免哈希表的信号。"""
from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402

NAMES = {
    0x11: "CONNECTION_REQUEST_ACCEPTED",
    0x13: "DISCONNECT",
    0x20: "ENTITY_MSG",
    0x21: "ENTITY_MSG_COMPRESSED",
    0x22: "ENTITY_REPLICATION",
    0x23: "ENTITY_REPLICATION_COMPRESSED",
    0x24: "ENTITY_CREATION",
    0x25: "ENTITY_CREATION_COMPRESSED",
    0x26: "ENTITY_DESTRUCTION",
}


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    ctrl: collections.Counter = collections.Counter()
    size_by_ctrl: dict[int, list[int]] = collections.defaultdict(list)
    first16: dict[int, bytes] = {}

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != 6 or e <= s:
                continue
            c = payload[s]
            ctrl[c] += 1
            size_by_ctrl[c].append(e - s)
            if c not in first16:
                first16[c] = payload[s:s + 40]

    total = sum(ctrl.values())
    print(f"ptype=6 共 {total:,} 包\n")
    for c, n in ctrl.most_common():
        sz = sorted(size_by_ctrl[c])
        print(f"  0x{c:02X} {NAMES.get(c, '?'):32s} {n:7,} 包  "
              f"p50={sz[len(sz)//2]:6d}  max={sz[-1]:8d}")
        print(f"        头: {first16[c][:32].hex()}")


if __name__ == "__main__":
    main(sys.argv[1])
