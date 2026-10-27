"""结算表(results BLK)解析 —— 录像**最后一个分片**尾部的那块 Dagor BLK。

内容: 对局状态 / 时长 / 每位玩家的名字·军团·队伍·战绩(`kills`/`deaths`/`score`…),
以及 `matchingInfo` 里按 **userId** 组织的出击队列(`crafts_info`)。

格式(逐字节验证过: 块数、字段数、嵌套表长度都恰好用尽, `nested` 零剩余):

    +0   type        = 0x01 (FAT)
         n_names     ULEB
         names_size  ULEB
         names       names_size 字节 (NUL 分隔的串表)
         n_blocks    ULEB
         n_fields    ULEB
         params_size ULEB          ← 这里**只有 3 个 ULEB**, 没有"参数个数"
         params      params_size 字节 (字符串池)
         fields_def  n_fields × 8   `name_id u24 LE | type_id u8 | data u32 LE`
         nested      剩余全部       每块 = `ULEB name_id | ULEB 字段数 | ULEB 子块数
                                           | (子块数>0 ? ULEB 首子块 : 无)`

三个坑(都踩过, 别再踩):
  1. 头部只有 3 个 ULEB。按"块数/字段数/参数个数/参数字节数"读 4 个, 字段定义表会
     整体错 1 字节, 字段名全变成 0x10000 的倍数。
     铁证: 偏移 684 处 `ba 02 e2 0c d3 11 73 75 63 63 65 73 73` = 314/1634/2259,
     紧跟的 `73` 是 `"success"` 的首字母(字符串池开头), 不是 ULEB。
  2. **块名 1 基**(0 = 根), **字段名 0 基**。混用会整体偏 2 项。
  3. `params_size` 是**字节数**, 不是条数。

关于 `rank` / `mrank` / `rankUnused`(2026-10-27 与用户核对过):
  * `rank` = 载具等级(Rank)。陆战/空战各自独立编号, 当前上限分别是 8 / 9;
  * `mrank` = 回放内部的整数档位, **不是权重(BR)**, 也不能除 4 换算;
  * `rankUnused` = 布尔, `False` 的那台是"代表本队列权重"的载具(每人恰好 1 条);
  * ⚠ 等级与权重每版都可能调整 —— 代码里**不要写死**, 详情只当"当时的快照"。
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import wrpl_reader as RD

T_STRING, T_INT, T_FLOAT = 0x01, 0x02, 0x03
T_F2, T_F3, T_F4 = 0x04, 0x05, 0x06
T_I2, T_I3 = 0x07, 0x08
T_BOOL, T_COLOR, T_F12, T_LONG = 0x09, 0x0A, 0x0B, 0x0C

TYPENAME = {
    T_STRING: "Str", T_INT: "Int", T_FLOAT: "Float",
    T_F2: "Float2", T_F3: "Float3", T_F4: "Float4",
    T_I2: "Int2", T_I3: "Int3", T_BOOL: "Bool",
    T_COLOR: "Color", T_F12: "Float12", T_LONG: "Long",
}

# 面板上要展示的字段(顺序即展示顺序)。其余字段照样解进 `raw`, 只是不单独取。
SHOWN_FIELDS = (
    "name", "clanTag", "team", "kills", "groundKills", "deaths",
    "assists", "captureZone", "damageZone", "score", "autoSquad", "squadId",
)


def _uleb(buf: bytes, p: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        b = buf[p]
        p += 1
        v |= (b & 0x7F) << shift
        if not (b & 0x80):
            return v, p
        shift += 7
        if shift > 63:
            raise ValueError("ULEB 过长")


class Blk:
    """FAT BLK 容器: 名字表 / 字符串池 / 字段定义 / 块树。"""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        if not buf:
            raise ValueError("空的 BLK")
        if buf[0] != 0x01:
            raise ValueError(f"不是 FAT BLK(首字节 0x{buf[0]:02x})")
        p = 1
        self.n_names, p = _uleb(buf, p)
        names_size, p = _uleb(buf, p)
        blob = buf[p:p + names_size]
        p += names_size
        self.names = blob.split(b"\x00")
        self.n_blocks, p = _uleb(buf, p)
        self.n_fields, p = _uleb(buf, p)
        params_size, p = _uleb(buf, p)      # ⚠ 只有 3 个 ULEB, 见模块 docstring
        self.params = buf[p:p + params_size]
        p += params_size
        self.fdef = buf[p:p + self.n_fields * 8]
        p += self.n_fields * 8
        self.nested = buf[p:]

    # ---------------------------------------------------------------- 名字
    def fname(self, idx: int) -> str:
        """**字段名: 0 基**。"""
        if 0 <= idx < len(self.names):
            return self.names[idx].decode("utf-8", "replace")
        return f"<bad fname #{idx}>"

    def bname(self, idx: int) -> str:
        """**块名: 1 基**; 0 表示根块。"""
        if idx == 0:
            return "<root>"
        if 1 <= idx <= len(self.names):
            return self.names[idx - 1].decode("utf-8", "replace")
        return f"<bad bname #{idx}>"

    # ---------------------------------------------------------------- 块
    def blocks(self) -> list[dict]:
        """按嵌套表顺序解出全部块(含字段定义切片下标)。"""
        out: list[dict] = []
        p = cursor = 0
        buf = self.nested
        for _ in range(self.n_blocks):
            try:
                nid, p = _uleb(buf, p)
                nf, p = _uleb(buf, p)
                nsub, p = _uleb(buf, p)
                first = 0
                if nsub > 0:
                    first, p = _uleb(buf, p)
            except (IndexError, ValueError):
                break
            out.append(dict(name_id=nid, name=self.bname(nid), n_fields=nf,
                            n_sub=nsub, first_sub=first, field_start=cursor))
            cursor += nf
        return out

    def fields(self, blk: dict) -> list[dict]:
        fs: list[dict] = []
        for i in range(blk["n_fields"]):
            off = (blk["field_start"] + i) * 8
            fd = self.fdef[off:off + 8]
            if len(fd) < 8:
                break
            nid = fd[0] | (fd[1] << 8) | (fd[2] << 16)
            tid = fd[3]
            fs.append(dict(name=self.fname(nid), type=tid,
                           type_name=TYPENAME.get(tid, f"0x{tid:02x}"),
                           value=self._value(tid, fd[4:8])))
        return fs

    def _value(self, tid: int, raw: bytes):
        d = struct.unpack("<I", raw)[0]
        try:
            if tid == T_STRING:
                return self.params[d:].split(b"\x00")[0].decode("utf-8", "replace")
            if tid == T_INT:
                return struct.unpack("<i", raw)[0]
            if tid == T_FLOAT:
                return round(struct.unpack("<f", raw)[0], 5)
            if tid == T_BOOL:
                return bool(d)
            if tid == T_LONG:
                return struct.unpack("<q", raw)[0]
            if tid == T_F2:
                return struct.unpack("<2f", self.params[d:d + 8])
            if tid == T_F3:
                return struct.unpack("<3f", self.params[d:d + 12])
            if tid == T_F4:
                return struct.unpack("<4f", self.params[d:d + 16])
            if tid == T_I2:
                return struct.unpack("<2i", self.params[d:d + 8])
            if tid == T_I3:
                return struct.unpack("<3i", self.params[d:d + 12])
            if tid == T_F12:
                return struct.unpack("<12f", self.params[d:d + 48])
        except (struct.error, IndexError):
            return None
        return None


# ---------------------------------------------------------------- 结果对象
@dataclass
class Results:
    """一局的结算表摘要(存进 `GlobalRecord.results`, 供面板展示)。"""
    status: str = ""
    time_played: float = 0.0
    players: list[dict] = field(default_factory=list)

    def team_counts(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for p in self.players:
            t = p.get("team")
            if isinstance(t, int):
                out[t] = out.get(t, 0) + 1
        return out

    def scores(self) -> int:
        return sum(int(p.get("score") or 0) for p in self.players)

    def as_dict(self) -> dict:
        """落盘用(只留面板要展示的字段, 体积很小)。"""
        return {
            "status": self.status,
            "time_played": round(float(self.time_played), 3),
            "players": self.players,
        }

    @classmethod
    def from_dict(cls, data) -> "Results | None":
        if not isinstance(data, dict):
            return None
        rows = [p for p in (data.get("players") or []) if isinstance(p, dict)]
        return cls(status=str(data.get("status") or ""),
                   time_played=float(data.get("time_played") or 0.0),
                   players=rows)


def parse_results(buf: bytes) -> Results:
    """一块结算 BLK 原始字节 → `Results`。"""
    blk = Blk(buf)
    blocks = blk.blocks()
    res = Results()
    for b in blocks:
        if b["name"] == "<root>":
            d = {f["name"]: f["value"] for f in blk.fields(b)}
            res.status = str(d.get("status") or "")
            res.time_played = float(d.get("timePlayed") or 0.0)
        elif b["name"] == "player":
            d = {f["name"]: f["value"] for f in blk.fields(b)}
            row = {k: d.get(k) for k in SHOWN_FIELDS if k in d}
            if row.get("name"):
                res.players.append(row)
    return res


def load_results(path: str) -> Results | None:
    """读回放(单文件或服务器分片目录)的结算表; 没有(本地录像 / 无结果段)返回 None。

    只读**最后一个分片**的 `results_offset` 之后的字节, 不碰包流, 几十毫秒。
    """
    try:
        if os.path.isdir(path):
            files, infos = RD._scan_parts(path)
        else:
            files, infos = [path], [RD.read_header_file(path)]
        if not files:
            return None
        off = int(getattr(infos[-1], "results_offset", 0) or 0)
        if off <= 0:
            return None
        with open(files[-1], "rb") as fh:
            fh.seek(off)
            buf = fh.read()
        if not buf:
            return None
        res = parse_results(buf)
        return res if res.players else None
    except (OSError, ValueError, IndexError, struct.error):
        return None
