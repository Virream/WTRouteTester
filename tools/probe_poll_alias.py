"""选默认轮询间隔: 模拟 8111 采样与游戏帧率的对拍(aliasing)。

模型
----
8111 的 `/indicators`、`/map_obj.json` 每渲染一帧才更新一次数据。
软件以周期 P 去取, 每次拿到的是"最近一帧"的数据。所以:

* P < 帧周期 T  -> 每一帧都采到(会重复采同一帧), 数据更新率 = 帧率, 最平滑;
* P > 帧周期 T  -> 只能采到部分帧, 采到的帧序号增量在 floor(P/T)、ceil(P/T) 之间摇摆,
  于是轨迹点间隔在两种值之间跳变; 当 P/T 接近整数时最规律, 接近 n+0.5 时最跳;
* 帧间隔与轮询周期都各有抖动(实测 30FPS 限帧时帧间隔 33~47ms), 所以"精确对拍"
  不会持久, 表现为慢速漂移的漏帧, 而不是永久锁死。

用法
----
    python tools/probe_poll_alias.py            # 默认候选表
    python tools/probe_poll_alias.py 9 10 11    # 只看指定毫秒
"""
from __future__ import annotations

import bisect
import random
import statistics
import sys

FPS_LIST = (60, 80, 100, 120, 144, 165, 240)
DUR_S = 30.0
FRAME_JIT = 0.10   # 帧间隔相对抖动(标准差); 实测限帧时更大, 高端显卡更小
POLL_JIT = 0.03    # 轮询周期相对抖动(Qt 定时器)


def simulate(poll_ms: float, fps: float, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    T = 1000.0 / fps
    frames: list[float] = []
    t = 0.0
    while t < DUR_S * 1000.0:
        frames.append(t)
        step = T * max(0.2, 1.0 + rnd.gauss(0.0, FRAME_JIT))
        t += step
    polls: list[float] = []
    t = 0.0
    while t < DUR_S * 1000.0:
        polls.append(t)
        step = poll_ms * max(0.2, 1.0 + rnd.gauss(0.0, POLL_JIT))
        t += step

    captured: list[int] = []
    for p in polls:
        i = bisect.bisect_right(frames, p) - 1
        if i < 0:
            continue
        if not captured or captured[-1] != i:
            captured.append(i)

    times = [frames[i] for i in captured]
    gaps = [b - a for a, b in zip(times, times[1:])]
    if not gaps:
        return dict(rate=0.0, cover=0.0, p50=0.0, p90=0.0, mx=0.0)
    gaps_sorted = sorted(gaps)
    return dict(
        rate=len(captured) / DUR_S,
        cover=len(captured) / len(frames),
        p50=statistics.median(gaps),
        p90=gaps_sorted[int(len(gaps_sorted) * 0.9)],
        mx=gaps_sorted[-1],
    )


def main() -> None:
    if len(sys.argv) > 1:
        cands = [float(x) for x in sys.argv[1:]]
    else:
        cands = [6.0, 7.5, 8.33, 9.0, 10.0, 11.0, 12.0, 12.5, 13.0, 14.0,
                 15.0, 16.0, 16.67, 20.0, 25.0, 40.0]

    head = f"{'间隔ms':>7} {'Hz':>5} |"
    for fps in FPS_LIST:
        head += f" {fps:>13}"
    print(head)
    print(f"{'':7} {'':5} |" + "  采到% / p90间隔ms".ljust(13) * len(FPS_LIST))
    print("-" * len(head))

    for P in cands:
        line = f"{P:>7.2f} {1000.0 / P:>5.0f} |"
        for fps in FPS_LIST:
            r = simulate(P, fps)
            line += f"  {r['cover'] * 100:5.0f}% {r['p90']:6.1f}"
        print(line)

    print()
    print("说明: '采到%' = 该帧率下能采到的帧占比(受 P > T 限制), "
          "p90间隔 = 相邻轨迹点时间间隔的 90 分位(越小越均匀)。")
    print("P < T 时恒为 100% 且间隔=p90=帧周期; P 逼近 T 时最怕时钟漂移造成的慢速漏帧。")


if __name__ == "__main__":
    main()
