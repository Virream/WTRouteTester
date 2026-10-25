"""探针: 回放里有没有「重生点 / 阵营」的直接数据。

用户思路: 玩家一定在重生点重生, 拿到「重生点 -> 阵营」就能反推 uid 的阵营;
        只有分体防空这类可能不在重生点重生的单位需要另解。

这里先扫全库:
  1. ECS 构造块里的**明文串**, 找 spawn / t1_ / t2_ / grid / base / point 之类关键词;
  2. 把 grid 类模板(t1_airfield_top 之类的地面生成区)和 sync_player(玩家槽位)整块 dump;
  3. 统计 MPI 通道里有没有疑似出生事件的信号。

用法:
    python tools/wrpl/probe_spawn.py "回放文件\\服务器"
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

from wt81111g import wrpl_reader as W                                  # noqa: E402
from probe_ecs_entity import (                                        # noqa: E402
    BitReader, Templates, parse_create_packet, scanned_strings, CTRL_CREATE)

KEY = re.compile(r"spawn|respawn|t[12]_|grid|base_|_base|point|team|side|"
                 r"player|point", re.I)


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    T = Templates()
    rows: list[dict] = []
    stats: collections.Counter = collections.Counter()
    mpi_sig: collections.Counter = collections.Counter()

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype == 6 and e > s and payload[s] in CTRL_CREATE:
                parse_create_packet(payload, s, e, ms, T, rows, stats)
            elif ptype == 4 and e - s >= 4:
                mpi_sig[payload[s + 2] | (payload[s + 3] << 8)] += 1
        del buf, payload, packets

    print(f"构造记录 {len(rows):,} 条; 模板 {len(T.by_tid)} 个")

    # ---- 1. 明文串里带关键词的
    print("\n=== 块内明文里带 spawn/team/grid/point 等关键词的串 ===")
    hits: collections.Counter = collections.Counter()
    where: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in rows:
        for _off, s in scanned_strings(r["block"], 3, 80):
            if KEY.search(s):
                hits[s] += 1
                where[s][r["name"]] += 1
    if not hits:
        print("  （没有）")
    for s, n in hits.most_common(40):
        top = ", ".join(f"{t}x{k}" for t, k in where[s].most_common(3))
        print(f"  {n:>5}  {s[:60]:<60s} {top}")

    # ---- 2. 模板名本身带关键词的
    print("\n=== 模板名带关键词的 ===")
    for name, n in collections.Counter(r["name"] for r in rows).most_common():
        if KEY.search(name):
            print(f"  {n:>5}  {name}")

    # ---- 3. grid / sync_player 块整块 dump
    print("\n=== 关键模板块 dump ===")
    for want in ("vehicles_grid", "attract_points_grid", "interactable_grid",
                 "sync_player", "sync_player+bot_player", "destroy_ri_box_area"):
        sub = [r for r in rows if r["name"] == want]
        if not sub:
            continue
        r = sub[0]
        blk, vs = r["block"], r["vstart"]
        print(f"\n  [{want}] {len(sub)} 个实例, 块 {len(blk)} B, vstart={vs}")
        print(f"    hex: {blk[:min(len(blk), 160)].hex()}")
        print(f"    串: {[s for _o, s in scanned_strings(blk, 1, 80)][:20]}")
        # 块内所有 f32(前后各留一点上下文)
        vals = []
        for i in range(0, len(blk) - 4, 4):
            v = struct.unpack_from("<f", blk, i)[0]
            if abs(v) > 1e-6 and abs(v) < 1e7:
                vals.append((i, round(v, 3)))
        print(f"    f32(4B 对齐, 非零): {vals[:24]}")

    # ---- 4. MPI 通道签名普查(找出生/重生相关)
    print("\n=== MPI 通道签名 top 30(0xF0xx / 0xD0xx) ===")
    for sig, n in mpi_sig.most_common(30):
        print(f"  0x{sig:04X} {n:>8,}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
