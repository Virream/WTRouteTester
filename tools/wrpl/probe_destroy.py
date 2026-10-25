"""探针: 解析 ptype=6 / 0x26(实体销毁) 的 eid 列表, 换算成 uid 并与地面轨迹对账。

eid → uid 换算来自上游 carve/entity.go:
    uid = (((eid & 0xFF) << 22) | (eid >> 8)) & 0x3FFFFF & 0x7FF
"""
from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402


def read_eid(br: W.BitReader) -> int:
    """上游 packet.ReadEID（u16 小端，按低位标志决定变体）。"""
    first = br.u16()
    if first & 1:
        return (first >> 2) | (((first & 2) >> 1) << 22)
    if first & 2:
        gen = br.byte()
        return (first >> 2) | (gen << 22)
    second = br.u16()
    return (second << 16) | first


def eid_to_uid(eid: int) -> int:
    idx = (((eid & 0xFF) << 22) | (eid >> 8)) & 0x3FFFFF
    return idx & 0x7FF


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    destructions: list[tuple[float, int, int]] = []      # (t, eid, uid)
    bad = 0
    n_pkt = 0
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != 6 or e <= s or payload[s] != 0x26:
                continue
            n_pkt += 1
            br = W.BitReader(payload[s + 1:e], 0)
            while br.pos + 16 <= br.nbits:
                before = br.pos
                try:
                    eid = read_eid(br)
                except (EOFError, ValueError):
                    break
                if eid == 0 or br.pos == before:
                    break
                destructions.append((ms / 1000.0, eid, eid_to_uid(eid)))
            if len(destructions) == 0:
                bad += 1

    print(f"0x26 包 {n_pkt} 个, 解析出 {len(destructions):,} 个销毁事件")
    if not destructions:
        return
    # 看 eid / uid 的分布
    uids = collections.Counter(u for _t, _e, u in destructions)
    print(f"不同 eid {len({e for _t, e, _u in destructions})} 个, "
          f"不同 uid {len(uids)} 个")
    print(f"前 20 条: ")
    for t, eid, uid in destructions[:20]:
        print(f"  t={t:7.1f}s eid={eid:<12} uid={uid}")

    print(f"\nuid 分布前 15: {uids.most_common(15)}")

    # 与地面轨迹对账
    d = W.load_replay(path)
    ground = set(d.units)
    hit = [u for u in uids if u in ground]
    print(f"\n地面轨迹 uid {len(ground)} 个, 其中有销毁事件的 {len(hit)} 个")
    print(f"  能对上的: {sorted(hit)[:30]}")


if __name__ == "__main__":
    main(sys.argv[1])
