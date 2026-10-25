"""把 ECS 里出现的**全部模板名**列出来 —— 看有没有类似 "wreck / debris / corpse" 的
模板(残骸如果是一个特殊单位, 应该会在这里露头)。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g.wrpl_reader import load_replay                    # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    data = load_replay(path)
    tpl = data.ecs_templates or {}
    print(f"模板 {len(tpl)} 种(按实例数降序):")
    for name, n in sorted(tpl.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}× {name}")
    print("\n含 wreck / debris / corpse / dead / destroyed 的:")
    for name in tpl:
        low = name.lower()
        if any(k in low for k in ("wreck", "debris", "corpse", "dead", "destroy",
                                  "ragdoll", "burn")):
            print(f"  {tpl[name]:5d}× {name}")


if __name__ == "__main__":
    main()
