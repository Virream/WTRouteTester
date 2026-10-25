"""探针: 验证飞机（ptype=2 FlightModel）轨迹解析。"""
from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_air as A                                    # noqa: E402
from wt81111g import wrpl_reader as W                                  # noqa: E402


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    paths: dict[int, list[tuple[float, float, float, float]]] = collections.defaultdict(list)
    n_pkt = n_ok = n_nodata = n_err = 0
    errs: collections.Counter = collections.Counter()

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != A.PACKET_FLIGHT_MODEL:
                continue
            n_pkt += 1
            try:
                rows = A.parse_fm_packet(payload, s, e)
            except Exception as exc:            # noqa: BLE001
                n_err += 1
                errs[str(exc)[:60]] += 1
                continue
            for uid, pos in rows:
                if pos is None:
                    n_nodata += 1
                else:
                    n_ok += 1
                    paths[uid].append((ms / 1000.0, pos[0], pos[1], pos[2]))

    print(f"ptype=2 包 {n_pkt:,} 个, 解出位置 {n_ok:,} 条, 无数据 {n_nodata:,}, "
          f"异常 {n_err:,}")
    if errs:
        print("  异常前 3 类:", errs.most_common(3))
    print(f"飞机 uid {len(paths)} 个")
    for uid, rows in sorted(paths.items(), key=lambda kv: -len(kv[1]))[:10]:
        xs = [r[1] for r in rows]
        ys = [r[2] for r in rows]
        zs = [r[3] for r in rows]
        ts = [r[0] for r in rows]
        print(f"  uid={uid:4d} 点={len(rows):5d} t={ts[0]:7.1f}~{ts[-1]:7.1f}s "
              f"x[{min(xs):9.0f},{max(xs):9.0f}] "
              f"y[{min(ys):7.1f},{max(ys):7.1f}] "
              f"z[{min(zs):9.0f},{max(zs):9.0f}]")


if __name__ == "__main__":
    main(sys.argv[1])
