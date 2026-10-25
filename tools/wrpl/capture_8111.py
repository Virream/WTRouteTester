"""抓取一局对局的 8111 数据, 用来校准「回放里的世界坐标 → 地图图片」。

为什么需要它(2026-10-25 结论):
  * 回放里 `cz_circle_*` 只是**占位变换**, 两张不同地图都是 (1832, 5, 2456) ——
    战区中心拿不到;
  * 坦克重生点 / 各机型重生点在回放里**只有名字没有坐标**;
  * 地图图片的对齐基准(`map_min` / `map_max`)只有 8111 给。
  所以进一局就能把这些一次性抓下来, 比录一整局简单得多。

抓的东西(全部原样落盘, 方便事后分析):
  map_info.json      地图外框(世界米) —— 对齐的基准
  map_obj.jsonl      每轮一条: 战区 + 所有 respawn_base_* + 玩家自身位置
  mission.json       任务信息
  indicators.json    仪表(含当前载具内部名)
  map.img            地图图片原始字节(PNG/JPEG)
  summary.txt        人类可读的汇总(战区/重生点按种类分组 + 去重坐标)

用法:
    # 已经在对局里:
    python tools/wrpl/capture_8111.py

    # 还没开: 先启动它, 它会在战局开始时自动开始抓
    python tools/wrpl/capture_8111.py --wait

    # 指定输出目录 / 时长
    python tools/wrpl/capture_8111.py --seconds 40 --out "回放文件\\8111抓取\\finland_dom"
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from wt81111g.api8111 import WT8111                              # noqa: E402


def _fmt_xy(o: dict) -> str:
    try:
        return f"({float(o.get('x')):.4f}, {float(o.get('y')):.4f})"
    except (TypeError, ValueError):
        return "(?)"


def _kind_of(o: dict) -> str:
    """对象种类: respawn_base_xxx 取 xxx; 其它取 type。"""
    for f in ("type", "icon"):
        raw = str(o.get(f) or "").strip().lower()
        if raw.startswith("respawn_base_"):
            return raw[len("respawn_base_"):]
        if raw:
            return raw
    return "?"


def _px(o: dict, info: dict) -> str:
    """把 0~1 归一化坐标换算成世界米(需要 map_min/map_max)。"""
    try:
        lo = [float(v) for v in (info.get("map_min") or [])]
        hi = [float(v) for v in (info.get("map_max") or [])]
        if len(lo) < 2 or len(hi) < 2:
            return ""
        x = lo[0] + float(o.get("x")) * (hi[0] - lo[0])
        z = hi[1] - float(o.get("y")) * (hi[1] - lo[1])
        return f"世界({x:9.1f},{z:9.1f})"
    except (TypeError, ValueError):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 8111 的对局数据(战区/重生点/地图外框)")
    ap.add_argument("--out", default="", help="输出目录(默认 回放文件/8111抓取/<时间戳>)")
    ap.add_argument("--seconds", type=float, default=30.0, help="抓多少秒")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔(秒)")
    ap.add_argument("--wait", action="store_true", help="先等战局开始(最多 5 分钟)")
    args = ap.parse_args()

    out = args.out or os.path.join(
        "回放文件", "8111抓取", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)

    cli = WT8111()
    if not cli.connected():
        print("❌ 连不上 8111 —— 先启动游戏。", flush=True)
        return 2

    if args.wait:
        print("等待战局开始(map_info.valid=true) …", flush=True)
        t0 = time.time()
        while time.time() - t0 < 300:
            info = cli.map_info()
            if info.get("valid"):
                break
            time.sleep(1.0)
        else:
            print("❌ 5 分钟内没等到战局。", flush=True)
            return 2
        print("✅ 战局已开始。", flush=True)

    print(f"输出目录: {os.path.abspath(out)}", flush=True)
    print(f"抓取 {args.seconds:.0f} 秒, 每 {args.interval:.1f} 秒一轮 …\n", flush=True)

    info = cli.map_info()
    with open(os.path.join(out, "map_info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    if not info.get("valid"):
        print("⚠ map_info.valid=false —— 现在不在战局里, 数据可能没用。", flush=True)
    print("map_info:")
    for k in ("valid", "map_min", "map_max", "grid_size", "grid_zero"):
        if k in info:
            print(f"  {k}: {info[k]}")
    print()

    for name, fn in (("mission.json", cli.mission),
                     ("indicators.json", cli.indicators)):
        try:
            d = fn()
        except Exception:                        # noqa: BLE001 - 抓取不该中断
            d = {}
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    # 地图图片(只要真正的图片, 机库占位图会被 map_image 过滤掉)
    img = cli.map_image()
    if img:
        ext = ".png" if img[:4] == b"\x89PNG" else ".jpg"
        with open(os.path.join(out, "map" + ext), "wb") as f:
            f.write(img)
        print(f"地图图片: map{ext}  {len(img) / 1024:.0f} KB")
    else:
        print("⚠ 没拿到地图图片(可能不在战局里)")

    rounds = 0
    seen_kinds: collections.Counter = collections.Counter()
    zones: dict[str, dict] = {}
    spawns: dict[tuple, dict] = {}
    t_end = time.time() + args.seconds
    with open(os.path.join(out, "map_obj.jsonl"), "w", encoding="utf-8") as fh:
        while time.time() < t_end:
            objs = cli.map_objects()
            rec = {"t": round(time.time(), 3), "objects": objs}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            rounds += 1
            for o in objs:
                if not isinstance(o, dict):
                    continue
                kind = _kind_of(o)
                seen_kinds[kind] += 1
                if kind == "capture_zone":
                    # ⚠ 不能用 icon 当键 —— 三个战区都叫 `capture_zone`, 会被并成一个。
                    # 8111 给的是 `zone_label`(A/B/C) 与 `zone_id`。
                    label = str(o.get("zone_label") or o.get("label")
                                or o.get("zone_id") or "?")
                    zones.setdefault(label, o)
                    zones[label].update(o)     # 保留最后一轮(含归属色/blink)
                elif kind not in ("player", "?"):
                    try:
                        key = (kind, round(float(o.get("x")), 4),
                               round(float(o.get("y")), 4))
                    except (TypeError, ValueError):
                        # 没有坐标的(如 respawn_base_airfield) 按颜色区分
                        key = (kind, "-", str(o.get("color")))
                    spawns.setdefault(key, o)
            print(f"  第 {rounds} 轮: {len(objs)} 个对象", flush=True)
            time.sleep(args.interval)

    # ---- 汇总
    lines: list[str] = []
    lines.append("=== 战局概览 ===")
    lines.append(f"map_min {info.get('map_min')}   map_max {info.get('map_max')}")
    gs, gz = info.get("grid_size"), info.get("grid_zero")
    lines.append(f"grid_size {gs}   grid_zero {gz}")
    try:
        lo = [float(v) for v in info.get("map_min") or []]
        hi = [float(v) for v in info.get("map_max") or []]
        lines.append(f"外框跨度: {hi[0] - lo[0]:.0f} × {hi[1] - lo[1]:.0f} m")
    except (TypeError, ValueError, IndexError):
        pass
    lines.append("")

    lines.append(f"=== 战区 {len(zones)} 个 ===")
    for label in sorted(zones):
        o = zones[label]
        lines.append(f"  {label:<4s} zone_id={o.get('zone_id')} "
                     f"color={o.get('color')} blink={o.get('blink')} "
                     f"归一化 {_fmt_xy(o)}  {_px(o, info)}")
    lines.append("")

    grp: dict[str, list[dict]] = collections.defaultdict(list)
    for (kind, _x, _y), o in spawns.items():
        grp[kind].append(o)
    lines.append("=== 重生点(按种类分组) ===")
    for kind in sorted(grp):
        items = sorted(grp[kind], key=lambda o: (o.get("color") or "",
                                                 float(o.get("x", 0))))
        lines.append(f"  [{kind}] {len(items)} 个")
        by_color = collections.Counter(str(o.get("color")) for o in items)
        lines.append(f"      颜色分布: {dict(by_color)}")
        for o in items[:6]:
            lines.append(f"      color={o.get('color')} 归一化 {_fmt_xy(o)}  "
                         f"{_px(o, info)}")
        if len(items) > 6:
            lines.append(f"      …(其余 {len(items) - 6} 个见 map_obj.jsonl)")
    lines.append("")

    lines.append("=== 所有对象种类(按出现次数) ===")
    for k, n in seen_kinds.most_common():
        lines.append(f"  {k:<24s} {n}")

    text = "\n".join(lines)
    print("\n" + text, flush=True)
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n✅ 已写入 {os.path.abspath(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
