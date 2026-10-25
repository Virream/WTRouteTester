"""验证 RouteStore 的校准存取 + 旧 `wrpl:` 键迁移(用临时文件, 不碰真数据)。"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.map_calib import MapCalib                    # noqa: E402
from wt81111g.route_store import GlobalRecord, RouteStore  # noqa: E402


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="wtstore_")
    path = os.path.join(tmp, "routes.json")

    st = RouteStore(path)
    print("空库:", st.map_calib, st.known_maps)

    calib = MapCalib(min_x=0, max_x=2048, min_z=0, max_z=2048,
                     img_w=2048, img_h=2048, source="unit-test",
                     zones=[{"label": "A", "x": 597.6, "z": 1039.6}])
    assert st.set_calib("wrpl_avg_finland", calib) is True
    assert st.set_calib("wrpl_avg_finland", calib) is False, "同样的校准不该重复写盘"
    print("set_calib OK, known_maps =", st.known_maps)

    got = st.calib_of("wrpl_avg_finland")
    assert got is not None and got.max_x == 2048 and got.zones[0]["label"] == "A"
    print("calib_of OK:", got.describe())

    st2 = RouteStore(path)          # 重新加载
    got2 = st2.calib_of("wrpl_avg_finland")
    assert got2 is not None and got2.to_dict() == got.to_dict(), "落盘/读回不一致"
    print("落盘 → 读回 OK")

    assert st2.clear_calib("wrpl_avg_finland") is True
    assert st2.calib_of("wrpl_avg_finland") is None
    assert RouteStore(path).calib_of("wrpl_avg_finland") is None
    print("clear_calib OK")

    # ---- 旧键迁移 ----
    raw = {
        "version": 1,
        "map_names": {"wrpl:avg_finland": "芬兰"},
        "known_maps": ["wrpl:avg_finland"],
        "map_calib": {"wrpl:avg_finland": calib.to_dict()},
        "globals": [{
            "route_id": "wrpl0001", "map_key": "wrpl:avg_finland",
            "map_name": "avg_finland", "created_at": "2026-10-25T20:00:00",
            "norm_rect": [0, 2048, 0, 2048],
            "sides": {"A": [], "B": []},
        }],
    }
    import gzip
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    st3 = RouteStore(path)
    assert st3.globals[0].map_key == "wrpl_avg_finland", st3.globals[0].map_key
    assert st3.name_of("wrpl_avg_finland") == "芬兰"
    assert st3.calib_of("wrpl_avg_finland") is not None
    print("旧 `wrpl:` 键迁移 OK →", st3.globals[0].map_key, "| is_replay =",
          st3.globals[0].is_replay)

    st3.save()
    st4 = RouteStore(path)
    assert "wrpl:avg_finland" not in st4.known_maps and "wrpl_avg_finland" in st4.known_maps
    print("迁移后写盘 OK, known_maps =", st4.known_maps)
    print("\n全部通过")


if __name__ == "__main__":
    main()
