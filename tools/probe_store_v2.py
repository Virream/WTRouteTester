# -*- coding: utf-8 -*-
"""存储层 v2 实测: 真实数据迁移 + 规模(启动/保存/加载)。

用法: python tools/probe_store_v2.py [--real]
    --real  额外拿 data/routes.json(用户真实数据)跑一遍迁移与对比
不依赖游戏与 8111。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wt81111g import session_store as ss           # noqa: E402
from wt81111g.route_store import (                 # noqa: E402
    LazyPoints, RoutePoint, RouteStore, TrackPoint, UnitLife,
)

fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
    if not cond:
        fails.append(label)


def timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


# ---------------------------------------------------------------- 真实数据迁移
def real_data(tmp: str) -> None:
    src = os.path.join(ROOT, "data", "routes.json")
    if not os.path.exists(src):
        print("跳过: 没有 data/routes.json")
        return
    dst = os.path.join(tmp, "real.json")
    shutil.copyfile(src, dst)
    size = os.path.getsize(dst)
    print(f"\n--- 真实数据迁移 ({size / 1e6:.2f} MB) ---")

    store, dt = timed(lambda: RouteStore(dst))
    print(f"  迁移耗时 {dt * 1000:.0f} ms")
    print(f"  单条录制 {len(store.routes)} 条 / 全局条目 {len(store.globals)} 个")
    n_lives = sum(g.count() for g in store.globals)
    n_pts = sum(r.n_pts for r in store.routes) + \
        sum(lf.n_pts for g in store.globals for side in ("A", "B") for lf in g.lives(side))
    print(f"  线路 {n_lives} 条 / 点 {n_pts:,} 个")
    check("旧文件留档", os.path.exists(dst + ".v1.bak"))
    with open(dst, encoding="utf-8") as fh:
        meta = json.load(fh)
    check("meta 是新格式且不含点", meta.get("layout") == "sessions-v1"
          and "routes" not in meta and "globals" not in meta)
    sess = os.listdir(store.sessions_dir)
    check("每局一个会话文件", len(sess) == len(store.routes) + len(store.globals),
          f"{len(sess)} 个文件")
    total = sum(os.path.getsize(os.path.join(store.sessions_dir, n)) for n in sess)
    print(f"  会话文件合计 {total / 1e6:.2f} MB (旧单文件 {size / 1e6:.2f} MB)")

    # 重新打开: 只读头部, 不读点
    store2, dt2 = timed(lambda: RouteStore(dst))
    print(f"  再次启动(只扫头部) {dt2 * 1000:.1f} ms")
    check("启动比迁移快得多", dt2 < max(0.05, dt * 0.2), f"{dt2 * 1000:.1f} vs {dt * 1000:.0f} ms")
    lazy = []
    for rec in [*store2.routes, *store2.globals]:
        if isinstance(rec, LazyPoints):
            lazy.append(rec)
        for side in ("A", "B"):
            for lf in getattr(rec, "lives", lambda _s: [])(side):
                lazy.append(lf.points)
    check("启动后所有点都是惰性的", lazy and not any(
        isinstance(p, LazyPoints) and p.loaded for p in lazy),
        f"{len(lazy)} 个点列表")

    # 改一个地图名 → 只写 meta
    key = next(iter(store2.map_names), "") or (store2.known_maps[0] if store2.known_maps else "")
    if key:
        _, dt3 = timed(lambda: (store2.set_map_hidden(key, True), store2.save_now()))
        print(f"  改地图级数据 + 落盘 {dt3 * 1000:.1f} ms")
        check("地图级改动是毫秒级", dt3 < 0.05, f"{dt3 * 1000:.1f} ms")

    # 读一局(把点真正解出来)并和旧文件里的原始值对数
    with open(dst + ".v1.bak", "rb") as fh:
        blob = fh.read()
    if blob[:2] == b"\x1f\x8b":
        import gzip
        blob = gzip.decompress(blob)
    legacy = json.loads(blob.decode("utf-8"))
    bad = 0
    checked = 0
    for item in legacy.get("globals") or []:
        rec = store2.get_global(item.get("route_id") or "")
        if rec is None:
            bad += 1
            continue
        for side in ("A", "B"):
            for old, new in zip(item.get("sides", {}).get(side) or [],
                                rec.lives(side)):
                for row, pt in zip(old.get("pts") or [], new.points):
                    checked += 1
                    # 旧格式: [t, x, y, speed(, hold)]; 新: float32
                    if abs(pt.t - row[0]) > 1e-4 or abs(pt.x - row[1]) > 1e-6 \
                            or abs(pt.y - row[2]) > 1e-6 or abs(pt.speed - row[3]) > 1e-3:
                        bad += 1
    print(f"  逐点核对 {checked:,} 个点")
    check("点值与旧文件一致", bad == 0 and checked > 0, f"{bad} 处不一致")

    # 一局的读取耗时
    if store2.globals:
        g = max(store2.globals, key=lambda x: x.count())
        _, dt4 = timed(lambda: [len(lf.points) for side in ("A", "B") for lf in g.lives(side)])
        print(f"  读出一局({g.count()} 条线)并构造对象 {dt4 * 1000:.0f} ms")
    # 重写一局
    if store2.globals:
        g = max(store2.globals, key=lambda x: x.count())
        for side in ("A", "B"):
            for lf in g.lives(side):
                len(lf.points)                # 全部加载
        _, dt5 = timed(lambda: store2._write_session(g))
        print(f"  重写这一局 {dt5 * 1000:.0f} ms")
        check("写一局比写全量快", dt5 < 2.0, f"{dt5 * 1000:.0f} ms")


# ------------------------------------------------------------------ 500 局规模
def scale(tmp: str, n_sessions: int = 500) -> None:
    print(f"\n--- 规模: {n_sessions} 局 ---")
    path = os.path.join(tmp, "scale.json")
    store = RouteStore(path)
    # 每局 200 条线 × 4000 点 ≈ 80 万点(与一局服务器回放同量级)
    pts = [TrackPoint(round(0.1 + i * 1e-5, 5), round(0.2 + i * 1e-5, 5),
                      round(i * 0.04, 2), 30.0 + (i % 40)) for i in range(4000)]
    t0 = time.perf_counter()
    for i in range(n_sessions):
        g = store.new_global(f"MAP{i % 8}", f"地图 {i % 8}", zone_count=3)
        for side in ("A", "B"):
            for k in range(100):
                g.sides[side].append(UnitLife(
                    life_id=f"{g.route_id}:{side}{k}", icon="T",
                    obj_type="ground_model", side=side, points=pts))
        store.globals.append(g)
        store._dirty_ids.add(g.route_id)
    store.save_now()
    dt = time.perf_counter() - t0
    total = sum(os.path.getsize(os.path.join(store.sessions_dir, n))
                for n in os.listdir(store.sessions_dir))
    n_pts = n_sessions * 200 * 4000
    print(f"  写入 {n_sessions} 局 / {n_pts:,} 点: {dt:.1f} s "
          f"({dt / n_sessions * 1000:.0f} ms/局), 磁盘 {total / 1e9:.2f} GB")
    check("每局写入在百毫秒级", dt / n_sessions < 1.0, f"{dt / n_sessions * 1000:.0f} ms")

    again, dt_load = timed(lambda: RouteStore(path))
    print(f"  启动(只扫 {n_sessions} 个头部) {dt_load * 1000:.0f} ms")
    check("500 局启动 < 500 ms", dt_load < 0.5, f"{dt_load * 1000:.0f} ms")
    check("记录数对得上", len(again.globals) == n_sessions)

    _, dt_meta = timed(lambda: (again.set_map_hidden("MAP0", True), again.save_now()))
    print(f"  改地图级数据 + 落盘 {dt_meta * 1000:.1f} ms")
    check("改地图名不再等全量", dt_meta < 0.3, f"{dt_meta * 1000:.1f} ms")

    g = again.globals[0]
    _, dt_one = timed(lambda: [len(lf.points) for side in ("A", "B") for lf in g.lives(side)])
    print(f"  打开一局(80 万点解成对象) {dt_one * 1000:.0f} ms")

    # 删一局
    _, dt_needs = timed(lambda: [r for r in again.globals if again._needs_write(r)])
    print(f"     （其中 _needs_write 扫 {len(again.globals)} 局 {dt_needs * 1000:.1f} ms）")
    _, dt_rm = timed(lambda: again.remove_global(g.route_id))
    _, dt_meta = timed(lambda: again.save_now())
    print(f"  删一局: remove {dt_rm * 1000:.1f} ms + 落盘 {dt_meta * 1000:.1f} ms")
    check("删一局不再重写全部", dt_rm + dt_meta < 0.3,
          f"{(dt_rm + dt_meta) * 1000:.1f} ms")


def roundtrip(tmp: str) -> None:
    print("\n--- 单个会话往返(含 hold / 朝向)---")
    path = os.path.join(tmp, "rt.json")
    store = RouteStore(path)
    rec = store.new_route("M", "地图", "T", 0.5, 0.5)
    rec.points = [RoutePoint(0.5, 0.5, 1.25, 12.5, 0.6, -0.8),
                  RoutePoint(0.51, 0.49, 2.5, 45.0, 1.0, 0.0)]
    store.add(rec)
    g = store.new_global("M", "地图")
    g.sides["A"].append(UnitLife(
        life_id=f"{g.route_id}:A1", icon="T", side="A",
        points=[TrackPoint(0.2, 0.3, 0.0, 0.0),
                TrackPoint(0.2, 0.3, 10.0, 0.0, 0.0, 0.0, 60.0)]))
    store.add_global(g)
    store.save_now()

    again = RouteStore(path)
    r = again.get(rec.route_id)
    check("朝向 dx/dy 存住了", abs(r.points[0].dx - 0.6) < 1e-6
          and abs(r.points[0].dy + 0.8) < 1e-6, f"{r.points[0].dx},{r.points[0].dy}")
    lf = again.get_global(g.route_id).lives("A")[0]
    check("hold 存住了", abs(lf.points[-1].hold - 60.0) < 1e-3, str(lf.points[-1].hold))
    check("t1 = 末点 + hold", abs(lf.t1 - 70.0) < 1e-3, str(lf.t1))
    check("未加载时 t1 用头部元数据", abs(lf.t1_hint - 70.0) < 1e-3, str(lf.t1_hint))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="probe_v2_")
    try:
        roundtrip(tmp)
        if "--real" in sys.argv:
            real_data(tmp)
        scale(tmp, int(os.environ.get("PROBE_SESSIONS", "500")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if fails:
        print("失败:", " / ".join(fails))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
