r"""点数据加载速度分解: 一局几十万点, 时间到底花在哪一步。

    python tools\probe_points_speed.py                 # 扫 dist\WTRouteTester\data
    python tools\probe_points_speed.py --dir <会话目录>

只读**软件自己录下的会话文件**(`routes.sessions\*.wts`), 不碰游戏目录。
挑点数最多的那一局(通常就是服务器回放), 把"打开一局"拆成四段计时:

  1. `read_block`      —— 读文件 + zstd 解压(每条线一个独立帧)
  2. 列展开            —— `array.frombytes`(纯 C, 不解对象)
  3. 对象构造          —— `unpack_points` 逐点造 `TrackPoint`(现在是这一步最贵)
  4. numpy 视图        —— `np.frombuffer` + reshape(第 3 步的替代方案)

这组数字决定 P3 怎么改: 如果 3 占大头, 就该让点以**列数组**形式常驻,
用到某个点时再临时构造(`TrackPoint` 只在绘制/悬停里出现)。
"""
import glob
import os
import sys
import time
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import session_store as ss          # noqa: E402
from wt81111g.route_store import RoutePoint, TrackPoint   # noqa: E402

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "dist", "WTRouteTester", "data", "routes.sessions")


def _pick(dir_path: str):
    """挑点数最多的那一局, 返回 (head, path, tracks, 总点数)。

    按**文件大小**挑 —— 头部的 `nl` 是线路条数不是点数(用它会挑中
    “线路少但点很多”的单条录制), 而点块紧跟在头/清单后面, 文件大就是点多。
    """
    best = None
    for head, path in ss.scan_dir(dir_path):
        size = os.path.getsize(path)
        if best is None or size > best[0]:
            best = (size, head, path)
    if best is None:
        return None, None, [], 0
    _size, head, path = best
    tracks = ss.read_tracks(path, head)
    if not tracks:                       # 单条录制: 点元数据在头部里
        entry = head.get("pts")
        tracks = [entry] if isinstance(entry, dict) else []
    total = sum(int(t.get("n") or 0) for t in tracks)
    return head, path, tracks, total


def _timed(label: str, fn):
    t0 = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t0
    print(f"  {label:<28} {dt * 1000:8.1f} ms")
    return out, dt


def main(argv: list[str]) -> int:
    dir_path = DEFAULT_DIR
    if "--dir" in argv:
        dir_path = argv[argv.index("--dir") + 1]
    if not os.path.isdir(dir_path):
        print(f"找不到会话目录: {dir_path}")
        return 2
    head, path, tracks, total = _pick(dir_path)
    if not path:
        print(f"{dir_path} 里没有会话文件")
        return 2
    print(f"文件: {os.path.basename(path)}  ({os.path.getsize(path):,} 字节)")
    print(f"线路: {len(tracks)} 条, 点数: {total:,}\n")

    dl = int(head.get("_dir_len") or 0)
    per_100k = lambda dt: dt / max(1, total) * 100_000      # noqa: E731

    # 1) 读 + 解压
    raws, t_decomp = _timed("1. read_block(读+zstd 解压)", lambda: [
        ss.read_block(path, int(t.get("off") or 0), int(t.get("clen") or 0),
                      int(t.get("raw") or 0), dl) or b"" for t in tracks])

    # 2) 列展开(纯 C)
    def expand_cols():
        for t, raw in zip(tracks, raws):
            n = int(t.get("n") or 0)
            if not n or not raw:
                continue
            arr = array("f")
            arr.frombytes(raw[:4 * n])
        return None
    _timed("2. 列展开(array.frombytes)", expand_cols)

    # 3) 逐点造对象 —— 现在 `LazyPoints` 走的就是这条路
    def make_objects():
        out = []
        for t, raw in zip(tracks, raws):
            fmt = int(t.get("fmt") or ss.FMT_BASE)
            if t is tracks[0] and head.get("kind") != "global":
                fac = lambda a, b, c, d, e, f, _h: RoutePoint(b, c, a, d, e, f)  # noqa: E731
            else:
                fac = lambda a, b, c, d, _e, _f, h: TrackPoint(b, c, a, d, 0.0, 0.0, h)  # noqa: E731
            out.extend(ss.unpack_points(raw, fmt=fmt, factory=fac))
        return out
    objs, t_obj = _timed("3. 逐点造对象(unpack_points)", make_objects)

    # 4) numpy 视图(第 3 步的替代方案)
    try:
        import numpy as np
    except Exception:            # noqa: BLE001
        np = None
    if np is not None:
        def np_views():
            out = []
            for t, raw in zip(tracks, raws):
                n = int(t.get("n") or 0)
                if not n or not raw:
                    out.append(None)
                    continue
                fmt = int(t.get("fmt") or ss.FMT_BASE)
                ncol = 4 + (2 if fmt & ss.FMT_DIR else 0) + (1 if fmt & ss.FMT_HOLD else 0)
                out.append(np.frombuffer(raw, dtype=np.float32).reshape(ncol, n))
            return out
        _timed("4. numpy 视图(frombuffer)", np_views)

    print(f"\n每 10 万点: 解压 {per_100k(t_decomp) * 1000:.0f} ms, "
          f"造对象 {per_100k(t_obj) * 1000:.0f} ms")
    print(f"对象个数: {len(objs):,}, 单个约 "
          f"{objs[0].__sizeof__() if objs else 0} 字节 + list 指针 8")
    if np is not None:
        print(f"→ 造对象这一步换成 numpy 视图, 就只剩解压那点开销")
        print(f"   （按此比例, 80 万点造对象约 {per_100k(t_obj) * 8:.2f} s）")

    # 顺带量一下回放每帧要做的事: 二分找点
    if objs:
        pts = objs if head.get("kind") != "global" else objs
        def scan():
            for _ in range(20):
                lo, hi = 0, len(pts) - 1
                while lo < hi:
                    mid = (lo + hi) // 2
                    if pts[mid].t < 1.0:
                        lo = mid + 1
                    else:
                        hi = mid
        _timed("5. 二分查点 ×20", scan)

    _end_to_end(path, tracks)
    return 0


def _end_to_end(src_path: str, tracks: list) -> None:
    """端到端: 把这一局装进一个临时 store, 量“第一次画出来”要花多久。

    对比两条路: **列存储**(现在的实现) vs **先物化成点对象**(P3 之前的做法)。
    """
    import shutil
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from wt81111g.route_map_view import RouteMapView
    from wt81111g.route_store import RouteStore

    tmp = tempfile.mkdtemp(prefix="wtrt-speed-")
    try:
        probe_path = os.path.join(tmp, "r.json")
        store = RouteStore(probe_path)
        os.makedirs(store.sessions_dir, exist_ok=True)
        dst = os.path.join(store.sessions_dir, os.path.basename(src_path))
        shutil.copy2(src_path, dst)
        store = RouteStore(probe_path)
        recs = [g for g in store.globals] + [r for r in store.routes]
        if not recs:
            print("  (这一局读不回来, 跳过端到端)")
            return
        app = QApplication.instance() or QApplication([])      # noqa: F841
        view = RouteMapView()

        def lines(rec):
            if hasattr(rec, "sides"):
                out = []
                for side in ("A", "B"):
                    out.extend(rec.lives(side))
                return out
            return [rec]

        def paint_prep():
            for rec in recs:
                for lf in lines(rec):
                    if not len(lf.points):
                        continue
                    vmax = view._vmax(lf)
                    view._times(lf)
                    view._bbox(lf)
                    view._px_line(lf)
                    view._speed_segments(lf, vmax)
            return None

        print("\n端到端(第一次画出来, 含解压): ")
        view._vmax_cache.clear(); view._times_cache.clear()
        view._bbox_cache.clear(); view._line_cache.clear(); view._seg_cache.clear()
        _timed("列存储(现在的实现)", paint_prep)

        # 先全部物化, 再算一遍 —— 这正是 P3 之前的路径
        for rec in recs:
            for lf in lines(rec):
                list(lf.points)
        view._vmax_cache.clear(); view._times_cache.clear()
        view._bbox_cache.clear(); view._line_cache.clear(); view._seg_cache.clear()
        _timed("先物化成点对象(旧行为)", paint_prep)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
