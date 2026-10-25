"""地图校准: 「回放里的世界坐标(米) → 地图图片像素」的变换。

**为什么需要它**(2026-10-25 实测): 8111 的 `/map_info.json` 给出的
`map_min` / `map_max` 是**地图图片覆盖的世界矩形**(米), 而 `/map_obj.json` 里
所有对象的 `x` / `y` 都是这个矩形内的 0~1 归一化值:

    px = (x - min_x) / (max_x - min_x) * 图片宽
    py = (max_z - z) / (max_z - min_z) * 图片高     ← 北在上

回放里的世界坐标和 8111 是**同一套**(实测: 8111 的坦克重生点簇
(1054~1157, 371~422) 与回放里同一张图的对象坐标吻合)。所以只要拿到这张图的
`map_min` / `map_max` + 图片尺寸, 回放的路线就能**准确落在底图上**, 而不是像
以前那样拿"本局观测到的 min/max"去凑。

实测样例(finland, 3 战区): `map_min [0,0]` / `map_max [2048,2048]`,
`map.jpg` 2048×2048 → **1 像素 = 1 米**。

数据来源: 用户进一局按 `tools/wrpl/capture_8111.py` 抓一次(见该脚本)。
**一张图只需要做一次**; 游戏更新后重抓即可。
"""
from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass, field

from .config import resource_path, data_dir

log = logging.getLogger("map_calib")

# 回放导入的线路用的地图键前缀(`wrpl_<level>`)—— 必须是**文件名安全**的:
# 地图底图缓存以 `<地图键>.<ext>` 存盘, 用 `wrpl:xxx` 里的冒号在 Windows 上是非法字符。
MAP_KEY_PREFIX = "wrpl_"

# 抓取目录里的文件名(与 tools/wrpl/capture_8111.py 的产出保持一致)
F_MAP_INFO = "map_info.json"
F_MAP_OBJ = "map_obj.jsonl"
F_MAP_IMG_PNG = "map.png"
F_MAP_IMG_JPG = "map.jpg"
F_CALIB = "calib.json"          # 已经是 MapCalib 时直接读这个


def image_size(data: bytes) -> tuple[int, int]:
    """从 PNG / JPEG 头部取 (宽, 高); 认不出返回 (0, 0)。"""
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return int(w), int(h)
    if len(data) > 4 and data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return int(w), int(h)
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seg
    return 0, 0


def _kind_of(obj: dict) -> str:
    for f in ("type", "icon"):
        raw = str(obj.get(f) or "").strip().lower()
        if raw.startswith("respawn_base_"):
            return raw[len("respawn_base_"):]
        if raw:
            return raw
    return ""


@dataclass
class MapCalib:
    """一张地图的校准数据(世界矩形 + 图片尺寸 + 战区 / 重生点)。"""

    min_x: float = 0.0
    max_x: float = 0.0
    min_z: float = 0.0
    max_z: float = 0.0
    img_w: int = 0
    img_h: int = 0
    source: str = ""                # 抓取目录名 / 来源说明
    captured_at: str = ""
    # 世界坐标(米) —— 已经换算好, 画图直接用
    zones: list[dict] = field(default_factory=list)
    spawns: list[dict] = field(default_factory=list)
    # 抓取时看到的对象种类统计(排查用)
    kinds: dict = field(default_factory=dict)

    # ---- 基本属性
    @property
    def span_x(self) -> float:
        return self.max_x - self.min_x

    @property
    def span_z(self) -> float:
        return self.max_z - self.min_z

    @property
    def valid(self) -> bool:
        return self.span_x > 0 and self.span_z > 0

    # ---- 坐标换算
    def norm(self, x: float, z: float) -> tuple[float, float]:
        """世界米 → 0~1(可超出, 飞机常在框外)。"""
        return ((x - self.min_x) / self.span_x,
                (self.max_z - z) / self.span_z)

    def pixel(self, x: float, z: float) -> tuple[float, float]:
        """世界米 → 图片像素。"""
        u, v = self.norm(x, z)
        return (u * self.img_w, v * self.img_h)

    def world(self, u: float, v: float) -> tuple[float, float]:
        """0~1 → 世界米。"""
        return (self.min_x + u * self.span_x,
                self.max_z - v * self.span_z)

    # ---- 序列化
    def to_dict(self) -> dict:
        return {
            "min_x": self.min_x, "max_x": self.max_x,
            "min_z": self.min_z, "max_z": self.max_z,
            "img_w": self.img_w, "img_h": self.img_h,
            "source": self.source, "captured_at": self.captured_at,
            "zones": self.zones, "spawns": self.spawns, "kinds": self.kinds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MapCalib":
        def _f(k: str, dv: float = 0.0) -> float:
            try:
                return float(d.get(k, dv))
            except (TypeError, ValueError):
                return dv

        def _i(k: str) -> int:
            try:
                return int(d.get(k, 0))
            except (TypeError, ValueError):
                return 0
        return cls(
            min_x=_f("min_x"), max_x=_f("max_x"),
            min_z=_f("min_z"), max_z=_f("max_z"),
            img_w=_i("img_w"), img_h=_i("img_h"),
            source=str(d.get("source") or ""),
            captured_at=str(d.get("captured_at") or ""),
            zones=[z for z in (d.get("zones") or []) if isinstance(z, dict)],
            spawns=[s for s in (d.get("spawns") or []) if isinstance(s, dict)],
            kinds=dict(d.get("kinds") or {}),
        )

    def describe(self) -> str:
        """一行人类可读描述(给地图管理器/状态栏用)。"""
        if not self.valid:
            return "无校准"
        return (f"{self.span_x:.0f}×{self.span_z:.0f} m · "
                f"图片 {self.img_w}×{self.img_h} · "
                f"战区 {len(self.zones)} · 重生点 {len(self.spawns)}")


def load_from_capture(dir_path: str) -> MapCalib:
    """从一个 `capture_8111.py` 的输出目录(或已导出的 calib.json)读出校准。

    认这两种:
      * `<dir>/calib.json`  —— 已经是导出好的;
      * `<dir>/map_info.json` + `map_obj.jsonl` + `map.jpg|png` —— 原始抓取目录。
    """
    if not dir_path or not os.path.isdir(dir_path):
        raise ValueError(f"不是一个文件夹: {dir_path}")

    calib_path = os.path.join(dir_path, F_CALIB)
    if os.path.isfile(calib_path):
        with open(calib_path, encoding="utf-8") as f:
            return MapCalib.from_dict(json.load(f))

    info_path = os.path.join(dir_path, F_MAP_INFO)
    if not os.path.isfile(info_path):
        raise ValueError(
            "这个文件夹里没有 map_info.json / calib.json ——\n"
            "请选 capture_8111.py 抓取生成的那个目录。")
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)

    # 图片尺寸
    img: bytes | None = None
    for name in (F_MAP_IMG_PNG, F_MAP_IMG_JPG):
        p = os.path.join(dir_path, name)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    img = f.read()
            except OSError:
                img = None
            break

    # 最后一轮地图对象 → 战区 / 重生点
    obj_path = os.path.join(dir_path, F_MAP_OBJ)
    objs: list = []
    if os.path.isfile(obj_path):
        last = ""
        with open(obj_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if last:
            try:
                objs = json.loads(last).get("objects") or []
            except (ValueError, AttributeError):
                objs = []

    return build_from_live(info, img, objs,
                           source=os.path.basename(os.path.normpath(dir_path)))


def build_from_live(info: dict, img: bytes | None, objects,
                    *, source: str = "", captured_at: str = "") -> MapCalib:
    """从「8111 现场数据」造校准: `map_info` + 地图片字节 + 一轮 `map_obj` 对象。

    这是 `load_from_capture()` 与「地图管理 → 补全信息」**共用**的那一处逻辑 ——
    两条入口解析出来的东西必须完全一致, 所以只有这一份实现。

    抛 `ValueError` 的三种情况(都带给人看的原因): 不在战局里 / 外框不完整 / 外框无效。
    """
    if not info.get("valid"):
        raise ValueError("这次抓取时不在战局里（map_info.valid=false），数据不可用。")

    try:
        lo = [float(v) for v in (info.get("map_min") or [])]
        hi = [float(v) for v in (info.get("map_max") or [])]
        assert len(lo) >= 2 and len(hi) >= 2
    except (TypeError, ValueError, AssertionError) as exc:
        raise ValueError("map_info 里的 map_min / map_max 不完整。") from exc

    calib = MapCalib(min_x=lo[0], max_x=hi[0], min_z=lo[1], max_z=hi[1],
                     source=source,
                     captured_at=captured_at or str(info.get("time") or ""))
    if not calib.valid:
        raise ValueError("map_max 与 map_min 相同，这张图的校准无效。")

    if img:
        w, h = image_size(img[:65536])
        if w and h:
            calib.img_w, calib.img_h = w, h

    kinds: dict[str, int] = {}
    seen_zone: set = set()
    seen_spawn: set = set()
    for o in objects or []:
        if not isinstance(o, dict):
            continue
        kind = _kind_of(o)
        if not kind:
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "capture_zone":
            label = str(o.get("zone_label") or o.get("zone_id") or "?")
            if label in seen_zone:
                continue
            seen_zone.add(label)
            try:
                x, z = calib.world(float(o["x"]), float(o["y"]))
            except (KeyError, TypeError, ValueError):
                continue
            calib.zones.append({"label": label, "x": round(x, 1), "z": round(z, 1),
                                "color": str(o.get("color") or "")})
        elif kind != "player":
            try:
                x, z = calib.world(float(o["x"]), float(o["y"]))
            except (KeyError, TypeError, ValueError):
                # 没有 x/y 的(如 respawn_base_airfield) 只记数量
                key = (kind, "no-pos", str(o.get("color")))
                if key in seen_spawn:
                    continue
                seen_spawn.add(key)
                calib.spawns.append({"kind": kind, "x": None, "z": None,
                                     "color": str(o.get("color") or "")})
                continue
            key = (kind, round(x, 1), round(z, 1))
            if key in seen_spawn:
                continue
            seen_spawn.add(key)
            calib.spawns.append({"kind": kind,
                                 "x": round(x, 1), "z": round(z, 1),
                                 "color": str(o.get("color") or "")})

    calib.zones.sort(key=lambda z: str(z.get("label")))
    calib.spawns.sort(key=lambda s: (str(s.get("kind")),
                                     s.get("x") if s.get("x") is not None else 0.0))
    calib.kinds = kinds
    return calib


def save_to_capture(dir_path: str, calib: MapCalib) -> str:
    """把校准写成 `<dir>/calib.json`(方便分发/再导入)。"""
    p = os.path.join(dir_path, F_CALIB)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(calib.to_dict(), f, ensure_ascii=False, indent=1)
    return p


# ---------------------------------------------------------------- 图片 + 预置校准
IMAGE_NAMES = (F_MAP_IMG_PNG, F_MAP_IMG_JPG)


def load_image(dir_path: str) -> bytes | None:
    """读出抓取目录里的地图片字节(`map.png` 优先, 其次 `map.jpg`)。"""
    for name in IMAGE_NAMES:
        p = os.path.join(dir_path, name)
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except OSError as exc:      # noqa: BLE001 - 读不到就当没有
                log.warning("读取地图片失败 %s: %s", p, exc)
                return None
    return None


def bundled_dir() -> str:
    """随软件分发的预置校准目录(`assets/map_calib`, 打包后在解压目录里)。"""
    return resource_path(os.path.join("assets", "map_calib"))


def collected_dir() -> str:
    """用户自己补全的校准目录(`data/map_calib/<关卡名>.json`)。

    为什么要单独存一份文件: 「补全信息」给一张图做出了校准以后, 这些文件可以
    直接拷进下一版软件的 `assets/map_calib/` 随包分发 —— 用户什么都不用做。
    """
    d = os.path.join(data_dir(), "map_calib")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:      # noqa: BLE001 - 目录开不了不该影响主流程
        log.warning("创建校准目录失败 %s: %s", d, exc)
    return d


def export_level(level: str, calib: MapCalib) -> str:
    """把校准写成 `data/map_calib/<关卡名>.json`, 返回路径。"""
    d = collected_dir()
    p = os.path.join(d, f"{level or 'unknown'}.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(calib.to_dict(), f, ensure_ascii=False, indent=1)
    except OSError as exc:      # noqa: BLE001
        log.warning("写校准文件失败 %s: %s", p, exc)
        return ""
    return p


def level_of(map_key: str) -> str:
    """地图键 → 关卡名。回放导入的键是 `wrpl_<level>`, 其它(哈希)键返回空串。"""
    k = (map_key or "")
    return k[len(MAP_KEY_PREFIX):] if k.startswith(MAP_KEY_PREFIX) else ""


def load_bundled(level: str) -> MapCalib | None:
    """读用户补全过的 / 随软件预置的 `<level>.json`(都没有返回 None)。

    优先级: 用户在「地图管理」里导入的校准(存在 routes.json 里) > 这里预置的。
    预置文件只是免去用户每张图都去游戏里抓一遍; 游戏更新后地图变了,
    用户重抓一次就覆盖了。
    """
    if not level:
        return None
    for d in (collected_dir(), bundled_dir()):
        p = os.path.join(d, f"{level}.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                calib = MapCalib.from_dict(json.load(f))
        except (OSError, ValueError) as exc:    # noqa: BLE001
            log.warning("读取预置校准失败 %s: %s", p, exc)
            continue
        if calib.valid:
            return calib
    return None
