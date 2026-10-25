"""探针: 解析 ptype=6 ECS 的**实体构造消息**(control 0x24 / 0x25)。

目的: 验证「不需要组件哈希表也能拿到实体身份」这条路走不走得通。

全部靠明文 / 自建表, 不读 ecshashes.json:
  * 模板名       —— 构造块开头的明文长度前缀串(tank / aircraft / rocket / ...)
  * entityId     —— packet.ReadEID 的 16/24/32 位三种编码
  * 组件值起点   —— 模板定义表长度 + compCount 字段之后
  * unit__className —— 块内逐偏移扫长度前缀串(tankModels/… / structures/… / 机型名)
  * uid          —— 块内 u16 低 11 位, 用「哪个偏移命中率最高」做经验标定

方法来源(只取格式事实, 代码为本项目独立实现):
  * ShioUmiusi/WTACRP  tools/wrpl_decode_ecs.py
  * maxsupermanhd/wrpl-inspector  wrpl/packet/parser/ecs2/{ecs2.go,compdefs.go}

用法:
    python tools/wrpl/probe_ecs_entity.py "回放文件\\服务器"
"""
from __future__ import annotations

import collections
import math
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402

CTRL_CREATE = (0x24, 0x25)
CTRL_NAMES = {
    0x20: "ENTITY_MSG", 0x21: "ENTITY_MSG_COMPRESSED",
    0x22: "ENTITY_REPLICATION", 0x23: "ENTITY_REPLICATION_COMPRESSED",
    0x24: "ENTITY_CREATION", 0x25: "ENTITY_CREATION_COMPRESSED",
    0x26: "ENTITY_DESTRUCTION",
}

# 模板名 -> 类别(模板名本身是明文, 这就是「不需要哈希表」的关键)
CLASS_OF = {
    "tank": "ground",
    "fortification": "structure",
    "aircraft": "aircraft",
    "aircraft+player_unit": "aircraft",
    "rocket": "projectile",
    "rocket+arh_shell": "projectile",
    "bomb": "projectile",
    "payload": "projectile",
    "jettisoned": "projectile",
    "damage_area": "explosion",
    "sync_player": "player_slot",
}
GROUND_NAME = re.compile(r"^(?:tankModels|structures)/[\w/.\-]+$")


def unit_templates(rows: list[dict]) -> list[str]:
    """带 uid 组件的模板 = 真正的「单位」(玩家坦克的各种变体 + 工事 + 飞机)。"""
    out = []
    for n in {r["name"] for r in rows}:
        if n.endswith("+player_unit") or n in ("tank", "aircraft", "fortification"):
            out.append(n)
    return sorted(out)


class BitReader:
    """MSB-first 位读器(与 wrpl_reader.BitReader 同语义, 这里独立一份便于试验)。"""
    __slots__ = ("d", "n", "pos")

    def __init__(self, d: bytes):
        self.d = d
        self.n = len(d) * 8
        self.pos = 0

    def bits(self, k: int) -> int:
        if k == 0:
            return 0
        p = self.pos
        if p + k > self.n:
            raise EOFError(f"bit eof {p}+{k}/{self.n}")
        b0 = p >> 3
        b1 = (p + k + 7) >> 3
        chunk = int.from_bytes(self.d[b0:b1], "big")
        total = (b1 - b0) * 8
        v = (chunk >> (total - (p & 7) - k)) & ((1 << k) - 1)
        self.pos = p + k
        return v

    def byte(self) -> int:
        return self.bits(8)

    def bytes_n(self, k: int) -> bytes:
        return self.bits(k * 8).to_bytes(k, "big")

    def u16(self) -> int:
        return int.from_bytes(self.bytes_n(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self.bytes_n(4), "little")

    def compressed(self) -> int:
        """ReadCompressed: 7 位一组, 低位在先, 高位置 1 表示继续。"""
        v = 0
        shift = 0
        while True:
            a = self.byte()
            v |= (a & 0x7F) << shift
            shift += 7
            if not (a & 0x80):
                return v

    def lenstr(self) -> str:
        n = self.byte()
        return self.bytes_n(n).decode("utf-8", "replace")

    def eid(self) -> int:
        """packet.ReadEID: 15/22/23 位三种编码(都是整字节)。"""
        f = self.u16()
        if f & 1:
            return (f >> 2) | (((f & 2) >> 1) << 22)
        if f & 2:
            return (f >> 2) | (self.byte() << 22)
        s = self.u16()
        ret = (s << 16) | f
        return ((ret & 0x00FFFFFF) >> 2) | ((ret >> 24) << 22)


# --------------------------------------------------------------- 模板表
class Templates:
    """模板定义表是**自建**的: 同一个 tid 只在首次出现时带定义。

    compdefs 只用来判断「这个 cid 是不是新出现的」(新出现才紧跟 8 字节哈希),
    不需要知道哈希对应什么名字 —— 这是绕开 ecshashes.json 的全部秘密。
    """

    def __init__(self):
        self.by_tid: dict[int, dict] = {}
        self.compdefs: set[int] = set()
        self.defined: list[dict] = []

    def parse(self, r: BitReader) -> dict:
        tid = r.compressed()
        t = self.by_tid.get(tid)
        if t is not None:
            return t
        name = r.lenstr()
        ncomp = r.u16()
        comps = []
        hashes = 0
        for _ in range(ncomp):
            cid = r.compressed()
            if cid not in self.compdefs:
                self.compdefs.add(cid)
                r.u32()                      # DataComponentHash
                r.u32()                      # ComponentHash
                hashes += 1
            comps.append(cid)
        t = {"id": tid, "name": name, "comps": comps, "hashes": hashes}
        self.by_tid[tid] = t
        self.defined.append(t)
        return t


def lz4_block_decompress(src: bytes) -> bytes:
    """LZ4 block 格式解压(无帧头)。

    `lz4` 这个 PyPI 包没有 cp314 的 wheel, 而 block 格式本身只有几条规则,
    自己实现比引依赖更省事:
      token 高 4 位 = literal 长度, 低 4 位 = match 长度 - 4;
      长度字段 == 15 时后面按 255 累加; offset 是 16 位小端; match 可重叠。
    """
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if i >= n:
                    return bytes(out)
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i:i + lit]
            i += lit
        if i + 2 > n:                       # 没有 offset 了 = 流结束
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0:
            break
        mlen = token & 0x0F
        if mlen == 15:
            while True:
                if i >= n:
                    return bytes(out)
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - off
        if start < 0:
            raise ValueError(f"lz4 offset 越界: {off} > {len(out)}")
        if off >= mlen:
            out += out[start:start + mlen]
        else:                               # 重叠复制(游程)
            for _ in range(mlen):
                out.append(out[start])
                start += 1
    return bytes(out)


def parse_create_packet(payload: bytes, s: int, e: int, tick: int,
                        T: Templates, out: list, stats: collections.Counter) -> None:
    ctrl = payload[s]
    if ctrl == 0x25:
        try:
            body = lz4_block_decompress(payload[s + 1:e])
        except (ValueError, IndexError):
            stats["lz4_fail"] += 1
            return
        stats["lz4_bytes"] += len(body)
        r = BitReader(body)
    elif ctrl == 0x24:
        r = BitReader(payload[s:e])
        r.byte()
    else:
        stats[f"ctrl_0x{ctrl:02x}"] += 1
        return
    n = r.byte()
    for _ in range(n + 1):
        try:
            eid = r.eid()
            bs = r.compressed()
            block = r.bytes_n(bs)
        except EOFError:
            stats["outer_eof"] += 1
            return
        try:
            br = BitReader(block)
            t = T.parse(br)
            tcc = len(t["comps"])
            comp_count = br.byte() if tcc < 256 else br.compressed()
            extra = br.compressed()
            vstart = br.pos // 8
        except EOFError:
            stats["block_eof"] += 1
            break
        out.append({
            "tick": tick, "eid": eid, "tid": t["id"], "name": t["name"],
            "tcc": tcc, "compCount": comp_count, "extra": extra,
            "bs": bs, "vstart": vstart, "block": block,
        })
    stats["pkts"] += 1


# --------------------------------------------------------------- 块内扫描
def collect_destroy(payload: bytes, s: int, e: int, tick: int,
                    out: list, stats: collections.Counter) -> None:
    """control 0x26 = 实体销毁: `0x26 | count(u8) | count+1 个 ReadEID`。"""
    br = BitReader(payload[s:e])
    br.byte()
    n = br.byte()
    for _ in range(n + 1):
        try:
            out.append((tick, br.eid()))
        except EOFError:
            stats["destroy_eof"] += 1
            return
    stats["destroy_pkts"] += 1


def scanned_strings(blk: bytes, lo: int = 3, hi: int = 64) -> list[tuple[int, str]]:
    """逐偏移找「长度前缀 + 全 ASCII」的串, 命中就跳过整串(与参考实现同策略)。"""
    out = []
    i, n = 0, len(blk)
    while i < n - 1:
        L = blk[i]
        if lo <= L <= hi and i + 1 + L <= n:
            raw = blk[i + 1:i + 1 + L]
            if all(32 <= b < 127 for b in raw):
                out.append((i, raw.decode("ascii")))
                i += 1 + L
                continue
        i += 1
    return out


def classify(name: str) -> str:
    if name in CLASS_OF:
        return CLASS_OF[name]
    if "aircraft" in name:
        return "aircraft"
    if name.startswith("tank"):
        return "ground"
    return "other"


def plaus_pos(x: float, y: float, z: float) -> bool:
    if not all(math.isfinite(v) for v in (x, y, z)):
        return False
    return (abs(x) <= 45000 and abs(z) <= 45000
            and -500 <= y <= 16000 and max(abs(x), abs(z)) > 500)


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    T = Templates()
    rows: list[dict] = []
    deaths: list[tuple[int, int]] = []          # (tick, eid)
    stats: collections.Counter = collections.Counter()
    ctrl_all: collections.Counter = collections.Counter()

    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        payload = W.decompress_stream(buf, info)
        packets, _ = W.parse_packets(payload)
        for ptype, ms, s, e in packets:
            if ptype != 6 or e <= s:
                continue
            ctrl_all[payload[s]] += 1
            if payload[s] in CTRL_CREATE:
                parse_create_packet(payload, s, e, ms, T, rows, stats)
            elif payload[s] == 0x26:
                collect_destroy(payload, s, e, ms, deaths, stats)
        del buf, payload, packets

    print(f"=== 分片 {len(files)} 个 ===")
    print("ptype=6 控制码:")
    for c, n in ctrl_all.most_common():
        print(f"  0x{c:02X} {CTRL_NAMES.get(c, '?'):34s} {n:8,}")
    print(f"\n解析: {dict(stats)}")
    print(f"模板定义 {len(T.by_tid)} 个, 组件定义 {len(T.compdefs)} 个, "
          f"构造记录 {len(rows):,} 条")

    # ---- 模板表
    print("\n=== 模板表(定义顺序) ===")
    cnt_t = collections.Counter(r["name"] for r in rows)
    for t in T.defined:
        print(f"  tid={t['id']:<3d} {t['name']:<24s} 组件 {len(t['comps']):<3d} "
              f"新哈希 {t['hashes']:<3d} 实例 {cnt_t.get(t['name'], 0):>6,}")

    # ---- 按类别
    print("\n=== 按模板类别 ===")
    by_class: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_class[classify(r["name"])].append(r)
    for k, v in sorted(by_class.items(), key=lambda kv: -len(kv[1])):
        eids = len({r["eid"] for r in v})
        print(f"  {k:<12s} 记录 {len(v):>6,}  互异 eid {eids:>5,}")

    # ---- 自检: 块是否被恰好用尽(需要组件值解析, 这里只报 vstart 分布)
    print("\n=== 组件值起点(自检: 同模板应集中) ===")
    for t in T.defined[:12]:
        sub = [r for r in rows if r["tid"] == t["id"]]
        if not sub:
            continue
        vs = collections.Counter(r["vstart"] for r in sub)
        cc = collections.Counter(r["compCount"] for r in sub)
        print(f"  {t['name']:<24s} vstart {dict(vs.most_common(4))}  "
              f"compCount {dict(cc.most_common(4))}")

    # ---- 真值: MPI 通道解出的地面 uid + 轨迹首点, 用来给 ECS 的偏移定标
    print("\n=== 加载真值(MPI 地面轨迹) …", flush=True)
    ref = W.load_replay(path, expect_server=None)
    truth = {u: s[0] for u, s in ref.units.items() if s}     # uid -> (t, x, y, z)
    known = set(truth)
    print(f"  地面 uid {len(known)} 个; 飞机 uid {len(ref.air_units)} 个", flush=True)
    del ref

    def mode_vstart(sub: list[dict]) -> int:
        return collections.Counter(r["vstart"] for r in sub).most_common(1)[0][0]

    def calibrate_uid(sub: list[dict], maxrel: int = 400) -> list[tuple]:
        """哪个相对偏移上的 u16 低 11 位最像 uid。

        两个条件缺一不可:
          * 落进已知 uid 集合(否则只是「值域恰好合理」);
          * **互异性够高** —— 常量字段(例如 +26 在飞机块里 50 个全是 48)
            也会 100% 命中已知集合, 那是假阳性。
        """
        out = []
        for rel in range(maxrel):
            vals = []
            for r in sub:
                blk, vs = r["block"], r["vstart"]
                o = vs + rel
                if o + 2 > len(blk):
                    vals = None
                    break
                vals.append((blk[o] | (blk[o + 1] << 8)) & 0x7FF)
            if not vals:
                break
            uniq = len(set(vals))
            if uniq < len(vals) * 0.5:          # 常量字段, 直接排除
                continue
            inter = sum(1 for v in vals if v in known)
            out.append((inter, uniq, rel, len(vals)))
        out.sort(key=lambda x: (-x[0], -x[1]))
        return out[:4]

    print("\n=== uid 偏移标定(交叉验证: 低 11 位是否落进已知 MPI uid) ===")
    uid_rel_of: dict[str, int] = {}
    for tname in unit_templates(rows):
        sub = [r for r in rows if r["name"] == tname]
        # 首次出现的模板会把定义表写在值前面, vstart 整体后移 -> 只用众数 vstart 的块
        mv = mode_vstart(sub)
        sub = [r for r in sub if r["vstart"] == mv]
        if len(sub) < 3:
            continue
        cand = calibrate_uid(sub)
        print(f"  {tname:<28s} 块 {len(sub):>3} 个 (vstart={mv})")
        for inter, uniq, rel, tot in cand:
            print(f"      +{rel:<4d} 命中已知 uid {inter:>3}/{tot} = "
                  f"{100.0 * inter / tot:5.1f}%  互异 {uniq}")
        if cand and cand[0][0] >= max(3, len(sub) * 0.3):
            uid_rel_of[tname] = cand[0][2]

    # ---- 位置偏移标定(静态工事最可靠, 坦克有移动但出生点就在轨迹首点附近)
    print("\n=== 位置偏移标定(3×f32 是否落在该 uid 的已知轨迹首点附近) ===")
    pos_rel_of: dict[str, int] = {}
    for tname, urel in uid_rel_of.items():
        sub = [r for r in rows if r["name"] == tname]
        mv = mode_vstart(sub)
        sub = [r for r in sub if r["vstart"] == mv]
        best: list[tuple] = []
        span = min((len(r["block"]) - r["vstart"] - 12) for r in sub)
        for rel in range(0, max(0, span) + 1):
            hits = tot = 0
            ds: list[float] = []
            for r in sub:
                blk, vs = r["block"], r["vstart"]
                o0 = vs + urel
                if o0 + 2 > len(blk):
                    continue
                uid = (blk[o0] | (blk[o0 + 1] << 8)) & 0x7FF
                p = truth.get(uid)
                if p is None:
                    continue
                x, y, z = struct.unpack_from("<3f", blk, vs + rel)
                if not all(math.isfinite(v) for v in (x, y, z)):
                    continue
                tot += 1
                d = math.hypot(x - p[1], z - p[3])
                ds.append(d)
                if d < 400:
                    hits += 1
            if tot:
                ds.sort()
                best.append((hits, tot, rel, ds[len(ds) // 2]))
        best.sort(key=lambda x: -x[0])
        print(f"  {tname:<28s} 可对比 {len(sub)} 块, 扫 0..{span}")
        for hits, tot, rel, med in best[:4]:
            print(f"      +{rel:<5d} {hits:>3}/{tot} = {100.0 * hits / tot:5.1f}% "
                  f"落在已知首点 400 m 内, 中位距离 {med:9.0f} m")
        if best and best[0][0] >= max(3, best[0][1] * 0.3):
            pos_rel_of[tname] = best[0][2]

    # ---- className 扫描
    print("\n=== className 扫描(明文长度前缀串) ===")
    for tname in unit_templates(rows):
        sub = [r for r in rows if r["name"] == tname]
        if not sub:
            continue
        is_ground = ("tank" in tname) or tname == "fortification"
        vals: collections.Counter = collections.Counter()
        hit_n = 0
        for r in sub:
            got = None
            for _off, s in scanned_strings(r["block"]):
                if GROUND_NAME.match(s):
                    got = s
                    break
                if not is_ground and re.match(r"^[a-z0-9][a-z0-9_.\-]{2,48}$", s) \
                        and s != "dummy_plane" and "player" not in s \
                        and "aircraft" not in s:
                    got = s
                    break
            if got:
                hit_n += 1
                vals[got] += 1
        print(f"  {tname:<28s} {hit_n}/{len(sub)} 命中, 互异 {len(vals)} 个")
        for v, k in vals.most_common(14):
            print(f"      {v:<48s} x{k}")

    # ---- 用标定好的偏移汇总「实体 -> uid / 型号 / 出生位置」
    print("\n=== 实体表(用标定偏移提取) ===")
    ents: list[dict] = []
    for tname, urel in uid_rel_of.items():
        sub = [r for r in rows if r["name"] == tname]
        mv = mode_vstart(sub)
        sub = [r for r in sub if r["vstart"] == mv]
        is_ground = ("tank" in tname) or tname == "fortification"
        prel = pos_rel_of.get(tname)
        for r in sub:
            blk, vs = r["block"], r["vstart"]
            o0 = vs + urel
            uid = (blk[o0] | (blk[o0 + 1] << 8)) & 0x7FF
            pos = None
            if prel is not None and vs + prel + 12 <= len(blk):
                x, y, z = struct.unpack_from("<3f", blk, vs + prel)
                if all(math.isfinite(v) for v in (x, y, z)):
                    pos = (x, y, z)
            cls = None
            for _off, s in scanned_strings(blk):
                if GROUND_NAME.match(s):
                    cls = s
                    break
                if not is_ground and re.match(r"^[a-z0-9][a-z0-9_.\-]{2,48}$", s) \
                        and s != "dummy_plane" and "player" not in s \
                        and "aircraft" not in s:
                    cls = s
                    break
            ents.append({"tmpl": tname, "uid": uid, "cls": cls, "pos": pos,
                         "tick": r["tick"]})
    n_uid = len({e["uid"] for e in ents})
    n_known = sum(1 for e in ents if e["uid"] in known)
    print(f"  实体 {len(ents)} 条, 互异 uid {n_uid} 个, "
          f"其中 {n_known} 条 uid 在已知 MPI uid 集合里")
    print(f"  有型号 {sum(1 for e in ents if e['cls'])} 条, "
          f"有位置 {sum(1 for e in ents if e['pos'])} 条")
    print("  --- 前 25 条 ---")
    for e in ents[:25]:
        p = e["pos"]
        ps = f"({p[0]:8.0f},{p[1]:6.0f},{p[2]:8.0f})" if p else " " * 26
        print(f"    uid={e['uid']:<5d} {e['tmpl']:<24s} {ps}  {e['cls'] or '-'}")
    # uid 覆盖情况: ECS 有、MPI 没有 / MPI 有、ECS 没有
    ecs_u = {e["uid"] for e in ents}
    print(f"\n  ECS uid 不在 MPI 集合: {len(ecs_u - known)} 个 "
          f"{sorted(ecs_u - known)[:20]}")
    print(f"  MPI uid 不在 ECS 集合: {len(known - ecs_u)} 个 "
          f"{sorted(known - ecs_u)[:20]}")

    # ---- 生命周期: 创建 eid 与销毁 eid 能否对上(这才是「消失时间」的正解)
    print("\n=== 生命周期(eid 交集) ===")
    born: dict[int, tuple[int, str]] = {}
    for r in rows:
        born.setdefault(r["eid"], (r["tick"], r["name"]))
    died: dict[int, int] = {}
    for tick, eid in deaths:
        died.setdefault(eid, tick)
    inter = set(born) & set(died)
    print(f"  创建 eid {len(born):,} 个, 销毁 eid {len(died):,} 个, "
          f"**交集 {len(inter):,} 个**")
    print(f"  销毁包 {stats.get('destroy_pkts', 0):,} 个, 解出 eid {len(deaths):,} 个")

    if inter and uid_rel_of:
        print("\n  --- 玩家坦克/飞机的 出现 -> 消失 ---")
        n_row = 0
        for tname, urel in uid_rel_of.items():
            if "tank" not in tname and "aircraft" not in tname:
                continue
            sub = [r for r in rows if r["name"] == tname]
            mv = mode_vstart(sub)
            for r in [x for x in sub if x["vstart"] == mv]:
                blk, vs = r["block"], r["vstart"]
                o = vs + urel
                if o + 2 > len(blk):
                    continue
                uid = (blk[o] | (blk[o + 1] << 8)) & 0x7FF
                d = died.get(r["eid"])
                if n_row < 20:
                    n_row += 1
                    ds = f"{d / 1000.0:8.1f}s" if d is not None else "     未销毁"
                    print(f"    uid={uid:<5d} {tname:<26s} "
                          f"出现 {r['tick'] / 1000.0:8.1f}s  消失 {ds}")

    # ---- transform 按位解析: 1 bit(isOrtho) + 36B(3x3) 或 62bit+scale, 然后 12B 平移
    print("\n=== transform 按位解析(平移不在字节边界上) ===")

    def read_transform(blk: bytes, vs: int) -> tuple[float, float, float]:
        br = BitReader(blk[vs:])
        if br.bits(1):                      # isOrthoUni: 62 位压缩正交 + 可选 scale
            br.bits(62)
            if br.bits(1):
                br.bytes_n(4)
        else:
            br.bytes_n(36)                  # 3x3 float32 矩阵
        return struct.unpack("<3f", br.bytes_n(12))

    for tname, urel in uid_rel_of.items():
        sub = [r for r in rows if r["name"] == tname]
        mv = mode_vstart(sub)
        sub = [r for r in sub if r["vstart"] == mv]
        ds: list[float] = []
        ok = bad = 0
        for r in sub:
            blk, vs = r["block"], r["vstart"]
            uid = (blk[vs + urel] | (blk[vs + urel + 1] << 8)) & 0x7FF
            p = truth.get(uid)
            if p is None:
                continue
            try:
                x, y, z = read_transform(blk, vs)
            except EOFError:
                bad += 1
                continue
            if not all(math.isfinite(v) for v in (x, y, z)):
                bad += 1
                continue
            ok += 1
            ds.append(math.hypot(x - p[1], z - p[3]))
        if not ds:
            print(f"  {tname:<28s} 无可对比样本")
            continue
        ds.sort()
        n = len(ds)
        print(f"  {tname:<28s} 解出 {ok} 条(坏 {bad}); 到已知首点距离 "
              f"中位 {ds[n//2]:8.0f} m, p90 {ds[int(n*0.9)]:8.0f} m, "
              f"<=400m {100.0*sum(1 for d in ds if d <= 400)/n:5.1f}%")

    # ---- 单块深挖: 打印一个工事的 transform 结果与已知坐标
    ft = [r for r in rows if r["name"] == "fortification"]
    if ft and "fortification" in uid_rel_of:
        mv = mode_vstart(ft)
        shown = 0
        urel = uid_rel_of["fortification"]
        for r in [x for x in ft if x["vstart"] == mv]:
            if shown >= 4:
                break
            blk, vs = r["block"], r["vstart"]
            uid = (blk[vs + urel] | (blk[vs + urel + 1] << 8)) & 0x7FF
            p = truth.get(uid)
            if p is None:
                continue
            shown += 1
            try:
                x, y, z = read_transform(blk, vs)
            except EOFError:
                print(f"    uid={uid:<4d} transform 读溢出")
                continue
            print(f"    uid={uid:<4d} block={len(blk):<5d} "
                  f"ECS ({x:9.1f},{y:7.1f},{z:9.1f})  "
                  f"MPI ({p[1]:9.1f},{p[2]:7.1f},{p[3]:9.1f})  "
                  f"平面差 {math.hypot(x-p[1], z-p[3]):7.0f} m")

    # ---- 明文串总览(找还有没有别的可用明文)
    print("\n=== 明文串总览(前 40 种) ===")
    allstr: collections.Counter = collections.Counter()
    for r in rows[:4000]:
        for _off, s in scanned_strings(r["block"], 3, 40):
            allstr[s] += 1
    for s, k in allstr.most_common(40):
        print(f"  {k:>5} {s[:70]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else r"回放文件\服务器")
