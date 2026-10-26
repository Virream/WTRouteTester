"""批量导入回放时的**按局并行**(用户 2026-10-26: "简单的能拆的就拆")。

**为什么按局拆**: 一局服务器回放要解 20~35 秒的位流, 全是纯 Python 计算 ——
GIL 把线程挡死了, 所以只有**多进程**才是真并行。按局又是最安全的粒度:
每一局自己读自己的文件、自己建自己的记录, 进程之间**不共享任何状态**。

**为什么单局内部不拆**: `wrpl_reader` 的 32 槽 delta 历史跨包、跨分片共享,
按分片拆开必然算错(P2 方案里明确排除)。

**怎么把结果搬回来**: 子进程**自己把点打包成 `.wts` 会话文件**, 只回报几十字节的
元数据(点数/玩家数/状态) —— 把几十万点 pickle 回主进程纯属搬砖, 一次就是几百 MB。
主进程用 `RouteStore.adopt_session()` 接管那个文件, **连打包都省了**(正好也是
`session_store` 拆成一局一文件的红利)。

⚠ **半成品必须写在 `routes.sessions` 目录里(后缀 `.wts.part`), 不能用系统临时目录**:
`os.replace` 不能跨盘 —— 临时目录默认在 C:, 而数据常在 D:/E: —— 实测直接
`WinError 17 “系统无法将文件移到不同的磁盘驱动器”`, 三局全部“写出来了但读不回来”。
同目录改名才是原子的。`.wts.part` 不以 `.wts` 结尾, `scan_dir` 不会把它当会话。

⚠ 打包版(exe)必须 `multiprocessing.freeze_support()`(见 `route_tester.py`
   `__main__` 的第一行), 否则每个子进程都会重新执行一遍 `main()` —— 开一堆窗口。

⚠ 只有**两局以上**才走这里: 单局用原来的串行路径, 能给出"分片 3/24"这种细进度,
   而且不用为一次导入付出 spawn 一个解释器的代价。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict

from . import session_store

log = logging.getLogger("import_pool")

# 同时解析几局。一局服务器回放光"解出来的对象"就占几百 MB, 所以不敢按核数开:
# 8 物理核 / 16 逻辑核的机器上取 4, 内存与收益平衡得最好。
MAX_WORKERS = 4


# ---------------------------------------------------------------- 子进程一侧
class _CalibView:
    """子进程里的最小校准查询(没有 `RouteStore`, 只有一张预展开的小表)。

    字段形状故意与 `RouteStore` 一致: `resolve_calib()` 只用到 `.calib_of()`。
    """
    __slots__ = ("_raw",)

    def __init__(self, raw: dict | None):
        self._raw = raw or {}

    def calib_of(self, map_key: str):
        from .map_calib import MapCalib

        raw = self._raw.get(map_key) if map_key else None
        if not isinstance(raw, dict):
            return None
        calib = MapCalib.from_dict(raw)
        return calib if calib.valid else None


def _worker_init(log_path: str) -> None:
    """每个工作进程启动时配一次日志。

    spawn 出来的解释器是**干净的**: 主进程 `logging.basicConfig(...)` 设的 handler
    一个都带不过来。不配的话解析过程中的告警会静默丢掉; 更难缠的是
    **打包版是 `--windowed`, `sys.stderr` 是 `None`** —— logging 保底的
    stderr 输出器会自己抛异常, 把一次“只是有点警告”的解析变成失败。
    多个进程追加同一个文件: 单行写入近似原子, 偶有交错可以接受。
    """
    root = logging.getLogger()
    if not log_path:
        root.addHandler(logging.NullHandler())
        return
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s[%(process)d]: %(message)s",
            filename=log_path, filemode="a", encoding="utf-8", force=True)
    except OSError:            # 日志文件打不开不该让解析失败
        root.addHandler(logging.NullHandler())


def parse_one(path: str, expect_server: bool | None, calibs: dict,
              out_dir: str, route_id: str) -> dict:
    """**在子进程里**解析一局 → 写成半成品 `.wts.part` → 回报元数据。失败也返回 dict。

    `out_dir` 必须是主进程的 `routes.sessions` 目录 —— 同目录才能原子改名(见模块说明)。
    返回 `{"ok": True, "session": 半成品路径, "route_id":..., "stats": {...}}`
    或 `{"ok": False, "error": "..."}`。
    """
    from .replay_import import build_record, resolve_calib
    from .route_store import _pack_global
    from .wrpl_reader import load_replay

    try:
        data = load_replay(path, progress=None, expect_server=expect_server)
        calib, note = resolve_calib(_CalibView(calibs), data.level)
        rec, stats = build_record(
            data, path if os.path.isfile(path) else "", calib=calib)
        # 条目 id 由**主进程**发号: 并行时两局可能落在同一毫秒, 各自取时间戳会撞。
        if route_id:
            rec.route_id = route_id
        header, blocks, tracks = _pack_global(rec)
        os.makedirs(out_dir, exist_ok=True)
        part = session_store.part_path(out_dir, rec.route_id)
        session_store.write_session(part, header, blocks, tracks)
        return {"ok": True, "session": part, "route_id": rec.route_id,
                "stats": asdict(stats), "calib_note": note}
    except BaseException as exc:   # noqa: BLE001 - 子进程里任何异常都要带回主进程
        log.warning("并行解析失败 %s: %s", path, exc, exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 主进程一侧
def default_workers(n: int) -> int:
    """并行度: 局数 / 半个 CPU 数 / `MAX_WORKERS` 三者取小。"""
    cpu = os.cpu_count() or 2
    return max(1, min(n, cpu // 2 or 1, MAX_WORKERS))


def collect_calibs(store) -> dict:
    """把「地图键 → 校准」预先展开成一张表, 发给子进程。

    子进程没有 `RouteStore`(它带线程、带锁, 本来也不该搬过去), 而回放导入要查
    "这张图有没有校准"。查表口径与 `RouteStore.calib_of()` 一致: 既认图片哈希键,
    也认回放关卡代号(`wrpl_<level>` → 用户给它关联的那张图的校准)。
    """
    from .map_calib import MAP_KEY_PREFIX

    out = dict(store.map_calib)
    for hkey, level in (getattr(store, "map_levels", None) or {}).items():
        raw = store.map_calib.get(hkey)
        if raw and level:
            out.setdefault(f"{MAP_KEY_PREFIX}{level}", raw)
    return out


def _new_route_id(store) -> str:
    """沿用「`wrpl` + 10 位毫秒时间戳」的样式, 并保证不与已有条目撞。"""
    ms = int(time.time() * 1000) % 10 ** 10
    for _ in range(2000):
        rid = f"wrpl{ms:010d}"
        taken = store.get_global(rid) is not None or os.path.exists(
            session_store.session_path(store.sessions_dir, rid))
        if not taken:
            return rid
        ms = (ms + 1) % 10 ** 10
    return f"wrpl{uuid.uuid4().hex[:10]}"      # 兜底: 理论上到不了这里


def import_many(paths: list[str], store, *, expect_server: bool | None,
                progress=None, log_path: str = "",
                workers: int | None = None
                ) -> tuple[list, list, bool]:
    """并行解析 `paths`(回放文件 / 分片文件夹), 每局生成一个全局条目。

    `progress(msg, frac)` 与串行路径同一套语义, **抛 `ImportCancelled` 即取消**。
    取消时**已经解析完的照样入库**(不让白跑的活浪费掉), 没开始的丢弃。

    返回 `(done, failed, cancelled)` —— `done` 是 `[(GlobalRecord, ImportStats)]`,
    与 `route_window._import_replay_many` 里串行分支的产物同形。
    """
    from .replay_import import ImportStats

    n = len(paths)
    total = max(1, workers or default_workers(n))
    # 半成品就写在 `routes.sessions\` 里: 与最终文件**同目录**才能原子改名,
    # 跨盘时 `os.replace` 会直接失败(踩过, 见模块说明)。
    out_dir = store.sessions_dir
    _cleanup_parts(out_dir)                 # 清掉上次取消/崩溃留下的半成品
    calibs = collect_calibs(store)
    done: list = []
    failed: list[tuple[str, str]] = []
    cancelled = False
    ex = ProcessPoolExecutor(max_workers=total, initializer=_worker_init,
                            initargs=(log_path,))
    futs: dict = {}
    parts: list[str] = []                   # 本次发出的半成品路径(取消时清理残留)
    try:
        for path in paths:
            rid = _new_route_id(store)
            parts.append(session_store.part_path(out_dir, rid))
            futs[ex.submit(parse_one, path, expect_server, calibs, out_dir, rid)] = path
        log.info("开始并行解析 %d 局回放(并行度 %d)", n, total)
        if progress is not None:
            # 进度条先动一下 + 报出并行度: 否则第一局要等半分钟, 看着像卡死了
            progress(f"正在并行解析 {n} 局(同时 {total} 局, 每局约半分钟)…", 0.01)
        for k, fut in enumerate(as_completed(futs), 1):
            path = futs[fut]
            name = os.path.basename(path.rstrip("\\/")) or path
            if progress is not None:
                # 每局跑完才报一次: 子进程内部的细分进度拿不到(那要多一条管道)
                progress(f"[{k}/{n}] 已完成 {name}", k / n)
            res = fut.result()
            if not res.get("ok"):
                failed.append((name, str(res.get("error") or "未知错误")))
                continue
            rec = store.adopt_session(res["session"])
            if rec is None:
                failed.append((name, "会话文件写出来了但接管失败 —— 详见日志 route_tester.log"))
                continue
            done.append((rec, ImportStats(**res["stats"])))
    except Exception as exc:   # noqa: BLE001 - 取消(ImportCancelled)或框架异常
        from .wrpl_reader import ImportCancelled

        if isinstance(exc, ImportCancelled):
            cancelled = True
        else:
            log.exception("并行导入中断")
            failed.append(("(并行导入)", f"{type(exc).__name__}: {exc}"))
    finally:
        # wait=False: 取消时**不能**等还在跑的那几局(一局几十秒, 界面会像死了一样)
        ex.shutdown(wait=False, cancel_futures=True)
        for part in parts:                  # 没被接管的半成品(取消/失败)当场删掉
            try:
                os.remove(part)
            except OSError:
                pass                        # 可能还在被别的进程写 → 下次开始时再清
    log.info("并行解析结束: 成功 %d, 失败 %d, 取消 %s", len(done), len(failed), cancelled)
    return done, failed, cancelled


def _cleanup_parts(dir_path: str) -> None:
    """删掉上次没来得及接管的半成品(`.wts.part`)。

    正常路径上它们会被 `adopt_session` 改名成正式文件、或者被 `finally` 删掉;
    只有在“取消时子进程还在写”这种情况下会残留, 所以每次导入前顺手扫一遍。
    """
    try:
        names = os.listdir(dir_path)
    except OSError:
        return
    for name in names:
        if name.endswith(session_store.PART_SUFFIX):
            try:
                os.remove(os.path.join(dir_path, name))
            except OSError:
                pass                        # 还在被写 → 留着, 下次再说

