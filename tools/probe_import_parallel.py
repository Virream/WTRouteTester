"""并行导入探针: 同一批回放, **串行 vs 按局并行** 的耗时与结果对比。

    python tools\probe_import_parallel.py <回放文件或分片文件夹> [更多...]

* 需要你**自己把路径写在命令行上** —— 探针**不会**去扫游戏目录(那是禁止的);
* 只读你给的文件, 结果写进系统临时目录, 测完就删, 不碰 `data\`;
* 局数不够时会把同一个文件**复制**几份凑数(复制到临时目录, 原件一个字节都不动) ——
  这样一局真回放也能看出多进程的加速比。

看什么:
  · 串行总耗时 / 并行总耗时 / 加速比(纯 Python 位流解码, 线程没用, 只能多进程);
  · 两条路径产出的条目是不是**一模一样**(地图键 / 线路数 / 总点数)。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import import_pool, route_store as rs          # noqa: E402
from wt81111g.replay_import import import_replay            # noqa: E402
from wt81111g.route_store import RouteStore                 # noqa: E402
from wt81111g.wrpl_reader import ImportCancelled            # noqa: E402

rs.SYNC_SAVE = True          # 探针要"写完立刻能读", 不要后台线程的合并窗口


def _fingerprint(store) -> list[tuple]:
    """每局一个 (地图键, 线路数, 总点数) —— 用来比对两条路径的产物。"""
    out = []
    for g in store.globals:
        pts = sum(lf.point_count for side in ("A", "B") for lf in g.lives(side))
        out.append((g.map_key, g.count(), pts))
    return sorted(out)


def _prepare(inputs: list[str], want: int) -> tuple[list[str], str]:
    """局数不够就把第一个输入复制几份, 返回 (路径表, 临时目录)。"""
    paths = list(inputs)
    tmp = tempfile.mkdtemp(prefix="wtrt-probe-")
    if len(paths) >= want or not inputs:
        return paths, tmp
    src = inputs[0]
    if os.path.isdir(src):
        print("(!) 只有一个分片文件夹, 无法靠复制凑数(复制整个目录太慢), 就按 1 局测")
        return paths, tmp
    k = 1
    while len(paths) < want:
        dst = os.path.join(tmp, f"copy{k}{os.path.splitext(src)[1]}")
        shutil.copy2(src, dst)
        paths.append(dst)
        k += 1
    print(f"(!) 只有 {len(inputs)} 个输入, 已复制 {k - 1} 份凑成 {len(paths)} 局")
    return paths, tmp


def main(argv: list[str]) -> int:
    inputs = [p for p in argv if os.path.exists(p)]
    if not inputs:
        print(__doc__)
        print("请把回放文件或分片文件夹的路径写在命令行上。")
        return 2
    paths, scratch = _prepare(inputs, 4)
    n = len(paths)
    print(f"共 {n} 局输入, 并行度上限 {import_pool.default_workers(n)}\n")

    ok = True
    serial_fp = parallel_fp = None

    # ---- 串行(原来的路径, 单进程逐局) ----
    sdir = tempfile.mkdtemp(prefix="wtrt-serial-", dir=scratch)
    sstore = RouteStore(os.path.join(sdir, "routes.json"))
    t0 = time.perf_counter()
    ser_ok = 0
    for i, path in enumerate(paths, 1):
        try:
            import_replay(path, sstore, expect_server=None)
            ser_ok += 1
        except ImportCancelled:
            break
        except Exception as exc:            # noqa: BLE001 - 探针里只报告不中断
            print(f"  串行第 {i} 局失败: {type(exc).__name__}: {exc}")
    serial_s = time.perf_counter() - t0
    serial_fp = _fingerprint(sstore)
    print(f"串行: 成功 {ser_ok}/{n} 局, 合计 {serial_s:.1f} s"
          f" (平均 {serial_s / max(1, ser_ok):.1f} s/局)")

    # ---- 并行(按局一个进程) ----
    pdir = tempfile.mkdtemp(prefix="wtrt-par-", dir=scratch)
    pstore = RouteStore(os.path.join(pdir, "routes.json"))
    t0 = time.perf_counter()
    done, failed, cancelled = import_pool.import_many(
        paths, pstore, expect_server=None)
    par_s = time.perf_counter() - t0
    parallel_fp = _fingerprint(pstore)
    print(f"并行: 成功 {len(done)}/{n} 局, 失败 {len(failed)}, 取消 {cancelled}, "
          f"合计 {par_s:.1f} s (平均 {par_s / max(1, len(done)):.1f} s/局)")
    for name, why in failed:
        print(f"  失败: {name} — {why}")

    if ser_ok and len(done):
        print(f"\n加速比: {serial_s / par_s:.2f}×  "
              f"(串行 {serial_s:.1f}s → 并行 {par_s:.1f}s)")
    if serial_fp != parallel_fp:
        ok = False
        print("\n!! 两条路径的产物不一致:")
        print(f"   串行 {serial_fp}")
        print(f"   并行 {parallel_fp}")
    elif ser_ok:
        print(f"产物一致: {len(parallel_fp)} 局, 共 "
              f"{sum(x[2] for x in parallel_fp):,} 个点")

    shutil.rmtree(scratch, ignore_errors=True)
    print("\n" + ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
