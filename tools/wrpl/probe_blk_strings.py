"""探针: 结算段数据区的 cstring 序列（验证“按玩家顺序排列”）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402


def read_cstr(buf: bytes, i: int):
    j = buf.find(b"\x00", i)
    if j < 0 or j - i > 200:
        return None, i
    seg = buf[i:j]
    if not seg:
        return None, i
    try:
        return seg.decode("utf-8"), j + 1
    except UnicodeDecodeError:
        return None, i


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]
    with open(files[0], "rb") as fh:
        buf = fh.read()
    tail = buf[infos[0].results_offset:]
    # 名字池: 头部 4 字节 + 70 个 \0 分隔名字
    i = 4
    names = 0
    while i < len(tail):
        s, j = read_cstr(tail, i)
        if s is None:
            break
        names += 1
        i = j
    print(f"名字池 {names} 个, 结束于 {i}")
    print(f"数据区头 8 B: {tail[i:i+8].hex()}")
    i += 6                                   # 跳过 3 个 u16
    print(f"从 {i} 开始读 cstring:\n")
    n = 0
    while i < len(tail) and n < 130:
        s, j = read_cstr(tail, i)
        if s is None:
            print(f"  [{i}] 非字符串, 后续 16 B: {tail[i:i+16].hex()}")
            i += 1
            continue
        print(f"  [{i:6d}] {s!r}")
        i = j
        n += 1


if __name__ == "__main__":
    main(sys.argv[1])
