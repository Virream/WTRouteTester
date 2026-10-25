"""`ptype=2`（FlightModel）—— 飞机 / 直升机的轨迹。

与地面通道（`0xF073`/`0xF074`）完全独立：**没有 4 字节签名**，包体本身就是一串
entry，每条 entry 一个 uid。实测频率 4 Hz（250 ms）。

一条 entry 的字段顺序：

```
HasUID:1            → 有则 uid = compressed() & 0x7FF，没有则 uid += 1
if uid == 0x7FF: 包结束
noData:1            → 本帧没有数据（沿用上一次）
Unk0:1 Unk1:1       → 两个都是 1 表示“这条 entry 到此为止”
Unk2:1 Unk3:u32 Unk4:1
Unk5                → 1 位存在标志 + 3 位 + 4 位长度 + N 位
Unk10 = zigzag      → 后面 Unk11 的条数
Unk11[i]            → zigzag, zigzag(非负则为内层条数，每层再 zigzag)
PosX/PosY/PosZ: f32  ← 位置（我们只要这三个）
EulerBytes:u32  Unk12:u32  对齐
Unk13: 7 B
engines / sensors / countermeasures / targets / 尾部两组可选字段
```

⚠ **必须把尾部字段全部走完**，否则一条 entry 少读一位就会把包里后面几十条 entry
全带偏（实测：只解到位置时，每包只能拿到 1 架飞机，而包里其实有 40+ 个 uid 条目）。

字段名沿用上游的 `Unk` 命名 —— 我们只需要位置，其余只求“读对长度”。
格式结论参考 wrpl-inspector(AGPL-3.0) 的公开实现，代码自行用 Python 实现。
"""
from __future__ import annotations

from .wrpl_reader import BitReader

PACKET_FLIGHT_MODEL = 2
FM_UID_END = 0x7FF
MAX_ENTRIES = 512


def _zigzag(br: BitReader) -> int:
    u = int(br.compressed()) & 0xFFFFFFFF
    return (u >> 1) ^ -(u & 1)


def _skip_engines(br: BitReader) -> None:
    n = br.byte()
    if n > 0xF:
        raise ValueError(f"引擎数异常: {n}")
    for _ in range(n):
        has_power = br.bits(1)
        br.byte()                        # Unk0
        if has_power:
            br.u16()                     # EnginePowerPacked
        if br.bits(1):                   # Unk1
            br.byte()                    # Unk2
        br.byte()                        # Unk3
        br.byte()                        # Unk4


def _read_sensor(br: BitReader) -> None:
    first = br.bits(1)
    kind = br.byte() >> 4
    if kind == 1:
        if not br.bits(1):               # Unk0 = 0 → 这条 sensor 到此为止
            return
        unk1 = br.u16()
        br.u32()                         # Unk2 float32
        br.u16()
        br.u16()
        br.u16()
        if unk1 >= 0x8000:               # int16(unk1) < 0
            br.byte()                    # Unk6
        if br.bits(1):                   # Unk7
            br.byte()                    # Unk8
    elif kind == 2:
        if not first:
            return
        br.bits(1)                       # Unk0
        br.u16()                         # Unk1
        br.skip(0x60)                    # Unk2 (12 B)
        br.skip(0x60)                    # Unk3 (12 B)
        br.u32()                         # Unk4 float32
    elif kind == 3:
        raise ValueError("sensor type 3（未支持）")
    elif kind == 4:
        if br.bits(1):                   # Unk0
            br.skip(0x60)
    # 公共尾部
    if br.bits(1):                       # Unk0
        n = br.bits(6)
        for _ in range(n):
            br.u32()
        br.bits(6)


def _skip_sensors(br: BitReader) -> int:
    n = br.byte()
    if n > 4:
        raise ValueError(f"传感器数异常: {n}")
    for _ in range(n):
        _read_sensor(br)
    return n


def _skip_counter_measures(br: BitReader) -> int:
    n = br.byte()
    for _ in range(n):
        br.byte()
        br.byte()
    return n


def _read_target(br: BitReader) -> None:
    br.byte()                            # Unk0
    br.byte()                            # Unk1
    if br.bits(1):                       # Unk2 → Unk3[3]f32
        br.skip(3 * 32)
    if br.bits(1):                       # Unk4 → Unk5[3]f32 + Unk6[3]u16
        br.skip(3 * 32)
        br.skip(3 * 16)
    else:                                # Unk7[3]f32 + Unk8[3]f32
        br.skip(3 * 32)
        br.skip(3 * 32)
    br.u32()                             # Unk9 float32
    br.bits(1)                           # Unk10
    br.bits(1)                           # Unk11
    if br.bits(1):                       # Unk12
        br.byte()                        # Unk13
    br.bits(1)                           # Unk14
    unk15 = br.bits(1)
    br.bits(1)                           # Unk16
    if unk15:
        br.byte()                        # Unk17
    if br.bits(1):                       # Unk18
        br.u32()                         # Unk19


def _skip_targets(br: BitReader) -> None:
    n = br.bits(4)
    if n > 8:
        raise ValueError(f"目标数异常: {n}")
    for _ in range(n):
        _read_target(br)


def _skip_entry_tail(br: BitReader) -> None:
    """PosZ 之后到 entry 结束的全部字段。"""
    br.u32()                             # EulerBytes
    br.u32()                             # Unk12
    br.align()
    br.skip(7 * 8)                       # Unk13
    _skip_engines(br)
    if _skip_sensors(br):                # 有传感器数据时多一个字节
        br.byte()                        # SensorsUnk0
    if _skip_counter_measures(br):       # 有干扰弹数据时多一个字节
        br.byte()                        # CounterMeasuresU0
    _skip_targets(br)
    if br.bits(1):                       # Unk14
        br.u16()
        br.u16()
    if br.bits(1):                       # Unk17
        n = br.u32()
        if n > 1 << 20:
            raise ValueError(f"尾部跳过长度异常: {n}")
        br.skip(n)


def _plausible(x: float, y: float, z: float) -> bool:
    """位置是否像真的（地图约 ±40 km，高度 -200~30000 m）。

    尾部字段一旦读错一位，后面的位置就是垃圾 —— 实测会出现 **-1137 m 的高度**。
    这种 entry 之后不会再“自愈”，所以调用方遇到直接放弃本包剩余部分。
    """
    return (-200.0 < y < 30000.0) and abs(x) < 64000.0 and abs(z) < 64000.0


def parse_fm_packet(payload: bytes, s: int, e: int
                    ) -> list[tuple[int, tuple[float, float, float] | None]]:
    """返回 `[(uid, (x, y, z) | None)]`；`None` = 本帧没有该单位的数据。"""
    br = BitReader(payload[s:e], 0)
    out: list[tuple[int, tuple[float, float, float] | None]] = []
    uid = 0
    for _ in range(MAX_ENTRIES):
        if br.pos + 1 > br.nbits:
            break
        if br.bits(1):
            uid = int(br.compressed()) & 0x7FF
        else:
            uid += 1
        if uid == FM_UID_END:
            break
        if br.bits(1):                       # noData
            out.append((uid, None))
            continue
        unk0 = br.bits(1)
        unk1 = br.bits(1)
        if unk0 and unk1:
            continue
        br.bits(1)                           # Unk2
        br.u32()                             # Unk3
        br.bits(1)                           # Unk4
        if br.bits(1):                       # Unk5 存在？
            br.bits(3)
            for _ in range(br.bits(4)):
                br.bits(1)
        n = _zigzag(br)                      # Unk10
        if n < 0 or n > 4096:
            raise ValueError(f"Unk10 条数异常: {n}")
        for _ in range(n):
            _zigzag(br)                      # Temp
            k = _zigzag(br)                  # Temp1
            if k > 0:
                if k > 4096:
                    raise ValueError(f"Unk11 内层条数异常: {k}")
                for _ in range(k):
                    _zigzag(br)
        x, y, z = br.f32(), br.f32(), br.f32()
        if not _plausible(x, y, z):
            break                            # 错位了 → 后面全不可信，丢掉本包剩余
        out.append((uid, (x, y, z)))
        _skip_entry_tail(br)
    return out
