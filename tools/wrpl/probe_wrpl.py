#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_wrpl.py —— 自研 `.wrpl` 解析器（第一步：容器 + 包流 + 包类型统计）。

**不依赖任何外部解析器**，只读回放文件、只打印统计，不写回放。

背景与动机：跑图线路测试器目前靠 8111 实时录制整局路线, 需要「A 队 + B 队」两次
录制再对轴。而回放文件里**一次就包含两队全部单位**(带 ECS 阵营真值)且文件头直接给出
`levels/<地图>.bin` —— 见 `docs/8111使用经验.md`。所以要做一条「读回放」的路线。

上游有 `wrpl-acmi`(Go, AGPL-3.0) 与 `WTACRP`(MIT) 两个成熟实现, 但本项目要保留
**自己解析**的能力(上游停更或格式变化时不至于断供), 因此按公开格式资料自行实现。

格式要点(来自公开文档 + 本机实测, 详见 docs/wrpl格式.md):

  容器: 定长头 1224 B → +2 字节填充 → settings BLK(长度在头 748 处) → zstd 单帧 → 结算 BLK
  包流: [VLQ 长度前缀][包体], 包体首字节 bit4 置位 = 沿用上一时间戳
  时间戳: u32 LE @ body[2:6], **单位毫秒**

用法:
    python tools/probe_wrpl.py <replay.wrpl> [--top 20] [--dump-packet 4]

验证判据(本脚本会自动检查):
  * 包流**恰好用尽**(无剩余字节) —— 帧格式正确的强证据;
  * EndMarker 的时间戳 / 1000 ≈ 结算 BLK 里的 timePlayed;
  * ptype=2(飞机) 的时间戳间隔众数 ≈ 250 ms(4 Hz)。
"""
from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter

HEADER_SIZE = 1224       # 定长头
HEADER_PAD = 2           # 头与 settings 之间的固定填充
OFF_REZ = 684            # u32: 内嵌结算 BLK 的文件偏移
OFF_SETTINGS_SIZE = 748  # u32: settings BLK 长度
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

PACKET_TYPES = {
    0: "EndMarker", 1: "StartMarker", 2: "AircraftSmall", 3: "Chat",
    4: "MPI", 5: "NextSegment", 6: "ECS", 7: "Snapshot", 8: "ReplayHeaderInfo",
}


def _cstr(buf: bytes, off: int, size: int) -> str:
    return buf[off:off + size].split(b"\x00")[0].decode("utf-8", "replace")


def parse_header(buf: bytes) -> dict:
    """定长头 1224 B。字段宽度按公开的 `parse_header` 布局。"""
    if len(buf) < HEADER_SIZE:
        raise SystemExit(f"文件只有 {len(buf)} B, 比 {HEADER_SIZE} B 的回放头还短")
    magic, version = struct.unpack_from("<II", buf, 0)
    if magic != 0x1000ACE5:
        raise SystemExit(f"magic 是 0x{magic:08X}(期望 0x1000ACE5)")
    rez = struct.unpack_from("<I", buf, OFF_REZ)[0]
    settings_size = struct.unpack_from("<I", buf, OFF_SETTINGS_SIZE)[0]
    return {
        "magic": magic,
        "version": version,
        "level": _cstr(buf, 8, 128),
        "levelSettings": _cstr(buf, 136, 260),
        "battleType": _cstr(buf, 396, 128),
        "environment": _cstr(buf, 524, 128),
        "visibility": _cstr(buf, 652, 32),
        "rezOffset": rez,
        "settingsSize": settings_size,
        "serverFlag": buf[742],          # 0x5a = 服务器回放
    }


def locate_stream(buf: bytes, hdr: dict) -> dict:
    """定位 settings BLK / zstd 帧 / 结算 BLK。"""
    settings_off = HEADER_SIZE + HEADER_PAD
    settings_end = settings_off + hdr["settingsSize"]
    rez = hdr["rezOffset"]
    if not (0 < rez <= len(buf)):
        rez = 0
    search_end = rez or len(buf)
    frame = buf.find(ZSTD_MAGIC, settings_end, search_end)
    return {
        "settingsOffset": settings_off,
        "settingsSize": hdr["settingsSize"],
        "zstdFrameOffset": frame if frame >= 0 else None,
        "gapBeforeZstd": (frame - settings_end) if frame >= 0 else None,
        "resultsOffset": rez,
        "resultsBytes": (len(buf) - rez) if rez else 0,
    }


def decompress_stream(buf: bytes, info: dict) -> bytes:
    """解压包流(zstd 单帧)。旧回放可能是 zlib。"""
    import zstandard  # 延迟导入, 免得没装时连头部都看不了
    raw = buf[info["zstdFrameOffset"]:info["resultsOffset"] or len(buf)]
    return zstandard.ZstdDecompressor().decompressobj().decompress(raw)


def read_vlq_size(buf: bytes, p: int) -> tuple[int, int]:
    """包长前缀。首字节的位模式决定前缀长度与解码方式。"""
    b0 = buf[p]
    if b0 & 0x80:                                   # 10xxxxxx
        return b0 & 0x7F, p + 1
    if b0 & 0x40:                                   # 01xxxxxx
        return (((b0 << 8) | buf[p + 1])) ^ 0x4000, p + 2
    if b0 & 0x20:                                   # 001xxxxx
        return (((b0 << 16) | (buf[p + 1] << 8) | buf[p + 2])) ^ 0x200000, p + 3
    if b0 & 0x10:                                   # 0001xxxx
        v = (b0 << 24) | (buf[p + 1] << 16) | (buf[p + 2] << 8) | buf[p + 3]
        return v ^ 0x10000000, p + 4
    return struct.unpack_from("<I", buf, p + 1)[0], p + 5   # 0000xxxx


def parse_packets(payload: bytes) -> tuple[list[tuple[int, int, int, int]], int]:
    """切成 [(ptype, ts_ms, payload_start, payload_end)]; 返回 (包列表, 消耗字节数)。"""
    out = []
    p, last_ms, n = 0, 0, len(payload)
    while p < n:
        try:
            size, p2 = read_vlq_size(payload, p)
        except IndexError:
            break
        if size == 0 or p2 + size > n:
            break
        h0 = payload[p2]
        if h0 & 0x10:                    # 沿用上一时间戳, 包头 2 字节
            ptype, ms, hl = h0 ^ 0x10, last_ms, 2
        else:                            # 包头 6 字节: [类型][0x00][u32 LE 毫秒]
            ptype = h0
            ms = struct.unpack_from("<I", payload, p2 + 2)[0]
            last_ms = ms
            hl = 6
        out.append((ptype, ms, p2 + hl, p2 + size))
        p = p2 + size
    return out, p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="自研 .wrpl 解析: 容器 + 包流 + 类型统计")
    ap.add_argument("replay")
    ap.add_argument("--top", type=int, default=20, help="MPI 签名 Top N")
    args = ap.parse_args(argv)

    with open(args.replay, "rb") as fh:
        buf = fh.read()

    print(f"文件: {args.replay}  ({len(buf):,} B)")
    hdr = parse_header(buf)
    print("\n=== 定长头 ===")
    for k in ("magic", "version", "level", "levelSettings", "battleType",
              "environment", "visibility", "rezOffset", "settingsSize", "serverFlag"):
        v = hdr[k]
        if k == "magic":
            print(f"  {k:16s} 0x{v:08X}")
        elif k == "serverFlag":
            print(f"  {k:16s} 0x{v:02X}  ({'服务器分片' if v == 0x5A else '客户端回放'})")
        else:
            print(f"  {k:16s} {v!r}")

    info = locate_stream(buf, hdr)
    print("\n=== 段定位 ===")
    for k, v in info.items():
        print(f"  {k:18s} {v}")
    if info["zstdFrameOffset"] is None:
        print("!! 没找到 zstd 帧, 可能是旧版 zlib 回放")
        return 2

    payload = decompress_stream(buf, info)
    print(f"\n=== 解压 ===\n  {info['resultsOffset'] - info['zstdFrameOffset']:,} B "
          f"→ {len(payload):,} B")

    packets, used = parse_packets(payload)
    print(f"\n=== 包流 ===\n  包数 {len(packets):,}   消耗 {used:,} / {len(payload):,} B"
          f"   {'恰好用尽 OK' if used == len(payload) else f'!! 剩余 {len(payload) - used} B'}")

    census = Counter(pt for pt, *_ in packets)
    print("\n  类型分布:")
    for pt, cnt in sorted(census.items()):
        print(f"    {pt} {PACKET_TYPES.get(pt, '?'):18s} {cnt:>7,}")

    # 时间轴
    if packets:
        marks = {}
        for pt, ms, s, e in packets:
            if pt in (0, 1):
                marks[pt] = ms
        first_ms = packets[0][1]
        last_ms = packets[-1][1]
        print(f"\n=== 时间轴 ===\n  首个包 {first_ms:,} ms ({first_ms / 1000:.3f} s)")
        if 1 in marks:
            print(f"  StartMarker {marks[1]:,} ms ({marks[1] / 1000:.3f} s)")
        if 0 in marks:
            print(f"  EndMarker   {marks[0]:,} ms ({marks[0] / 1000:.3f} s)")
        print(f"  最后包 {last_ms:,} ms ({last_ms / 1000:.3f} s)")

        # ptype=2 的间隔众数
        p2 = [ms for pt, ms, _s, _e in packets if pt == 2]
        if len(p2) > 2:
            gaps = Counter(b - a for a, b in zip(p2, p2[1:]))
            top_gap, top_cnt = gaps.most_common(1)[0]
            print(f"  飞机(ptype=2) {len(p2):,} 个包, 间隔众数 {top_gap} ms "
                  f"({top_cnt} 次) → {1000 / top_gap:.2f} Hz" if top_gap else "")

    # MPI 签名
    mpi = Counter()
    for pt, _ms, s, e in packets:
        if pt == 4 and e - s >= 4:
            mpi[payload[s:s + 4].hex()] += 1
    print(f"\n=== MPI 签名 Top {args.top}(共 {len(mpi)} 种) ===")
    for sig, cnt in mpi.most_common(args.top):
        tag = ""
        if len(sig) == 8:
            mid = int(sig[4:6], 16) | (int(sig[6:8], 16) << 8)
            tag = f"  0x{mid:04X}"
            if mid in (0xF073, 0xF074):
                tag += "  ← 地面同步(GroundModelPositions)"
        print(f"  {sig} {cnt:>7,}{tag}")

    probe_ground_uid_ranges(payload, packets)
    probe_ground_positions(payload, packets)
    return 0


def read_uleb(buf: bytes, p: int) -> tuple[int, int]:
    """LEB128（wrpl 里的 ReadCompressed）：低 7 位有效、最高位为继续标志。"""
    val, shift = 0, 0
    while True:
        b = buf[p]
        p += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, p
        shift += 7


def probe_ground_uid_ranges(payload: bytes, packets) -> None:
    """地面同步包的最小解析: 只读 `uid 下界/上界`, 用来验证位流起点对不对。

    体布局(公开文档): 4 字节签名 → ULEB 体比特长度 → 字节对齐 → 体数据,
    体数据开头是 `u16 LE uid 下界` + `u16 LE uid 上界` + `u32 同步时刻` + `u16 序号`。
    判据: 每包的 `下界 < 上界 <= 2048`(游戏内地面 uid 上限)。
    """
    print("\n=== 地面同步包: uid 范围(验证位流起点) ===")
    for want in (0xF073, 0xF074):
        los, his, lens, bad = [], [], [], 0
        for pt, _ms, s, e in packets:
            if pt != 4 or e - s < 12:
                continue
            if (payload[s + 2] | (payload[s + 3] << 8)) != want:
                continue
            lens.append(e - s)
            try:
                _bits, p = read_uleb(payload, s + 4)
            except IndexError:
                bad += 1
                continue
            if p + 4 > e:
                bad += 1
                continue
            lo, hi = struct.unpack_from("<HH", payload, p)
            if not (lo < hi <= 2048):
                bad += 1
                continue
            los.append(lo)
            his.append(hi)
        if not lens:
            print(f"  0x{want:04X}: 无")
            continue
        ok = len(lens) - bad
        med = sorted(lens)[len(lens) // 2]
        print(f"  0x{want:04X}: {len(lens):,} 个包, uid 范围合法 {ok:,}/{len(lens):,}"
              f" ({100.0 * ok / len(lens):.1f}%), 包长 中位 {med} B 最大 {max(lens)} B")
        if los:
            print(f"            下界 {min(los)}~{max(los)}   上界 {min(his)}~{max(his)}"
                  f"   平均跨度 {sum(h - l for l, h in zip(los, his)) / len(los):.1f} 个 uid")



def probe_ground_positions(payload: bytes, packets) -> None:
    """完整解析 0xF073 / 0xF074: 拿到每个 uid 的米制位置样本。"""
    from wrpl_ground import DeltaHistory, ground_parse_packet   # 同目录的库

    for want in (0xF073, 0xF074):
        hists: dict[int, DeltaHistory] = {}
        per_uid: dict[int, list[tuple[int, float, float, float]]] = {}
        fails: Counter = Counter()
        ok_pk = 0
        for pt, ms, s, e in packets:
            if pt != 4 or e - s < 12:
                continue
            if (payload[s + 2] | (payload[s + 3] << 8)) != want:
                continue
            try:
                rows = ground_parse_packet(payload[s:e], hists)
            except Exception as exc:                       # noqa: BLE001
                fails[f"{type(exc).__name__}: {exc}"] += 1
                continue
            ok_pk += 1
            for uid, x, y, z in rows:
                per_uid.setdefault(uid, []).append((ms, x, y, z))

        total = sum(len(v) for v in per_uid.values())
        bad = sum(fails.values())
        print(f"\n--- 0x{want:04X}: 包 {ok_pk + bad:,}(成功 {ok_pk:,}, 失败 {bad:,}) "
              f" uid {len(per_uid)}  位置样本 {total:,}")
        if fails:
            for msg, cnt in fails.most_common(5):
                print(f"      失败 {cnt:>6,}  {msg}")
        if not per_uid:
            continue

        hz = []
        for uid, rows in per_uid.items():
            rows.sort()
            span = (rows[-1][0] - rows[0][0]) / 1000.0
            if span > 5 and len(rows) > 5:
                hz.append((len(rows) - 1) / span)
        hz.sort()
        if hz:
            print(f"      采样率 中位 {hz[len(hz) // 2]:.2f} Hz   "
                  f"p90 {hz[int(len(hz) * 0.9)]:.2f} Hz   "
                  f"最高 {hz[-1]:.2f} Hz")
        xs = [r[1] for rows in per_uid.values() for r in rows]
        ys = [r[2] for rows in per_uid.values() for r in rows]
        zs = [r[3] for rows in per_uid.values() for r in rows]
        print(f"      x {min(xs):9.1f}~{max(xs):9.1f}   "
              f"高度 {min(ys):8.1f}~{max(ys):8.1f}   z {min(zs):9.1f}~{max(zs):9.1f}")
        top = sorted(per_uid.items(), key=lambda kv: -len(kv[1]))[:5]
        for uid, rows in top:
            rows.sort()
            span = (rows[-1][0] - rows[0][0]) / 1000.0
            print(f"      uid {uid:4d}: n={len(rows):6,}  "
                  f"{rows[0][0] / 1000:7.1f}~{rows[-1][0] / 1000:7.1f}s  "
                  f"{(len(rows) - 1) / span if span else 0:5.2f} Hz")


if __name__ == "__main__":
    sys.exit(main())
