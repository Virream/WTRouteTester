"""从 `.wrpl` 里解出「玩家」与「击杀」—— 不需要 ECS 哈希表。

背景（2026-10-25）：位置包只给 `uid`（0..2047 的**载具槽位**），没有任何身份字段。
玩家身份其实在另外两种包里，而且**都不依赖 ECS 组件表**：

* **Slot 包**（ptype=4，签名 `0xF02D`）—— 玩家槽位 0..255 的结构化字段：
  `player_id`(u64) + 名字(65 B) / 军团 / 称号 / 队伍(u8) / realNick
* **Kill 包**（ptype=4，签名 `0xF058`）—— 击杀事件：
  `KillerPid` / `PlayerVehicle` / `VictimUid` / `KillerUid` / `PlayerWeapon` /
  `VictimPid` / `DestroyedWeapon`

`KillerPid`/`VictimPid` 是**玩家 PID**（对应 Slot 的 `player_id`），
`KillerUid`/`VictimUid` 是**载具 uid** —— 于是凡参与过击杀/被击毁的载具，
uid ↔ 玩家 的映射都能白拿到，不必先做 ECS。

字段表格式（`IdFieldSerializer255` / `32`）：一个定长头 + 偏移指向的尺寸表 +
定宽索引表，然后才是字段本体。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .wrpl_reader import BitReader

SIG_SLOT = 0xF02D
SIG_KILL = 0xF058
SIG_SEVERE_DAMAGE = 0xF057

INVALID_OID = 0xFFFF
EXT_MASK = 0x7FF

# Slot 消息里的字段号
F_PLAYER_UID = 2          # u64 player_id + 65 字节名字
F_INVITED_NICK = 3
F_CLAN_TAG = 5
F_TITLE = 6
F_TEAM = 9
F_REAL_NICK = 41

# Kill 包里的字段号
K_KILLER_PID = 1
K_VEHICLE = 2
K_VICTIM_UID = 3
K_KILLER_UID = 4
K_WEAPON = 0xA
K_VICTIM_PID = 0xB
K_DESTROYED_WEAPON = 0xC


@dataclass
class PlayerSlot:
    """一个玩家槽位当前已知的信息（多次 Slot 包会逐步补齐）。"""
    slot: int = 0
    player_id: int = 0
    name: str = ""
    clan_tag: str = ""
    title: str = ""
    team: int = 0
    real_nick: str = ""

    @property
    def display(self) -> str:
        who = self.real_nick or self.name or f"slot{self.slot}"
        return f"[{self.clan_tag}] {who}" if self.clan_tag else who


@dataclass
class KillEvent:
    """一次击杀。uid 是载具槽位，pid 是玩家。"""
    t: float = 0.0
    killer_pid: int = 0
    killer_uid: int = 0
    victim_pid: int = 0
    victim_uid: int = 0
    vehicle: str = ""
    weapon: str = ""
    destroyed: str = ""


# ---------------------------------------------------------- 字段表(变长)
def read_size(br: BitReader) -> int:
    """字段尺寸（单位：**位**）。前 3 位是类型码，0 表示后面跟变长值。"""
    hdr = br.bits(3)
    if hdr == 1:
        return 1
    if hdr == 2:
        return 8
    if hdr == 3:
        return 16
    if hdr == 4:
        return 32
    if hdr == 5:
        return 64
    if hdr == 6:
        return 96
    if hdr == 7:
        return 128
    return int(br.compressed())


def _align8(n: int) -> int:
    return n + 8 - (((n - 1) & 7) + 1) if n else 0


def read_fields_255(br: BitReader, want: dict[int, str] | None = None) -> dict[int, bytes]:
    """`IdFieldSerializer255`：字段号是定宽（`bitsPerID`）的索引，最多 254 个字段。

    `want` 给定时只保留这些字段（其余跳过），省内存也省时间。
    """
    out: dict[int, bytes] = {}
    start = br.pos
    offset = br.u16()
    count = br.u16()
    nfields = count & 0xFFF
    bits_per_id = count >> 12
    if nfields >= 255:
        raise ValueError(f"字段数异常: {nfields}")
    start_body = br.pos

    # ① 尺寸表
    br.pos = start + (offset << 3)
    sizes = [read_size(br) for _ in range(nfields)]

    # ② 索引表（在 startBody 之前，按 bitsPerID 定宽）
    bits_for_idx = _align8(bits_per_id * nfields)
    br.pos = start_body - 32 + ((offset << 3) - bits_for_idx)
    indexes = [br.bits(bits_per_id) for _ in range(nfields)]

    # ③ 字段本体
    br.pos = start_body
    for idx, size in zip(indexes, sizes):
        body = br.bytes_n((size + 7) >> 3) if (size & 7) == 0 else _read_bits_bytes(br, size)
        if want is None or idx in want:
            out[idx] = body
    return out


def _read_bits_bytes(br: BitReader, bits: int) -> bytes:
    """读 n 位，返回左对齐的字节串（与上游 danet.ReadBits 语义一致）。"""
    nbytes = (bits + 7) >> 3
    whole, rest = bits >> 3, bits & 7
    buf = bytearray()
    if whole:
        buf += br.bytes_n(whole)
    if rest:
        buf.append(br.bits(rest) << (8 - rest))
    if len(buf) < nbytes:
        buf += b"\x00" * (nbytes - len(buf))
    return bytes(buf)


def read_fields_32(br: BitReader, want: dict[int, str] | None = None) -> dict[int, bytes]:
    """`IdFieldSerializer32`：用 32 位掩码标记存在哪些字段（字段号 = 位号）。"""
    out: dict[int, bytes] = {}
    start = br.pos
    offset = br.u16()
    mask = int(br.compressed())
    start_body = br.pos
    br.pos = start + (offset << 3)
    idxs = [i for i in range(32) if (mask >> i) & 1]
    sizes = [read_size(br) for _ in idxs]
    br.pos = start_body
    for idx, size in zip(idxs, sizes):
        body = _read_bits_bytes(br, size)
        if want is None or idx in want:
            out[idx] = body
    return out


def _lenstr(buf: bytes) -> str:
    if not buf:
        return ""
    n = buf[0]
    return buf[1:1 + n].decode("utf-8", "replace").strip("\x00")


# ---------------------------------------------------------------- Slot 包
def parse_slot_packet(payload: bytes, s: int, e: int, t: float = 0.0
                      ) -> list[PlayerSlot]:
    """解析一个 Slot 包，返回**本包里出现过的**玩家槽位（可能只带部分字段）。"""
    body = payload[s:e]
    if len(body) < 6:
        return []
    br = BitReader(body, 32)                 # 跳过 4 字节签名
    compressed = br.byte()
    if compressed:
        br.compressed()                      # 压缩后长度
        br.compressed()                      # 解压后长度
        raw = bytes(body[(br.pos >> 3):])
        try:
            import zstandard                                  # noqa: PLC0415
            inner = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
        except Exception:                    # noqa: BLE001 - 解不开就放弃这个包
            return []
        br = BitReader(inner, 0)

    nmsg = br.u16()
    if nmsg > 4096:
        raise ValueError(f"slot 消息数异常: {nmsg}")
    out: list[PlayerSlot] = []
    for _ in range(nmsg):
        if br.pos + 16 > br.nbits:
            break
        mlen = br.u16()
        if mlen == 0:
            continue
        before = br.pos
        oid = br.u16()
        ext = 0
        if (oid & EXT_MASK) == EXT_MASK and oid != INVALID_OID:
            ext = int(br.compressed())
        consumed = (br.pos - before) >> 3
        rest = mlen - consumed
        if rest < 0:
            break
        msg = br.bytes_n(rest)
        if (oid >> 0xB) != 0xE:              # 不是玩家槽位消息（0x7000..0x77FF）
            continue
        slot = oid & 0x7FF
        try:
            got = read_fields_255(BitReader(msg), want={
                F_PLAYER_UID, F_CLAN_TAG, F_TITLE, F_TEAM, F_REAL_NICK})
        except (ValueError, EOFError, IndexError):
            continue
        plr = PlayerSlot(slot=slot)
        uid_body = got.get(F_PLAYER_UID)
        if uid_body and len(uid_body) >= 8:
            plr.player_id = struct.unpack_from("<Q", uid_body, 0)[0]
            plr.name = uid_body[8:8 + 65].split(b"\x00")[0].decode("utf-8", "replace")
        if F_CLAN_TAG in got:
            plr.clan_tag = _lenstr(got[F_CLAN_TAG])
        if F_TITLE in got:
            plr.title = _lenstr(got[F_TITLE])
        if F_REAL_NICK in got:
            plr.real_nick = _lenstr(got[F_REAL_NICK])
        if F_TEAM in got and got[F_TEAM]:
            plr.team = got[F_TEAM][0]
        out.append(plr)
    return out


# ---------------------------------------------------------------- Kill 包
def parse_kill_packet(payload: bytes, s: int, e: int, t: float = 0.0
                      ) -> KillEvent | None:
    """解析一个击杀包（签名 `0xF058`）。"""
    body = payload[s:e]
    if len(body) < 6:
        return None
    got = read_fields_32(BitReader(body, 32), want=set(range(32)))
    ev = KillEvent(t=t)
    if K_KILLER_PID in got and len(got[K_KILLER_PID]) >= 4:
        ev.killer_pid = struct.unpack_from("<I", got[K_KILLER_PID], 0)[0]
    if K_VICTIM_PID in got and len(got[K_VICTIM_PID]) >= 4:
        ev.victim_pid = struct.unpack_from("<I", got[K_VICTIM_PID], 0)[0]
    if K_VICTIM_UID in got and len(got[K_VICTIM_UID]) >= 2:
        ev.victim_uid = struct.unpack_from("<H", got[K_VICTIM_UID], 0)[0] & EXT_MASK
    if K_KILLER_UID in got and len(got[K_KILLER_UID]) >= 2:
        ev.killer_uid = struct.unpack_from("<H", got[K_KILLER_UID], 0)[0] & EXT_MASK
    ev.vehicle = _lenstr(got.get(K_VEHICLE, b""))
    ev.weapon = _lenstr(got.get(K_WEAPON, b""))
    ev.destroyed = _lenstr(got.get(K_DESTROYED_WEAPON, b""))
    if ev.killer_pid == 0 and ev.victim_pid == 0 and not ev.vehicle:
        return None
    return ev
