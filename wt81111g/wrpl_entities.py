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
# AI 工事 / 机场防空的**任务对象名**, 形如 `t1_airfield_aaa_small_01` /
# `t2_airfield_aaa_big_03`。这不是型号, 但它是这些 AI 单位**唯一**的阵营线索:
# 前缀 `t1_` / `t2_` 就是边(玩家出生组名 `tN_playerMM_K` 的 tN 不是, 见模块 docstring)。
MISSION_OBJ_RE = re.compile(r"^t([12])_[a-z0-9][a-z0-9_.\-]{2,48}$")

# 模板名 -> 是否「玩家载具」。
# tank+player_unit / tank+unit_aps+player_unit / tank+cloth_wind+player_unit 都是
# 坦克的变体（后两个是 APS / 布篷这种附加模块）。**不能只用 `startswith("tank")`**:
# `tank_smokescreen_ecs`（坦克烟雾弹改装件，83 个）也以 tank 开头，但它没有 uid
# 和 className，混进来会把 uid 偏移标定带歪。
UNIT_TEMPLATE_EXACT = ("tank", "aircraft", "fortification")
UNIT_TEMPLATE_SUFFIX = "+player_unit"

# 开局每个玩家都有一台**占位飞机**(块里有明文 `dummy_plane`, 没有型号也没有 uid,
# 实测 32 个, 8~10 s 出生)。它不是玩家载具 —— 以前靠「寿命 <5 s」这种阈值筛,
# 现在按这个明文标记直接跳过。
PLACEHOLDER_MARK = b"dummy_plane"

# 同一次出生可能被建成好几个实体(实测 `tN_playerMM_0` 下会出现 2~5 条间隔
# 0.1~2 s、型号乱跳的记录 —— 选车/换车时游戏反复建实体)。**同一个 uid 上**
# 相邻且时间接得上的记录要合成一条寿命, 否则会凭空多出几条零点几秒的假线。
# 只合并「同一 uid」, 不同 uid 之间照旧按「下一台车出现 = 上一台结束」切段。
MERGE_GAP_SEC = 1.0


def is_unit_template(name: str) -> bool:
    return name in UNIT_TEMPLATE_EXACT or name.endswith(UNIT_TEMPLATE_SUFFIX)


# ---------------------------------------------------------------- 采样校验
def _count_between(stream: dict, uid: int, lo: float, hi: float | None) -> int:
    """`stream[uid]` 里时间落在 `[lo, hi]` 的点数(点元组第 0 项是秒)。"""
    pts = stream.get(uid)
    if not pts:
        return 0
    if hi is None:
        return sum(1 for p in pts if p[0] >= lo)
    return sum(1 for p in pts if lo <= p[0] <= hi)


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
    vstart: int             # 组件值起点(块内字节偏移, **向下取整**)
    # 组件值起点的**精确位偏移**。很多包的头部不是字节对齐的(实测占相当一部分),
    # 只按 `vstart` 的字节偏移去读 uid 会**错位几 bit** —— 那就是「少数创建包读出来的
    # uid 是垃圾(同一值上挤了 4~5 台不同玩家的车, 且该 uid 在位置流里没有采样)」的原因。
    vbits: int = 0
    ctrl: int = 0x24


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
    # 落在 [born, died] 窗口内的位置采样条数。0 = 这条记录可能读错了 uid
    # (实测同一台车被建成多条时, 只有一条真有采样)。由 `build()` 填。
    samples: int = 0
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
    uid_offset2: int | None = None      # 少数布局不同的块用的第二个偏移
    uid_offset_air: int | None = None   # 飞机块的偏移(组件布局与地面不同)
    stats: Counter = field(default_factory=Counter)


class EcsCollector:
    """在 `load_replay` 遍历包时喂数据, 最后 `build()` 出实体档案。"""

    def __init__(self) -> None:
        self._templates: dict[int, dict] = {}
        # cid -> (DataComponentHash, ComponentHash)。两个 hash 必须留着：
        # 组件值要怎么读，由 ComponentHash 对应的**类型名**决定（int / bool /
        # ecs::string / *StorageComponent …），DataComponentHash 只给字段名。
        self._compdefs: dict[int, tuple[int, int]] = {}
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
        ctrl_lz4 = payload[s]
        if ctrl_lz4 == 0x25:
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
                c = self._parse_block(tick, eid, block, ctrl_lz4)
            except EOFError:
                self.stats["block_eof"] += 1
                break
            if c is not None:
                self._creations.append(c)
        self.stats["create_pkts"] += 1

    def _parse_block(self, tick: int, eid: int, block: bytes,
                     ctrl: int = 0x24) -> Creation | None:
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
                    self._compdefs[cid] = (br.u32(), br.u32())
                comps.append(cid)
            t = {"id": tid, "name": name, "comps": comps}
            self._templates[tid] = t
        tcc = len(t["comps"])
        br.byte() if tcc < 256 else br.compressed()   # componentCount
        br.compressed()                              # 值区长度/标志(未解, 只需跳过)
        vbits = br.pos
        return Creation(tick=tick, eid=eid, template=t["name"],
                        block=block, vstart=vbits // 8, vbits=vbits, ctrl=ctrl)

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
    def _uid_at(c: Creation, rel: int) -> int | None:
        """在「组件值起点 + rel 字节」处取 16 位(**小端**, 字段内低字节在前), &0x7FF。

        用 `vbits`(精确位偏移) 而不是 `vstart`(字节, 向下取整), 这样包头部
        不是字节对齐时也不会错位。
        """
        bit = c.vbits + rel * 8
        byte = bit >> 3
        sh = bit & 7
        blk = c.block
        if byte + 1 >= len(blk):
            return None
        v = blk[byte] | (blk[byte + 1] << 8)
        if byte + 2 < len(blk):
            v |= blk[byte + 2] << 16
        return (v >> sh) & UID_BITS

    @staticmethod
    def _spawn_of(c: Creation) -> str:
        """块里的**出生组名**(`tN_playerMM_K`); 没有就返回空串 —— 有它就是玩家载具。"""
        for _off, s in scanned_strings(c.block, 3, 60):
            if SPAWN_RE.match(s):
                return s
        return ""

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
            vals: list[int] = []
            ok = True
            for c in creations:
                v = EcsCollector._uid_at(c, rel)
                if v is None:
                    ok = False
                    break
                vals.append(v)
            if not ok or not vals:
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
              include_air: bool = False, t_end: float | None = None,
              ground_pts: dict | None = None, air_pts: dict | None = None
              ) -> EcsResult:
        """把收集到的构造记录变成实体档案。

        `known_uids` 是 MPI 通道解出的地面 uid 集合 —— 只用来**标定 uid 偏移**;
        `players` 是 slot 包解出的 `{slot: PlayerSlot}`; `kills` 是击杀事件。
        `ground_pts` / `air_pts` 是位置流(uid -> [(秒, …)]), 用来给每条记录数
        「窗口内到底有多少个真实采样」—— 那是判断记录真假/合并同一台车的硬事实。
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

        # 飞机块的组件布局与地面不同(`rel` 上常常读出 0/1 这种值)。有空中位置流时
        # 单独标定一次, 标不出来就沿用 `rel`。
        air_want = [c for c in want
                    if "aircraft" in c.template and PLACEHOLDER_MARK not in c.block]
        rel_air = rel
        if air_pts and air_want:
            cand = self._calibrate_uid(air_want, set(air_pts))
            if cand is not None and cand != rel:
                rel_air = cand
                res.uid_offset_air = cand
                res.stats["uid_offset_air"] = len(air_want)


        # ⚠ 实测: 75 个 `tank+player_unit` 里总有 18 个在 `rel` 上读出垃圾
        #   (同一值上挤 4~5 台车、且该 uid 在位置流里没有采样)。它们的组件值布局
        #   比主流多 1 个字节, 所以 uid 实际在 `rel+1`。办法不是猜, 而是**把这批
        #   挑出来再标定一次** —— 同一个“命中已知 uid + 互异性”规则在子集上会给出
        #   `rel+1`(实测 18/18 命中、17 个互异), 而常量字段(如 +27 恒为 56)被
        #   互异性门槛挡掉。
        rel2: int | None = None
        if known_uids:
            off = [c for c in (ground_want or want)
                   if self._uid_at(c, rel) not in known_uids
                   and self._spawn_of(c)]
            if len(off) >= 3:
                rel2 = self._calibrate_uid(off, known_uids)
                if rel2 == rel:
                    rel2 = None
                else:
                    res.uid_offset2 = rel2
                    res.stats["uid_offset2"] = len(off)

        for c in want:
            blk, vs = c.block, c.vstart
            is_air = "aircraft" in c.template
            if is_air and PLACEHOLDER_MARK in blk:
                res.stats["air_placeholder"] += 1
                continue
            # 型号的判定分支必须**按模板**而不是按全局开关：
            # `include_air=True` 时以前的写法会让地面模板(工事/机场防空)也去走
            # “机型词”分支，于是 `t1_airfield_aaa_small_01` 这种任务对象名
            # 被当成型号写了进去。
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
                    elif not is_air and MISSION_OBJ_RE.match(s) and not SPAWN_RE.match(s):
                        cls = s
                    elif is_air and AIR_CLS_RE.match(s) and s != "dummy_plane" \
                            and "player" not in s and "aircraft" not in s:
                        cls = s
                if spawn and cls:
                    break
            # uid 最后取: 在 `rel` 上不合法就试 `rel2`(只对**玩家载具** ——
            # 工事/AI 本来就不在位置流里, 不要让它们借道子集偏移).
            uid = self._uid_at(c, rel_air if is_air else rel)
            if (uid is None or uid == EXT_MASK_UID
                    or (known_uids and uid not in known_uids and not is_air)):
                if rel2 is not None and spawn:
                    alt = self._uid_at(c, rel2)
                    if alt is not None and alt != EXT_MASK_UID \
                            and (not known_uids or alt in known_uids):
                        uid = alt
            if uid is None or uid == EXT_MASK_UID:
                continue
            died = self._died_at(c.eid, c.tick)
            born_t = c.tick / 1000.0
            died_t = (died / 1000.0) if died else None
            res.vehicles.append(Vehicle(
                uid=uid, eid=c.eid, born_t=born_t,
                died_t=died_t,
                died_by="ecs" if died else "",
                template=c.template, cls=cls, spawn=spawn,
                samples=_count_between(
                    (air_pts if is_air else ground_pts) or {}, uid,
                    born_t, died_t if died_t is not None else t_end)))

        self._bind_players(res, players, kills)
        self._merge_same_uid(res)
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

        # ---- 4. 没有出生组名的(无人侦察机 UCAV 等): 用击杀事实定主人
        # UCAV 是玩家放出去的, 但它的构造块里**没有** `tN_playerMM_K`。
        # 只用**它自己击杀别人**这条:`killer_uid == 该 uid` 时的 `killer_pid` 就是主人。
        #
        # ⚠ **不能反向用 `victim_pid`**(它被击落时): 那个字段解出来的是垃圾 ——
        #   拿 ECS 的出生组名当裁判核对 88 条击杀, `killer_pid` **65/65 全对**,
        #   而 `victim_pid` **56/57 全错**(53 条的"victim_pid"都是 14, 根本不是槽位)。
        #   以前用它给 UCAV 定主人, 结果把 26 个 uid 全算到同一个玩家名下。
        # 事件时刻要落在存活窗口内: uid 会复用, 时间不对就会张冠李戴。
        for v in res.vehicles:
            if v.slot is not None:
                continue
            for k in kills:
                if getattr(k, "killer_uid", EXT_MASK_UID) != v.uid:
                    continue
                if not (v.born_t - 1.0 <= k.t
                        <= ((v.died_t + 1.0) if v.died_t is not None else 1e9)):
                    continue
                pid = getattr(k, "killer_pid", 0)
                if not pid or pid not in name_of:
                    continue
                v.slot = pid
                v.player = name_of.get(pid, "")
                v.team = team_of.get(pid)
                v.bind = "kill-uid"
                break

        # ---- 5. AI 工事 / 机场防空: 任务对象名里的 `t1_` / `t2_` 前缀
        # (`t1_airfield_aaa_small_01` / `t2_airfield_aaa_big_03`)。这些是 AI 控制的
        # 防空炮/工事, 没有出生组名也不会被击落。
        #
        # ⚠ 前缀与队伍编号是**反的**: `t1_` 属于 team 2, `t2_` 属于 team 1。
        # 两条独立验证(2026-10-25, avg_finland):
        #   * 机场出生的飞机: 已知 team1 的 12 架首点全部靠近 `t2_` 机群;
        #   * 地面载具出生点: team1 的坦克 29/29 靠 `t2_`, team2 的 35/36 靠 `t1_`。
        # 注: 玩家出生组名 `tN_playerMM_K` 的 tN **不是**阵营(见模块 docstring),
        # 这里是另一套命名, 两者不要混。
        for v in res.vehicles:
            if v.team is not None:
                continue
            m = re.match(r"^t([12])_(?!player)", v.cls or "")
            if m:
                v.team = 3 - int(m.group(1))
                v.bind = "name-prefix"

    @staticmethod
    def _merge_same_uid(res: EcsResult) -> None:
        """同一个 uid 上「接得上」的多条创建记录 = 同一台车, 合成一条寿命。

        实测(`tN_playerMM_0`, Abandoned factory 服务器回放): 换车/选车时游戏会在
        1~2 s 内反复建实体 —— `uid=306` 有 4 条记录(235.78/236.52/238.93/240.84),
        型号 `cn_ztz_99a → cn_m1a2t → cn_ztz_99a → cn_vt_4b` 乱跳, 每条寿命 0.7~2 s。
        以前它们被 `_fill_end_times` 当成「下一台车」逐条截断, 于是凭空多出 3 条
        零点几秒的假线; 真正的那台(240.84~304.35, 窗口内有 1490 个采样)也被切短。

        合并后取「采样更多」的那条当代表(型号/模板跟它走), 寿命取并集。
        """
        groups: dict[tuple, list[Vehicle]] = defaultdict(list)
        for v in res.vehicles:
            groups[(v.slot, v.uid) if v.slot is not None else ("?", id(v))].append(v)
        kept: list[Vehicle] = []
        merged = 0
        for lst in groups.values():
            if len(lst) == 1:
                kept.append(lst[0])
                continue
            lst.sort(key=lambda v: v.born_t)
            cur = lst[0]
            for nxt in lst[1:]:
                # 合并判据: 前一条**窗口内没有任何采样**(说明它只是选车/换车时临时建的
                # 实体, 从没真正出现在位置流里), 且时间接得上。
                # ⚠ 光看时间不够: 玩家快速重生时同一 uid 会紧接着再出生一次, 那两条
                #   都有采样, 必须分开(实测按纯时间合并会让 7 个玩家的线数偏少)。
                # ⚠ `died_t is None`(没有 0x26 销毁包)时不知道边界, 一律不合并。
                if (cur.samples or cur.died_t is None
                        or nxt.born_t - cur.died_t > MERGE_GAP_SEC):
                    kept.append(cur)
                    cur = nxt
                    continue
                rep = nxt if nxt.samples > cur.samples else cur
                cur.samples = max(cur.samples, nxt.samples)
                cur.template = rep.template
                cur.cls = rep.cls or cur.cls
                # 终点**无条件**跟最后一条走: 合并发生在这批记录的 `died_t` 还没被
                # `_fill_end_times` 补全之前, 最后一条常常是 `None`(等下一台车来截)。
                # 用 max() 会让"临时实体早死"的终点盖住真车的终点, 整条寿命被吞掉。
                cur.died_t = nxt.died_t
                cur.died_by = nxt.died_by
                merged += 1
            kept.append(cur)
        if merged:
            res.stats["merged_same_uid"] = merged
            res.vehicles = kept

    @staticmethod
    def _fill_end_times(res: EcsResult, t_end: float | None) -> None:
        """把「什么时候消失」补全。

        残骸会留在场上, ECS 不一定对所有载具发 0x26 —— 但**玩家同一时刻只能开
        一台车**, 所以下一台出现的那一刻, 上一台就结束了。这条规则把所有换车
        都切得干净, 而且不依赖击杀包(击杀只能覆盖被打死的)。

        ⚠ **只对「有出生组名」的实体生效**。无人侦察机(UCAV)是玩家放出去的,
        它会和玩家的坦克**并行存在**, 把它算进链条就会把坦克截成零点几秒
        (实测 `GHOST40044` 的 `ussr_t_90m_arena_m` 被自己的 UCAV 顶掉)。
        """
        by_slot: dict[int, list[Vehicle]] = defaultdict(list)
        for v in res.vehicles:
            if v.slot is not None and v.spawn:
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
