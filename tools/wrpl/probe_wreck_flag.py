"""找「残骸」标记: 把地面位置包里**每个单位的标志位**全部记下来, 看有没有哪一位
在"被击毁那一刻"翻转并一直保持。

`ground_parse_packet` 里读了但没用的位:
    b1 / has4(+4bit) / v2 / v3(同为 1 就整条跳过) / b2 / has_dir / turrets
    / hasBody / full / point4 / 炮塔 / 传感器
如果游戏把残骸标记成特殊单位, 很可能就是其中某一位。
"""
from __future__ import annotations

import collections
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g import wrpl_reader as wr                          # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
REC: dict[int, list] = collections.defaultdict(list)   # uid -> [(pos, flags…)]

NAMES = ("b1", "4bit", "v2", "v3", "b2", "dir", "turrets", "full", "pnd", "npn",
         "size", "point4", "turretPos", "sensor")


def make_patched():
    BitReader = wr.BitReader
    bits_le = wr.bits_le
    DeltaHistory = wr.DeltaHistory
    parse_pos = wr.parse_vehicle_position
    skip_turrets = wr._skip_turrets
    GROUND_UID_MAX = wr.GROUND_UID_MAX
    MAX_PACKET_BYTES = wr.MAX_PACKET_BYTES
    MAX_DELTA_BYTES = wr.MAX_DELTA_BYTES

    def patched(payload, hist, start=0, end=None):
        out = []
        view = payload[start:end]
        r = BitReader(view, 32)
        body_bits = r.compressed()
        r.align()
        if body_bits <= 0 or body_bits > MAX_PACKET_BYTES * 8:
            raise ValueError("体比特长度异常")
        body = r.bytes_n((body_bits + 7) // 8)
        g = BitReader(body)
        lower, upper = g.u16(), g.u16()
        g.u32()
        g.u16()
        if not (lower < upper <= GROUND_UID_MAX):
            raise ValueError("uid 范围异常")
        for uid in range(lower, upper):
            if not g.bits(1):
                continue
            b1 = g.bits(1)
            has4 = g.bits(1)
            v4 = g.bits(4) if has4 else -1
            v2, v3 = g.bits(1), g.bits(1)
            if v2 and v3:
                REC[uid].append((None, (b1, v4, v2, v3, -1, -1, -1, -1, -1, -1,
                                        -1, -1, -1, -1)))
                continue
            b2 = g.bits(1)
            has_dir = g.bits(1)
            turrets = g.byte()
            has_body = g.bits(1)
            if not has_body:
                raise ValueError("未压缩体")
            full = g.bits(1)
            pnd = bits_le(g, 5)
            npn = bits_le(g, 13)
            size = g.compressed()
            g.align()
            if size <= 0 or size > MAX_DELTA_BYTES:
                raise ValueError("增量体长度异常")
            packed = g.bytes_n(size)
            h = hist.get(uid)
            if h is None:
                h = hist[uid] = DeltaHistory()
            pos = parse_pos(h.apply(bool(full), pnd, npn, packed), bool(has_dir))
            p4 = g.bits(1)
            if p4:
                g.skip(4 * 32)
            t_has = g.bits(1)
            if t_has:
                skip_turrets(g, turrets)
            s_has = g.bits(1)
            if s_has:
                if g.bits(1):
                    n = g.u32()
                    if n > 1 << 20:
                        raise ValueError("seeker 长度异常")
                    g.skip(n)
            g.align()
            if pos is not None:
                out.append((uid, pos[0], pos[1], pos[2]))
                REC[uid].append((pos, (b1, v4, v2, v3, b2, has_dir, turrets, full,
                                       pnd, npn, size, p4, t_has, s_has)))
        return out
    return patched


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    target = int(sys.argv[2]) if len(sys.argv) > 2 else 35
    orig = wr.ground_parse_packet
    wr.ground_parse_packet = make_patched()
    try:
        data = wr.load_replay(path)
    finally:
        wr.ground_parse_packet = orig

    kills = sorted(ev.t for ev in data.kills if ev.victim_uid == target)
    print(f"uid {target}: 击杀时刻 {kills}")
    rows = REC.get(target) or []
    pos2t = {}
    for t, x, y, z in data.units.get(target, ()):
        pos2t[(round(x, 3), round(y, 3), round(z, 3))] = t
    print(f"记录 {len(rows)} 条; 位置流 {len(pos2t)} 个不同坐标")
    if not kills:
        print("这车没死, 换个 uid")
        return
    kill_t = kills[-1]

    series = []
    for pos, flags in rows:
        if pos is None:
            continue
        t = pos2t.get((round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)))
        series.append((t, flags))

    print(f"\n死亡时刻 {kill_t:.1f}s 前后各看 8 条:")
    before = [s for s in series if s[0] is not None and s[0] < kill_t][-8:]
    after = [s for s in series if s[0] is not None and s[0] >= kill_t][:8]
    for label, chunk in (("死前", before), ("死后", after)):
        print(f"  --- {label} ---")
        for t, flags in chunk:
            short = " ".join(f"{n}={v}" for n, v in zip(NAMES, flags)
                             if n not in ("npn", "size", "pnd"))
            print(f"   t={t:7.1f}  {short}")

    def collect(zone):
        out = collections.defaultdict(set)
        for t, flags in series:
            if t is None:
                continue
            if (zone == "before" and t < kill_t) or (zone == "after" and t >= kill_t):
                for n, v in zip(NAMES, flags):
                    out[n].add(v)
        return out
    b, a = collect("before"), collect("after")
    print("\n标志位: 死前 / 死后 取值个数, 以及『只在死后出现』的值")
    for n in NAMES:
        bv, av = b.get(n, set()), a.get(n, set())
        new = sorted(av - bv)[:6]
        print(f"  {n:10s} 死前 {len(bv):5d} 种 / 死后 {len(av):5d} 种"
              + (f"   ⭐ 仅死后出现: {new}" if new else ""))


if __name__ == "__main__":
    main()
