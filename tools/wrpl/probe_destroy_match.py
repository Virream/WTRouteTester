"""验证: 实体销毁(0x26)的时刻是否与地面轨迹的结束时刻吻合。

若吻合 → 这就是「载具死亡」的精确信号, 且**不需要 ECS 哈希表**。
uid 换算: uid = (eid >> 8) & 0x7FF
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402


def read_eid(br: W.BitReader) -> int:
    first = br.u16()
    if first & 1:
        return (first >> 2) | (((first & 2) >> 1) << 22)
    if first & 2:
        gen = br.byte()
        return (first >> 2) | (gen << 22)
    second = br.u16()
    return (second << 16) | first


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    # uid -> 销毁时刻列表
    dead_at: dict[int, list[float]] = {}
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != 6 or e <= s or payload[s] != 0x26:
                continue
            br = W.BitReader(payload[s + 1:e], 0)
            while br.pos + 16 <= br.nbits:
                before = br.pos
                try:
                    eid = read_eid(br)
                except (EOFError, ValueError):
                    break
                if eid == 0 or br.pos == before:
                    break
                uid = (eid >> 8) & 0x7FF
                dead_at.setdefault(uid, []).append(ms / 1000.0)
        br = None

    d = W.load_replay(path)
    print(f"地面 uid {len(d.units)} 个; 有销毁记录的 {len(set(dead_at) & set(d.units))} 个\n")
    print(f"{'uid':>4} {'轨迹结束':>9} {'销毁时刻(前3)':>26} {'差':>8}")
    ok = close = far = 0
    for uid, rows in sorted(d.units.items()):
        ts = dead_at.get(uid)
        if not ts:
            continue
        ts.sort()
        t_end = rows[-1][0]
        best = min(ts, key=lambda x: abs(x - t_end))
        diff = best - t_end
        ok += 1
        if abs(diff) <= 3.0:
            close += 1
        elif abs(diff) > 30:
            far += 1
        if ok <= 40:
            print(f"{uid:4d} {t_end:9.1f} {str([round(x, 1) for x in ts[:3]]):>26} "
                  f"{diff:+8.1f}")

    print(f"\n有销毁记录的 {ok} 个 uid 中: 差 <=3s 的 {close} 个 "
          f"({100*close/max(1,ok):.0f}%), 差 >30s 的 {far} 个")


if __name__ == "__main__":
    main(sys.argv[1])
