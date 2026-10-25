"""探针: 结算段里能直接扫出的载具型号 / 玩家名（明文），用于补 ECS 的缺口。"""
from __future__ import annotations

import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402

STR = re.compile(rb"(?:[\x20-\x7e]|[\xc2-\xf4][\x80-\xbf]+){4,}")
VEHICLE = re.compile(r"^[a-z]{2,4}_[a-z0-9_]{3,48}$")


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]

    veh: collections.Counter = collections.Counter()
    names: list[str] = []
    for fp, info in zip(files, infos):
        with open(fp, "rb") as fh:
            buf = fh.read()
        rez = info.results_offset
        if not rez or rez >= len(buf):
            continue
        body = buf[rez:]
        for m in STR.finditer(body):
            try:
                t = m.group().decode("utf-8")
            except UnicodeDecodeError:
                continue
            if VEHICLE.match(t):
                veh[t] += 1
            elif len(names) < 40 and 2 <= len(t) <= 32 and " " not in t.strip():
                names.append(t)

    print(f"结算段里的载具型号: {len(veh)} 种")
    for k, v in veh.most_common(60):
        print(f"  {v:4}x  {k}")
    print(f"\n疑似玩家名/军团（前 25）: {names[:25]}")


if __name__ == "__main__":
    main(sys.argv[1])
