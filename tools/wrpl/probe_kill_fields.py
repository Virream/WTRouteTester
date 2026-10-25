"""查击杀包里哪个字段才是「受害者玩家槽位」。

现象: `victim_pid`(K_VICTIM_PID = 0xB) 在 88 条击杀里有 **53 条都是 14** ——
不可能是真的槽位(那意味着一局里几乎所有人都在杀同一个人)。
这个探针把每个包里 0~0xC 的原始字段都摊开, 逐字段统计"有多少个不同值",
真正的槽位应该是 0~31、并且有很多不同值。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g import wrpl_events as we                          # noqa: E402
from wt81111g.wrpl_reader import load_replay                    # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
RAW: list[dict] = []


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

    orig = we.parse_kill_packet

    def patched(payload, s, e, t=0.0):
        try:
            got = we.read_fields_32(we.BitReader(payload[s:e], 32),
                                    want=set(range(32)))
        except Exception:          # noqa: BLE001
            got = {}
        RAW.append(got)
        return orig(payload, s, e, t)

    we.parse_kill_packet = patched
    try:
        data = load_replay(path)
    finally:
        we.parse_kill_packet = orig

    print(f"抓包 {len(RAW)} 个, 解析出击杀事件 {len(data.kills)} 条\n")

    # 逐字段: 有值的个数 / 不同值个数 / 取值
    print("字段  有值  不同值  前 8 个取值")
    for fidx in range(1, 13):
        vals = []
        for got in RAW:
            b = got.get(fidx)
            if b is None:
                continue
            if len(b) == 4:
                v = int.from_bytes(b[:4], "little")
            elif len(b) == 2:
                v = int.from_bytes(b[:2], "little")
            else:
                v = int.from_bytes(b[:1], "little") if b else -1
            vals.append(v)
        if not vals:
            continue
        cnt = collections.Counter(vals)
        print(f"  {fidx:#4x} {len(vals):5d} {len(cnt):6d}   {cnt.most_common(8)}")

    name = {p.slot: p.display for p in data.players.values()}
    print("\n前 12 条击杀: killer_pid 与各候选字段的取值")
    for ev in data.kills[:12]:
        print(f"  t={ev.t:7.1f} killer pid{ev.killer_pid:<3d}"
              f" {(name.get(ev.killer_pid) or '?')[:20]:22s}"
              f" victim_pid{ev.victim_pid:<3d} {(name.get(ev.victim_pid) or '?')[:20]:22s}"
              f" uid {ev.killer_uid}->{ev.victim_uid} [{ev.vehicle}]")


if __name__ == "__main__":
    main()
