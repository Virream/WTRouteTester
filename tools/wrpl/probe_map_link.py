"""探针: 验证「图片哈希 ↔ 回放关卡代号」的关联机制(用户 2026-10-26 的规格)。

模拟用户描述的场景并逐条核对:

    1. 解析服务器录像 → 得到 `avg_finland`(没有底图, 没有校准)
    2. 在同一张图上用 8111 录过一条 → 得到图片哈希 `c59f768c0fde`(有图有校准)
    3. 此时侧栏是**两张地图**: 一张写 avg_finland, 一张写「地图 N」
    4. 到「地图管理」把哈希那条的「可选」选成 avg_finland
    5. 关窗后: 两者归成一张图, 显示名用用户起的名字, 校准用那张图的
    6. 选错了(填成 avg_netherlands)要能改回来, 数据一行都不能少

跑法:

    cmd /c "set PYTHONIOENCODING=utf-8&& set PYTHONPATH=d:\\course\\VSCODE\\WT81111G&&
            set QT_QPA_PLATFORM=offscreen&& .venv\\Scripts\\python.exe
            tools\\wrpl\\probe_map_link.py"
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PyQt6.QtCore import Qt                            # noqa: E402
from PyQt6.QtGui import QColor, QImage                 # noqa: E402
from PyQt6.QtWidgets import QApplication               # noqa: E402

from wt81111g.map_cache import MapCache                # noqa: E402
from wt81111g.map_calib import MapCalib                # noqa: E402
from wt81111g.map_manager import COL_KEY, COL_LEVEL, COL_NAME, MapManagerDialog  # noqa: E402
from wt81111g.route_store import RouteStore            # noqa: E402

HASH_KEY = "c59f768c0fde"
LEVEL = "avg_finland"
WRONG_LEVEL = "avg_netherlands"
REPLAY_KEY = f"wrpl_{LEVEL}"

fails: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
    if not cond:
        fails.append(label)


def make_png(color: str = "#2f6f3f") -> bytes:
    """造一张能解码的 PNG(内容哈希才会稳定)。"""
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    tmp = os.path.join(tempfile.gettempdir(), "_probe_map_link.png")
    img.save(tmp, "PNG")
    with open(tmp, "rb") as f:
        return f.read()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841
    root = tempfile.mkdtemp(prefix="probe_map_link_")
    store = RouteStore(os.path.join(root, "routes.json"))
    cache = MapCache(os.path.join(root, "maps"))

    # ---- 场景搭建: 回放那条(无图) + 8111 那条(有图有校准) ----------------
    replay = store.new_global(map_key=REPLAY_KEY, map_name=LEVEL,
                              zone_count=3, mode_name="战区控制")
    replay.created_at = "2026-10-24T22:45:13"
    store.add_global(replay)

    png = make_png()
    put_ok = cache.put(HASH_KEY, png)
    store.remember_map(HASH_KEY)
    calib = MapCalib(min_x=0.0, max_x=2048.0, min_z=0.0, max_z=2048.0,
                     img_w=2048, img_h=2048, source="probe")
    store.set_calib(HASH_KEY, calib)

    print("\n--- 1) 关联之前 ---")
    keys = store.ordered_map_keys()
    check("侧栏是两张地图", len(keys) == 2, f"keys={keys}")
    check("回放那条有名字", store.display_name(REPLAY_KEY) == LEVEL,
          f"={store.display_name(REPLAY_KEY)!r}")
    check("哈希那条用自动编号", store.display_name(HASH_KEY).startswith("地图"),
          f"={store.display_name(HASH_KEY)!r}")
    check("回放那条拿不到校准", store.calib_of(REPLAY_KEY) is None)
    check("底图放在哈希键上", put_ok and cache.has(HASH_KEY))

    print("\n--- 2) 用户给哈希那条起名「芬兰」 + 关联 avg_finland ---")
    store.rename_map(HASH_KEY, "芬兰")
    changed = store.set_map_level(HASH_KEY, LEVEL)
    check("关联生效", changed)
    check("关卡代号记下了", store.level_of_hash(HASH_KEY) == LEVEL,
          f"={store.level_of_hash(HASH_KEY)!r}")
    check("反查得到哈希", store.hash_of_level(LEVEL) == HASH_KEY)
    check("canon 归一到哈希", store.canon(REPLAY_KEY) == HASH_KEY,
          f"canon({REPLAY_KEY})={store.canon(REPLAY_KEY)}")

    keys = store.ordered_map_keys()
    check("只剩一张地图", keys == [HASH_KEY], f"keys={keys}")
    check("显示名 = 用户起的「芬兰」", store.display_name(HASH_KEY) == "芬兰",
          f"={store.display_name(HASH_KEY)!r}")
    check("回放那条的显示名也跟着变",
          store.display_name(REPLAY_KEY) == "芬兰",
          f"={store.display_name(REPLAY_KEY)!r}")

    bar = store.calib_of(REPLAY_KEY)
    check("回放身份现在查得到校准", bar is not None,
          f"={bar.describe() if bar else None}")
    check("校准就是哈希那份",
          bar is not None and abs(bar.max_x - 2048.0) < 1e-6)

    groups = store.globals_by_mode()
    check("回放记录归到哈希这张图下", any(k[0] == HASH_KEY for k in groups),
          f"keys={list(groups)}")
    check("回放记录原 map_key 没被改动", replay.map_key == REPLAY_KEY,
          f"={replay.map_key!r}")

    print("\n--- 3) 界面: 左下键值对 + 右下预览 ---")
    dlg = MapManagerDialog(store, cache)
    check("列表只有一行", dlg.table.rowCount() == 1, f"rows={dlg.table.rowCount()}")
    key_item = dlg.table.item(0, COL_KEY)
    check("键列 = 自动编号_哈希",
          key_item is not None and key_item.text().endswith(HASH_KEY),
          f"={key_item.text() if key_item else None!r}")
    edit = dlg.table.cellWidget(0, COL_NAME)
    check("值列是个输入框", edit is not None, f"type={type(edit).__name__}")
    check("输入框里是当前名字", edit is not None and edit.text() == "芬兰",
          f"={edit.text() if edit else None!r}")
    combo = dlg.table.cellWidget(0, COL_LEVEL)
    check("可选列是个下拉框", combo is not None, f"type={type(combo).__name__}")
    check("下拉框选中 avg_finland",
          combo is not None and combo.currentData() == LEVEL,
          f"={combo.currentData() if combo else None!r}")
    combo_items = [combo.itemData(i) for i in range(combo.count())] if combo else []
    check("下拉框 = 「未选择」+ 软件见过的关卡代号",
          combo_items[:1] == [""] and LEVEL in combo_items,
          f"items={combo_items}")
    check("校准时按钮已可用(有选中行)", dlg.btn_calib.isEnabled())
    check("预览已装上底图", dlg._preview_pm is not None)
    dlg.close()

    print("\n--- 4) 选错: 改成 avg_netherlands ---")
    store.remember_map(f"wrpl_{WRONG_LEVEL}")     # 那个关卡得先存在(软件见过)
    wrong = store.new_global(map_key=f"wrpl_{WRONG_LEVEL}",
                             map_name=WRONG_LEVEL, zone_count=1,
                             mode_name="战区争夺")
    store.add_global(wrong)
    store.set_map_level(HASH_KEY, WRONG_LEVEL)
    check("改了之后关联的是错的那个",
          store.level_of_hash(HASH_KEY) == WRONG_LEVEL)
    keys = store.ordered_map_keys()
    check("avg_finland 又独立出来了一行", REPLAY_KEY in keys, f"keys={keys}")
    check("哈希那张图还在", HASH_KEY in keys, f"keys={keys}")
    check("回放记录没被搬走", replay.map_key == REPLAY_KEY,
          f"={replay.map_key!r}")
    check("回放记录仍属于原来那张图",
          store.canon(replay.map_key) == REPLAY_KEY)

    print("\n--- 5) 改回来: avg_finland ---")
    store.set_map_level(HASH_KEY, LEVEL)
    check("回放记录又归到哈希那张图", store.canon(replay.map_key) == HASH_KEY)
    check("哈希那张图只算一张(不重复)",
          store.ordered_map_keys().count(HASH_KEY) == 1,
          f"keys={store.ordered_map_keys()}")
    check("刚错选的那个关卡独立成行(数据没丢)",
          f"wrpl_{WRONG_LEVEL}" in store.ordered_map_keys(),
          f"keys={store.ordered_map_keys()}")
    check("错选的那张图数据完好",
          store.canon(wrong.map_key) == f"wrpl_{WRONG_LEVEL}",
          f"={wrong.map_key!r}")

    print("\n--- 6) 改回「未选择」= 完全解除 ---")
    store.set_map_level(HASH_KEY, "")
    check("关联已清除", store.level_of_hash(HASH_KEY) == "")
    check("又变回多张地图", len(store.ordered_map_keys()) == 3,
          f"keys={store.ordered_map_keys()}")
    check("回放那条退回自己的名字", store.display_name(REPLAY_KEY) == LEVEL,
          f"={store.display_name(REPLAY_KEY)!r}")
    check("回放那条又拿不到校准", store.calib_of(REPLAY_KEY) is None)

    print("\n--- 7) 重启后依然记得(落盘 + 重读) ---")
    store.set_map_level(HASH_KEY, LEVEL)
    store.save()
    again = RouteStore(os.path.join(root, "routes.json"))
    check("关联从盘上读回来", again.level_of_hash(HASH_KEY) == LEVEL,
          f"={again.level_of_hash(HASH_KEY)!r}")
    check("一键归并仍然成立", again.canon(REPLAY_KEY) == HASH_KEY)
    check("记录还在", any(g.map_key == REPLAY_KEY for g in again.globals))
    check("校准还在", again.calib_of(REPLAY_KEY) is not None)

    print("\n--- 8) 在界面里关联 → 已导入的回放线路自动按新校准重算 ---")
    from wt81111g.route_store import TrackPoint, UnitLife

    # 造一条"当初还没有校准、按估算范围归一化"的线路
    old_rect = [-3000.0, 3000.0, -3000.0, 3000.0]
    life = UnitLife(life_id="probe:1", map_key=REPLAY_KEY)
    life.points.append(TrackPoint(x=0.5, y=0.5, t=1.0))   # 旧矩形中心 = 世界 (0, 0)
    replay.sides["A"].append(life)
    replay.norm_rect = list(old_rect)
    store.set_map_level(HASH_KEY, "")          # 先退回"未关联"
    store.save()

    dlg2 = MapManagerDialog(store, cache)
    row2 = next(r for r in range(dlg2.table.rowCount())
                if dlg2.table.item(r, COL_KEY).data(Qt.ItemDataRole.UserRole) == HASH_KEY)
    combo2 = dlg2.table.cellWidget(row2, COL_LEVEL)
    before = dlg2.calib_changed
    combo2.setCurrentIndex(combo2.findData(LEVEL))     # = 用户在下拉框里选中关卡代号
    app.processEvents()

    check("关联生效", store.level_of_hash(HASH_KEY) == LEVEL)
    check("记录的归一化矩形已换成校准矩形",
          replay.norm_rect == [0.0, 2048.0, 0.0, 2048.0], f"={replay.norm_rect}")
    px = life.points[0]
    check("线路坐标按新校准重算了",
          abs(px.x - 0.0) < 1e-6 and abs(px.y - 1.0) < 1e-6,
          f"=({px.x:.4f}, {px.y:.4f})")
    check("主窗口会被通知重画(calib_changed 增加)",
          dlg2.calib_changed == before + 1, f"={before} → {dlg2.calib_changed}")
    # 解除关联不该把坐标改回去(那属于又一次重算, 没有旧矩形可用)
    combo2.setCurrentIndex(combo2.findData(""))
    app.processEvents()
    check("解除关联后记录本身没被破坏",
          replay.norm_rect == [0.0, 2048.0, 0.0, 2048.0], f"={replay.norm_rect}")
    dlg2.close()

    print("\n--- 9) 一个关卡代号只能归一张图(被占用的变灰, 不是消失) ---")
    other = "0fc34e933121"
    cache.put(other, make_png("#884455"))
    store.remember_map(other)
    store.rename_map(other, "荷兰")
    store.set_map_level(HASH_KEY, LEVEL)        # 芬兰那张占着 avg_finland

    dlg3 = MapManagerDialog(store, cache)

    def combo_of(k: str):
        r = next(rr for rr in range(dlg3.table.rowCount())
                 if dlg3.table.item(rr, COL_KEY).data(Qt.ItemDataRole.UserRole) == k)
        return dlg3.table.cellWidget(r, COL_LEVEL)

    def items(k: str) -> list[str]:
        c = combo_of(k)
        return [c.itemData(i) for i in range(c.count())]

    def enabled_of(k: str, level: str) -> bool:
        c = combo_of(k)
        return c.model().item(c.findData(level)).isEnabled()

    check("两行都在列表里", dlg3.table.rowCount() == 2, f"rows={dlg3.table.rowCount()}")
    check("自己占的代号可用", enabled_of(HASH_KEY, LEVEL))
    check("别人占的代号**仍然列出来**(不藏)", LEVEL in items(other), f"={items(other)}")
    check("但它是灰的、点不了", not enabled_of(other, LEVEL))
    tip = combo_of(other).model().item(combo_of(other).findData(LEVEL)).toolTip()
    check("提示里写明被谁用了", "芬兰" in tip, f"={tip!r}")
    check("没人占的代号两边都能选",
          enabled_of(HASH_KEY, WRONG_LEVEL) and enabled_of(other, WRONG_LEVEL))

    # 在另一行把它选走 → 本行那个项要立刻变灰
    cb = combo_of(other)
    cb.setCurrentIndex(cb.findData(WRONG_LEVEL))
    app.processEvents()
    check("另一行选走后, 本行里它变灰了", not enabled_of(HASH_KEY, WRONG_LEVEL))
    check("两行各占一个, 互不干扰",
          store.level_of_hash(HASH_KEY) == LEVEL
          and store.level_of_hash(other) == WRONG_LEVEL,
          f"={store.level_of_hash(HASH_KEY)!r},{store.level_of_hash(other)!r}")

    # 释放回来 → 本行又能选
    cb.setCurrentIndex(cb.findData(""))
    app.processEvents()
    check("解除后本行又能选了", enabled_of(HASH_KEY, WRONG_LEVEL))
    dlg3.close()

    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
