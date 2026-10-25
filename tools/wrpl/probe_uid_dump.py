"""挑几个「uid 读成 112」的创建包, 把组件值起点附近 80 字节摊开看:
哪个偏移上的 u16(低 11 位)落在位置流的 uid 集合里。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wt81111g import wrpl_entities as we                        # noqa: E402
from wt81111g.wrpl_reader import load_replay                    # noqa: E402

DEFAULT = os.path.join(ROOT, "回放文件", "服务器")
KEEP: list = []


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    orig = we.EcsCollector.build

    def patched(self, **kw):
        KEEP.extend(self._creations)
        return orig(self, **kw)

    we.EcsCollector.build = patched
    try:
        data = load_replay(path)
    finally:
        we.EcsCollector.build = orig
    known = set(data.units)
    rel = data.ecs_uid_offset or 28
    print(f"位置流 uid {len(known)} 个; 标定偏移 rel={rel}")
    print(f"uid 集合(前 30): {sorted(known)[:30]}")

    shown = 0
    for c in KEEP:
        if "player_unit" not in c.template or "aircraft" in c.template:
            continue
        v = we.EcsCollector._uid_at(c, rel)
        if v in known:
            continue
        # 找一下 spawn 名, 确认是玩家载具
        spawn = ""
        for _o, s in we.scanned_strings(c.block, 3, 60):
            if we.SPAWN_RE.match(s):
                spawn = s
                break
        if not spawn:
            continue
        print(f"\n--- tick={c.tick} template={c.template} spawn={spawn}"
              f"  rel={rel} 读出 {v} ---")
        base = c.vbits // 8
        chunk = c.block[base:base + 80]
        print("  hex:", chunk.hex(" "))
        hits = []
        for d in range(0, 80):
            val = we.EcsCollector._uid_at(c, d)
            if val is not None and val in known:
                hits.append((d, val))
        print(f"  落在 uid 集合里的偏移: {hits}")
        shown += 1
        if shown >= 5:
            break


if __name__ == "__main__":
    main()
