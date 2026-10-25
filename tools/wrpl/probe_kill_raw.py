"""把前几个击杀包的**原始字节**按字段打印出来, 看清 0xB 到底是什么。"""
from __future__ import annotations

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
            body = payload[s:e]
            got = we.read_fields_32(we.BitReader(body, 32), want=set(range(32)))
        except Exception:          # noqa: BLE001
            got = {}
        RAW.append({"t": t, "len": e - s, "got": got})
        return orig(payload, s, e, t)

    we.parse_kill_packet = patched
    try:
        data = load_replay(path)
    finally:
        we.parse_kill_packet = orig

    print(f"抓包 {len(RAW)} 个\n")
    for i, rec in enumerate(RAW[:6]):
        print(f"--- 包 {i + 1}  t={rec['t']:.1f}  体长 {rec['len']} ---")
        for fidx in sorted(rec["got"]):
            b = rec["got"][fidx]
            txt = we._lenstr(b)
            print(f"  {fidx:#4x} {len(b):3d}B  {b[:24].hex(' '):<48s}"
                  f" u32={int.from_bytes(b[:4].ljust(4, b'\x00'), 'little'):<12d}"
                  f" str={txt!r}")
        print()

    # 0xB 全量: 当整数 / 当字符串分别长什么样
    print("0xB 的取值统计:")
    import collections
    as_int = collections.Counter()
    as_str = collections.Counter()
    for rec in RAW:
        b = rec["got"].get(0xB)
        if b is None:
            continue
        as_int[int.from_bytes(b[:4].ljust(4, b"\x00"), "little")] += 1
        as_str[we._lenstr(b)] += 1
    print(f"  当 u32: {as_int.most_common(8)}")
    print(f"  当字符串: {as_str.most_common(8)}")


if __name__ == "__main__":
    main()
