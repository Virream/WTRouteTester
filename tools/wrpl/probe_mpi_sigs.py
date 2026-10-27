"""探针: 盘点 MPI(ptype=4) 报文的**报文 id 分布** —— 找有没有"出生/部署"事件包。

答案(2026-10-27 实测, 详见 `docs/录像解析调研.md` §2.6): **没有**。有两条实测事实:
  * 报文目录(`wrpl-inspector` 的 `mpipackets/names.go`)里没有"出生"这一条;
  * 名字最像的 `0xF02F UnitRequestRespawn` / `0xF04B OnUnitDead` / `0xD0AD` / `0xD137` /
    `0xF12C`+`0xF12D` / `0xB00E`, 在服务器回放里**一条都不出现**。
出生事实只在 ECS(`ptype=6` 的 `0x24`/`0x25` 创建包)里。这个探针用来复现上面的统计。

⚠ **统计陷阱**: MPI 报文体的头两字节**不总是 `02 58`**。实测还有 `ff 0f` / `ff 07` /
`ff 8f` / `NN c0` 等前缀, 那不是报文头而是另一种序列化 —— 把这些体的 `[2:4]` 当 id 会
产出 `0x8081`…`0xF886` 一堆假签名。所以本探针**只把 `?? 58` 前缀体的 `[2:4]` 当 id**
(`--all` 可以看全部, 但结论别当真)。

用法:
    python tools\\wrpl\\probe_mpi_sigs.py "回放文件\\服务器\\Abandoned factory"
    python tools\\wrpl\\probe_mpi_sigs.py <回放> --all            # 连假签名一起列
    python tools\\wrpl\\probe_mpi_sigs.py <回放> --dump 0xD079 5   # 打印 5 条样例 hex
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from wt81111g import wrpl_reader as RD          # noqa: E402

PTYPE_NAME = {
    0: "EndMarker", 1: "StartMarker", 2: "FlightModel", 3: "聊天", 4: "MPI",
    5: "NextSegment", 6: "ECS", 7: "快照", 8: "HeaderInfo",
}
# 目录里的名字(只列我们实测会遇到的; 全表见 wrpl-inspector 的 mpipackets/names.go)
MID_NAME = {
    0xF02D: "ReflectionData(玩家槽位)",
    0xF02E: "?",
    0xF037: "UnitDetected",
    0xF039: "?",
    0xF055: "MissionFailOrSuccess",
    0xF056: "TextCriticalHitReport",
    0xF058: "TextKillReport",
    0xF05D: "FadeToDebriefing",
    0xF073: "GroundModelPositions(本地)",
    0xF074: "GroundModelPositions(服务器回放)",
    0xF078: "TextStreak",
    0xF07A: "MissionProgressData",
    0xF07C: "MissionProgressTimeData",
    0xF0AA: "RedundancyReflectionData",
    0xF0C3: "MessageStartGame",
    0xF0D7: "CamTrack",
    0xF0DF: "CustomWeatherParams",
    0xF0F7: "InteractiveObjectAuthorityApprovedState",
    0xF0F8: "InteractiveObjectAuthorityApprovedPartialState",
    0xF118: "UnitDamagePartKill",
    0xF11A: "ShellsDataServerReplay",
    0xF122: "TerraformPatchAlt",
    0xF123: "TerraformData",
    0xF142: "TargetDesignationMark",
    0xF157: "严重伤害(自研)",
    0xD039: "DeferredReplicatedObjectCreate",
    0xD044: "?",
    0xD079: "?(未记载: 单位出现通知, 带 uid)",
    0xD11B: "?",
    0xD136: "DeferredReflectionData",
    0xD137: "UnitDataSnapshot",
}
_STR = re.compile(rb"[\x20-\x7E]{4,}")


def ascii_strings(body: bytes, limit: int = 4) -> list[str]:
    out: list[str] = []
    for m in _STR.finditer(body):
        s = m.group().decode("ascii", "replace")
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    show_all, dump_mid, dump_n = False, -1, 0
    argv = sys.argv[2:]
    for i, a in enumerate(argv):
        if a == "--all":
            show_all = True
        elif a == "--dump" and i + 2 < len(argv):
            dump_mid = int(argv[i + 1], 16)
            dump_n = int(argv[i + 2])

    if os.path.isdir(path):
        files, infos = RD._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [RD.read_header_file(path)]

    ptype_cnt: Counter = Counter()
    prefix_cnt: Counter = Counter()
    mid_cnt: Counter = Counter()
    mid_bytes: Counter = Counter()
    mid_lens: dict[int, Counter] = defaultdict(Counter)
    strs: dict[int, Counter] = defaultdict(Counter)
    ecs_cnt: Counter = Counter()

    for idx, fp in enumerate(files, 1):
        print(f"  [{idx}/{len(files)}] {os.path.basename(fp)}", flush=True)
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = RD.decompress_stream(buf, infos[idx - 1])
        packets, _ = RD.parse_packets(payload)
        print(f"     包 {len(packets):,} 个", flush=True)
        for ptype, _ms, s, e in packets:
            ptype_cnt[ptype] += 1
            n = e - s
            if ptype == RD.PACKET_MPI and n >= 4:
                prefix_cnt[payload[s:s + 2].hex(" ")] += 1
                if not show_all and payload[s + 1] != 0x58:
                    continue
                mid = payload[s + 2] | (payload[s + 3] << 8)
                body = payload[s:e]
                mid_cnt[mid] += 1
                mid_bytes[mid] += n
                mid_lens[mid][n] += 1
                if len(strs[mid]) < 40:
                    for st in ascii_strings(body):
                        strs[mid][st] += 1
                if dump_mid >= 0 and mid == dump_mid and mid_cnt[mid] <= dump_n:
                    print(f"     0x{mid:04X} #{mid_cnt[mid]} len={n}: {body[:200].hex(' ')}")
            elif ptype == RD.PACKET_ECS and n >= 1:
                ecs_cnt[payload[s]] += 1
        del payload

    total = sum(ptype_cnt.values())
    print()
    print("=" * 104)
    print("1. 包类型分布")
    print("=" * 104)
    for pt, c in ptype_cnt.most_common():
        print(f"  ptype={pt:<3} {PTYPE_NAME.get(pt, '?'):<12} {c:>10,} 条 "
              f"{c / total * 100:6.2f}%")

    print()
    print("=" * 104)
    print("2. MPI 报文体前缀分布(只有 `?? 58` 才是真报文头)")
    print("=" * 104)
    for h, c in prefix_cnt.most_common():
        tag = "真报文头" if h.endswith("58") else "另一种序列化(别当 id)"
        print(f"  '{h}'  {c:>9,} 条  {tag}")

    tot_b = sum(mid_bytes.values())
    print()
    print("=" * 104)
    print(f"3. 报文 id 分布 —— 共 {sum(mid_cnt.values()):,} 条 / {tot_b:,} B"
          f"{'（含假签名）' if show_all else '（只统计真报文头）'}")
    print("=" * 104)
    print(f"  {'mid':>7} {'条数':>9} {'字节':>13} {'占比':>7}  名称 / 常见长度 / 明文样例")
    for mid, c in mid_cnt.most_common(40):
        lens = ",".join(f"{ln}x{k}" for ln, k in mid_lens[mid].most_common(2))
        ss = ", ".join(f'"{k}"' for k, _v in strs[mid].most_common(2))
        print(f"  0x{mid:04X} {c:>9,} {mid_bytes[mid]:>13,} "
              f"{mid_bytes[mid] / max(tot_b, 1) * 100:6.2f}%  "
              f"{MID_NAME.get(mid, '?'):<40} {lens:<16} {ss[:36]}")

    if ecs_cnt:
        print()
        print("=" * 104)
        print("4. ECS(ptype=6) 子类型")
        print("=" * 104)
        for mid, c in ecs_cnt.most_common():
            print(f"  0x{mid:02X} {c:>9,} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
