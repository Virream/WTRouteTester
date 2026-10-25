"""探针: dump 一条 Kill 包(0xF058)的完整字段表, 逐字段核对。

参照: t≈123.8s 那条 killer_pid=31, victim_uid=37, 字段值里应有 victim 的玩家槽位。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_events as E                                 # noqa: E402
from wt81111g import wrpl_reader as W                                  # noqa: E402

ZERO = bytes(1)


def as_u32(raw: bytes) -> int | str:
    if len(raw) >= 4:
        return struct.unpack("<I", raw[:4])[0]
    return "-"


def show(idx: int, size: int, raw: bytes) -> None:
    print(f"    f{idx:<2} size={size:<4} hex={raw.hex()}")
    print(f"        u32={as_u32(raw)}  u16={struct.unpack('<H', raw[:2])[0] if len(raw) >= 2 else '-'}"
          f"  lenstr={E._lenstr(raw)!r}")


def dump(payload: bytes, s: int, e: int, t: float) -> None:
    body = payload[s:e]
    print(f"\n--- kill 包 t={t:.1f}s, {len(body)} B ---")
    print(f"  前 16 B: {body[:16].hex()}")
    br = W.BitReader(body, 32)
    start = br.pos
    offset = br.u16()
    mask = int(br.compressed())
    start_body = br.pos
    idxs = [i for i in range(32) if (mask >> i) & 1]
    print(f"  offset={offset} mask=0x{mask:08X} 字段号={idxs}")

    br.pos = start + (offset << 3)
    sizes = [E.read_size(br) for _ in idxs]
    print(f"  尺寸表: {sizes}")

    br.pos = start_body
    for idx, size in zip(idxs, sizes):
        show(idx, size, E._read_bits_bytes(br, size))
    print(f"  读完后剩余 {br.nbits - br.pos} 位 "
          f"(共 {br.nbits}, 用了 {br.pos})")


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    shown = 0
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != W.PACKET_MPI or e - s < 12:
                continue
            if (payload[s + 2] | (payload[s + 3] << 8)) != E.SIG_KILL:
                continue
            if 120.0 <= ms / 1000.0 <= 126.0:
                dump(payload, s, e, ms / 1000.0)
                shown += 1
            if shown >= 2:
                return
    print("没找到 t=120~126s 的 kill 包")


if __name__ == "__main__":
    main(sys.argv[1])
