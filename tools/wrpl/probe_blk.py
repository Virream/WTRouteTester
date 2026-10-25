"""探针: 看清结算 BLK 的分段结构（名字池边界 + 数据区头）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wt81111g import wrpl_reader as W                                  # noqa: E402


def main(path: str) -> None:
    if os.path.isdir(path):
        files, infos = W._scan_parts(path, expect_server=None)
    else:
        files, infos = [path], [W.read_header_file(path)]
    fp, info = files[0], infos[0]
    with open(fp, "rb") as fh:
        buf = fh.read()
    rez = info.results_offset
    tail = buf[rez:]
    print(f"结算段 {len(tail):,} B, 头 8 B: {tail[:8].hex()}")

    # 名字池: 从偏移 4 开始, 连续 \0 分隔的可见字符串
    i = 4
    names = []
    while i < len(tail):
        j = tail.find(b"\x00", i)
        if j < 0 or j == i:
            break
        seg = tail[i:j]
        if not all(0x20 <= c < 0x7F for c in seg):
            break
        names.append(seg.decode("latin-1"))
        i = j + 1
        if len(names) > 300:
            break
    print(f"名字池: 从偏移 4 起, {len(names)} 个, 结束于偏移 {i}")
    print(f"  最后 8 个: {names[-8:]}")
    print(f"\n数据区起点 {i}, 之后 160 B:")
    body = tail[i:i + 160]
    print("  hex :", body[:80].hex())
    print("  hex2:", body[80:].hex())

    # 试着按“每个值前面有 tag”看
    print("\n数据区前 40 字节逐字节:", " ".join(f"{b:02x}" for b in tail[i:i + 40]))


if __name__ == "__main__":
    main(sys.argv[1])
