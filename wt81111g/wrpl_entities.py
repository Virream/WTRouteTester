"""从 `ptype=6` ECS（Danet ECS）解析「实体档案」。

这条线只解决一件事: **每个地面载具是谁的、什么型号、什么时候出现/消失**。
不需要组件哈希表(ecshashes.json) —— 全部靠明文与自建模板表:

  * 模板名 —— 构造块开头的明文长度前缀串(`tank+player_unit` / `fortification` / …);
              模板名本身决定实体类别, 这是绕开哈希表的关键。
  * `unit__className` —— 块内逐偏移扫长度前缀串(`tankModels/ussr_t_80u` / `su_25t` / …)
  * 出生组名 `tN_playerMM_K` —— 同样是明文。**这是玩家身份的来源**:
              `t1_player(NN)_0 -> slot = NN-1`, `t2_player(NN)_0 -> slot = NN-1+K`,
              K = 每组人数(16v16 时 K=16; 8v8 时 K=8)。
  * uid —— 块内某个相对偏移上的 u16 低 11 位。**偏移不写死**, 用「与 MPI 通道
              已知 uid 集合的交集 + 互异性」现场标定(见 `_calibrate_uid`)。
  * 消失时刻 —— `0x26` 销毁包里的 ReadEID, 与创建包的 eid 100% 对得上。

**为什么出生组名能当玩家身份**: 实测(2026-10-25, avg_finland 服务器回放) 32 个
出生组名正好对应 32 个玩家槽位, 且「击杀者 uid -> 击杀者 pid」这条确定事实与公式
**32/32 完全一致**。注意 `t1`/`t2` 前缀**不是阵营**(两队是交叉的), 它只是
「先进大厅的那 16 人 / 后进的 16 人」这样的分组。

格式来源(只取事实, 代码为本项目独立实现):
  * ShioUmiusi/WTACRP  tools/wrpl_decode_ecs.py
  * maxsupermanhd/wrpl-inspector  wrpl/packet/parser/ecs2/{ecs2.go,compdefs.go}
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .wrpl_reader import BitReader

CTRL_CREATE = (0x24, 0x25)          # 实体创建 / 实体创建(压缩)
CTRL_DESTROY = 0x26                 # 实体销毁
EXT_MASK_UID = 0x7FF                # uid 低 11 位, 全 1 = 无
UID_BITS = 0x7FF

SPAWN_RE = re.compile(r"^t([12])_player(\d{2})_(\d+)$")
GROUND_CLS_RE = re.compile(r"^tankModels/[\w/.\-]+$")
AIR_CLS_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,48}$")

# 模板名 -> 是否「玩家载具」。
# tank+player_unit / tank+unit_aps+player_unit / tank+cloth_wind+player_unit 都是
# 坦克的变体（后两个是 APS / 布篷这种附加模块）。**不能只用 `startswith("tank")`**:
# `tank_smokescreen_ecs`（坦克烟雾弹改装件，83 个）也以 tank 开头，但它没有 uid
# 和 className，混进来会把 uid 偏移标定带歪。
UNIT_TEMPLATE_EXACT = ("tank", "aircraft", "fortification")
UNIT_TEMPLATE_SUFFIX = "+player_unit"


def is_unit_template(name: str) -> bool:
    return name in UNIT_TEMPLATE_EXACT or name.endswith(UNIT_TEMPLATE_SUFFIX)


def _lenstr(br: BitReader) -> str:
    n = br.byte()
    return br.bytes_n(n).decode("utf-8", "replace")


def _read_eid(br: BitReader) -> int:
    """packet.ReadEID：15/22/23 位三种编码(都是整字节)。"""
    f = br.u16()
    if f & 1:
        return (f >> 2) | (((f & 2) >> 1) << 22)
    if f & 2:
        return (f >> 2) | (br.byte() << 22)
    s = br.u16()
    ret = (s << 16) | f
    return ((ret & 0x00FFFFFF) >> 2) | ((ret >> 24) << 22)


def lz4_block_decompress(src: bytes) -> bytes:
    """LZ4 block 格式解压(无帧头)。

    `lz4` 这个 PyPI 包没有 cp314 的 wheel, 而 block 格式只有几条规则,
    自己实现比引依赖省事:
      token 高 4 位 = literal 长度, 低 4 位 = match 长度 - 4;
      长度 == 15 时后面按 255 累加; offset 是 16 位小端; match 允许重叠。
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
        if i + 2 > n:                   # 没有 offset 了 = 流结束
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
        else:                           # 重叠复制(游程)
            for _ in range(mlen):
                out.append(out[start])
                start += 1
    return bytes(out)


def scanned_strings(blk: bytes, lo: int = 3, hi: int = 64) -> list[tuple[int, str]]:
    """逐偏移找「长度前缀 + 全 ASCII」的串, 命中就跳过整串。"""
    out = []
    i, n = 0, len(blk)
    while i < n - 1:
        ln = blk[i]
        if lo <= ln <= hi and i + 1 + ln <= n:
            raw = blk[i + 1:i + 1 + ln]
            if all(32 <= b < 127 for b in raw):
                out.append((i, raw.decode("ascii")))
                i += 1 + ln
                continue
        i += 1
    return out


@dataclass
class Creation:
    tick: int               # 包时间戳(ms), 与 MPI 通道同一基准
    eid: int
    template: str
    block: bytes
    vstart: int             # 组件值起点(块内字节偏移)


@dataclass
class Vehicle:
    """一辆载具的完整档案。"""
    uid: int
    eid: int
    born_t: float                       # 秒
    died_t: float | None                # 秒; None = 到对局结束还在
    template: str
    cls: str = ""                       # unit__className(tankModels/xxx)
    spawn: str = ""                     # tN_playerMM_K
    slot: int | None = None             # 玩家槽位
    player: str = ""                    # 玩家名
    team: int | None = None             # 1 / 2
    bind: str = ""                      # 绑定依据: kill / formula / none
    # 消失时刻的来源:
    #   "ecs"  = 0x26 销毁包(最硬的事实)
    #   "next" = 同一玩家的下一台车出现了(玩家同一时刻只能开一台)
    #   "end"  = 到对局结束还在场上 => 视为**没死**
    died_by: str = ""

    @property
    def short_cls(self) -> str:
        return self.cls.split("/")[-1] if self.cls else ""

    @property
    def label(self) -> str:
        who = self.player or (f"slot{self.slot}" if self.slot is not None else "")
        car = self.short_cls
        if who and car:
            return f"{who} · {car}"
        return who or car or f"uid{self.uid}"


@dataclass
class EcsResult:
    vehicles: list[Vehicle] = field(default_factory=list)
    templates: dict[int, int] = field(default_factory=dict)   # 模板名 -> 实例数
    group_size: int = 0                 # 每组人数 K(推断值; 0 = 没能推断)
    uid_offset: int | None = None       # 现场标定出的 uid 相对偏移
    stats: Counter = field(default_factory=Counter)


class EcsCollector:
    """在 `load_replay` 遍历包时喂数据, 最后 `build()` 出实体档案。"""

    def __init__(self) -> None:
        self._templates: dict[int, dict] = {}
        self._compdefs: set[int] = set()
        self._creations: list[Creation] = []
        self._destroys: dict[int, list[int]] = defaultdict(list)   # eid -> [tick, …]
        self.stats: Counter = Counter()

    # ---- 喂包 -----------------------------------------------------------
    def feed(self, payload: bytes, s: int, e: int, tick: int) -> None:
        ctrl = payload[s]
        if ctrl in CTRL_CREATE:
            self._feed_create(payload, s, e, tick)
        elif ctrl == CTRL_DESTROY:
            self._feed_destroy(payload, s, e, tick)

    def _feed_create(self, payload: bytes, s: int, e: int, tick: int) -> None:
        ctrl = payload[s]
        if ctrl == 0x25:
            try:
                body = lz4_block_decompress(payload[s + 1:e])
            except (ValueError, IndexError):
                self.stats["lz4_fail"] += 1
                return
            br = BitReader(body)
        else:
            br = BitReader(payload[s + 1:e])
        try:
            n = br.byte()
        except EOFError:
            self.stats["create_empty"] += 1
            return
        for _ in range(n + 1):
            try:
                eid = _read_eid(br)
                bs = br.compressed()
                block = br.bytes_n(bs)
            except EOFError:
                self.stats["outer_eof"] += 1
                return
            try:
                c = self._parse_block(tick, eid, block)
            except EOFError:
                self.stats["block_eof"] += 1
                break
            if c is not None:
                self._creations.append(c)
        self.stats["create_pkts"] += 1

    def _parse_block(self, tick: int, eid: int, block: bytes) -> Creation | None:
        """只读到「组件值起点」为止 —— 组件值一个都不解析(那才需要哈希表)。"""
        br = BitReader(block)
        tid = br.compressed()
        t = self._templates.get(tid)
        if t is None:
            name = _lenstr(br)
            ncomp = br.u16()
            comps = []
            for _ in range(ncomp):
                cid = br.compressed()
                if cid not in self._compdefs:
                    self._compdefs.add(cid)
                    br.u32()            # DataComponentHash
                    br.u32()            # ComponentHash
                comps.append(cid)
            t = {"id": tid, "name": name, "comps": comps}
            self._templates[tid] = t
        tcc = len(t["comps"])
        br.byte() if tcc < 256 else br.compressed()   # componentCount
        br.compressed()                              # 值区长度/标志(未解, 只需跳过)
        vstart = br.pos // 8
        return Creation(tick=tick, eid=eid, template=t["name"],
                        block=block, vstart=vstart)

    def _feed_destroy(self, payload: bytes, s: int, e: int, tick: int) -> None:
        try:
            br = BitReader(payload[s + 1:e])
            n = br.byte()
        except EOFError:
            return
        for _ in range(n + 1):
            try:
                eid = _read_eid(br)
            except EOFError:
                self.stats["destroy_eof"] += 1
                return
            # **eid 也会被复用**: 同一个 eid 可以是先后两个实体(实测 uid=114 的
            # 后一辆车拿到的是前一辆的销毁时刻, died_t 比 born_t 还早)。所以这里
            # 记**全部**销毁时刻, 匹配时再取「不早于创建时刻」的那个。
            self._destroys.setdefault(eid, []).append(tick)
        self.stats["destroy_pkts"] += 1

    def _died_at(self, eid: int, born_tick: int) -> int | None:
        """这辆车的销毁时刻 = 不早于它创建时刻的**第一个**销毁事件。"""
        for t in self._destroys.get(eid, ()):
            if t >= born_tick:
                return t
        return None

    # ---- 现场标定 uid 偏移 ----------------------------------------------
    @staticmethod
    def _calibrate_uid(creations: list[Creation], known: set[int]) -> int | None:
        """哪个相对偏移上的 u16 低 11 位最像 uid。

        判据两条件缺一不可:
          * 落进**已知 MPI uid 集合**(否则只是「值域恰好合理」);
          * **互异性够高** —— 常量字段(实测 +26 在飞机块里 50 个全是同一个值)
            也会 100% 命中已知集合, 那是假阳性。
        """
        best: tuple[tuple[int, int], int] | None = None
        for rel in range(0, 400):
            vals: list[int] | None = []
            for c in creations:
                o = c.vstart + rel
                if o + 2 > len(c.block):
                    vals = None
                    break
                vals.append((c.block[o] | (c.block[o + 1] << 8)) & UID_BITS)
            if not vals:
                break
            uniq = len(set(vals))
            if uniq < len(vals) * 0.5:
                continue
            inter = sum(1 for v in vals if v in known)
            if inter < 3:
                continue
            score = (inter, uniq)
            if best is None or score > best[0]:
                best = (score, rel)
        return best[1] if best else None

    # ---- 最终产出 -------------------------------------------------------
    def build(self, *, known_uids: set[int], players: dict, kills: list,
              include_air: bool = False, t_end: float | None = None) -> EcsResult:
        """把收集到的构造记录变成实体档案。

        `known_uids` 是 MPI 通道解出的地面 uid 集合 —— 只用来**标定 uid 偏移**;
        `players` 是 slot 包解出的 `{slot: PlayerSlot}`; `kills` 是击杀事件。
        """
        res = EcsResult(stats=self.stats)
        res.templates = dict(Counter(c.template for c in self._creations))

        want = [c for c in self._creations
                if is_unit_template(c.template)
                and (include_air or "aircraft" not in c.template)]
        if not want:
            res.stats["no_vehicle_template"] += 1
            return res

        # uid 偏移只在**地面**模板上标定：`aircraft+player_unit` 里有一大批是
        # 开局出生的占位实体(dummy_plane, 没有型号也没有 uid), 混进来会把标定带歪。
        ground_want = [c for c in want if "aircraft" not in c.template]
        rel = self._calibrate_uid(ground_want or want, known_uids)
        if rel is None:
            res.stats["uid_offset_failed"] += 1
            return res
        res.uid_offset = rel

        ground = not include_air
        for c in want:
            blk, vs = c.block, c.vstart
            o = vs + rel
            if o + 2 > len(blk):
                continue
            uid = (blk[o] | (blk[o + 1] << 8)) & UID_BITS
            if uid == EXT_MASK_UID:
                continue
            cls = ""
            spawn = ""
            for _off, s in scanned_strings(blk, 3, 60):
                if not spawn:
                    m = SPAWN_RE.match(s)
                    if m:
                        spawn = s
                        continue
                if not cls:
                    if GROUND_CLS_RE.match(s):
                        cls = s
                    elif not ground and AIR_CLS_RE.match(s) and s != "dummy_plane" \
                            and "player" not in s and "aircraft" not in s:
                        cls = s
                if spawn and cls:
                    break
            died = self._died_at(c.eid, c.tick)
            res.vehicles.append(Vehicle(
                uid=uid, eid=c.eid, born_t=c.tick / 1000.0,
                died_t=(died / 1000.0) if died else None,
                died_by="ecs" if died else "",
                template=c.template, cls=cls, spawn=spawn))

        self._bind_players(res, players, kills)
        self._fill_end_times(res, t_end)
        return res

    @staticmethod
    def _bind_players(res: EcsResult, players: dict, kills: list) -> None:
        """出生组名 -> 玩家槽位 -> 名字/阵营。

        证据优先级:
          1. **击杀事实**: 某 uid 在某一刻开火击杀某人, 那个 uid 就属于那个槽位。
             这是确定事实, 实测 32/32 与公式一致。
          2. **公式**: `t1_player(NN) -> NN-1`, `t2_player(NN) -> NN-1+K`。
             用于没有击杀记录的车。K 由击杀证据现场拟合。
        """
        name_of = {s: getattr(p, "display", "") or getattr(p, "name", "")
                   for s, p in players.items()}
        team_of = {s: getattr(p, "team", None) for s, p in players.items()}
        if not name_of:
            return

        by_uid: dict[int, list[Vehicle]] = defaultdict(list)
        for v in res.vehicles:
            by_uid[v.uid].append(v)

        # ---- 1. 击杀投票: 出生组名 -> 槽位
        votes: dict[str, Counter] = defaultdict(Counter)
        for k in kills:
            ku = getattr(k, "killer_uid", EXT_MASK_UID)
            kp = getattr(k, "killer_pid", 0)
            if ku == EXT_MASK_UID or not kp:
                continue
            for v in by_uid.get(ku, ()):
                if not v.spawn:
                    continue
                # uid 会被复用: 只在「这一枪发生在它活着的时候」才计票
                if v.born_t - 1.0 <= k.t <= (v.died_t + 1.0 if v.died_t else 1e9):
                    votes[v.spawn][kp] += 1
                    break
        observed = {sp: c.most_common(1)[0][0] for sp, c in votes.items() if c}

        # ---- 2. 拟合每组人数 K(t1 占前 K 个槽位, t2 占后 K 个)
        def slot_of(spawn: str, k: int) -> int | None:
            m = SPAWN_RE.match(spawn)
            if not m:
                return None
            tn, nn = int(m.group(1)), int(m.group(2))
            return nn - 1 if tn == 1 else nn - 1 + k

        best_k, best_hit = 0, 0
        for kcand in range(1, 33):
            hit = sum(1 for sp, slot in observed.items()
                      if slot_of(sp, kcand) == slot)
            if hit > best_hit:
                best_k, best_hit = kcand, hit
        if not best_k:
            # 没有 t2 的击杀证据 -> 用 t1 组里最大的 NN 当每组人数
            best_k = max((int(SPAWN_RE.match(sp).group(2))
                          for sp in {v.spawn for v in res.vehicles}
                          if SPAWN_RE.match(sp) and sp.startswith("t1_")),
                         default=0)
        res.group_size = best_k
        if observed and best_hit < len(observed):
            res.stats["formula_mismatch"] += len(observed) - best_hit

        # ---- 3. 写入
        for v in res.vehicles:
            slot = observed.get(v.spawn)
            v.bind = "kill" if slot is not None else "none"
            if slot is None:
                slot = slot_of(v.spawn, best_k)
                if slot is not None:
                    v.bind = "formula"
            if slot is None or slot not in name_of:
                # 槽位不存在(中途加入/退出导致偏移错位) -> 不猜
                v.bind = "none"
                continue
            v.slot = slot
            v.player = name_of.get(slot, "")
            v.team = team_of.get(slot)

    @staticmethod
    def _fill_end_times(res: EcsResult, t_end: float | None) -> None:
        """把「什么时候消失」补全。

        残骸会留在场上, ECS 不一定对所有载具发 0x26 —— 但**玩家同一时刻只能开
        一台车**, 所以下一台出现的那一刻, 上一台就结束了。这条规则把所有换车
        都切得干净, 而且不依赖击杀包(击杀只能覆盖被打死的)。
        """
        by_slot: dict[int, list[Vehicle]] = defaultdict(list)
        for v in res.vehicles:
            if v.slot is not None:
                by_slot[v.slot].append(v)
        for lst in by_slot.values():
            lst.sort(key=lambda v: v.born_t)
            for a, b in zip(lst, lst[1:]):
                if b.born_t > a.born_t and (a.died_t is None or a.died_t > b.born_t):
                    a.died_t = b.born_t
                    a.died_by = "next"
        if t_end:
            for v in res.vehicles:
                if v.died_t is None:
                    v.died_t = t_end
                    v.died_by = "end"
