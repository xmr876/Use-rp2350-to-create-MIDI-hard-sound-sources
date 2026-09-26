#!/usr/bin/env python3
"""
ps5_pianoroll.py — 把乐曲画成钢琴卷帘图（PNG），用来肉眼检查编曲。

为什么需要：MIDI 的"对不对"光靠数字看不出来。画成图以后一眼能看出
   · 各声部有没有真的在活动、什么时候进什么时候出
   · 有没有哪个声部一直在响（说明漏了 note-off）
   · 段落结构（INTRO/A/B/BREAK/C）是否真的做出层次
   · 旋律线是不是连贯的线条，而不是一堆乱点

红线是段落边界，颜色按 MIDI 通道区分。
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

from ps5 import gmnames as gm   # noqa: E402

P = None                        # 在 main() 里按 --piece 载入

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

W, H = 1800, 640
PAD_L, PAD_R, PAD_T, PAD_B = 90, 30, 40, 50

# 通道 → 颜色
COLORS = {
    gm.CH_PIANO: (60, 130, 240),
    gm.CH_BASS: (230, 120, 40),
    gm.CH_STRINGS: (220, 60, 90),
    gm.CH_GTR: (40, 170, 110),
    gm.CH_LEAD: (170, 90, 220),
    gm.CH_BRASS: (200, 160, 30),
    gm.CH_CHOIR: (120, 200, 220),
    gm.CH_MARIMBA: (240, 120, 180),
    gm.CH_PAD: (140, 140, 150),
    gm.CH_DRUMS: (90, 90, 100),
}
CH_LABEL = {
    gm.CH_PIANO: "1 钢琴", gm.CH_BASS: "2 贝斯", gm.CH_STRINGS: "3 弦乐",
    gm.CH_GTR: "4 吉他", gm.CH_LEAD: "5 主音", gm.CH_BRASS: "6 铜管",
    gm.CH_CHOIR: "7 合唱", gm.CH_MARIMBA: "8 马林巴", gm.CH_PAD: "9 垫/清音",
    gm.CH_DRUMS: "10 鼓",
}


def draw(path: str) -> str:
    score = P.build_score()
    img = Image.new("RGB", (W, H), (18, 18, 22))
    d = ImageDraw.Draw(img)

    t_max = P.TOTAL_BEATS
    lo = min(e.pitch for e in score.events)
    hi = max(e.pitch for e in score.events)
    lo, hi = lo - 2, hi + 2

    def x(beat: float) -> float:
        return PAD_L + (beat / t_max) * (W - PAD_L - PAD_R)

    def y(pitch: int) -> float:
        return PAD_T + (hi - pitch) / (hi - lo) * (H - PAD_T - PAD_B)

    # 每八度一条横线 + 音名
    for p in range(lo, hi + 1):
        if p % 12 == 0:
            yy = y(p)
            d.line([(PAD_L, yy), (W - PAD_R, yy)], fill=(45, 45, 52))
            d.text((8, yy - 6), f"C{p // 12 - 1}", fill=(150, 150, 160))

    # 小节线
    for bar in range(0, P.TOTAL_BARS + 1, 4):
        xx = x(bar * 4)
        major = bar % 16 == 0
        d.line([(xx, PAD_T), (xx, H - PAD_B)],
               fill=(70, 70, 80) if major else (38, 38, 44))

    # 音符
    for e in score.events:
        c = COLORS.get(e.ch, (200, 200, 200))
        x0, x1 = x(e.beat), x(e.beat + e.dur)
        y0, y1 = y(e.pitch + 0.42), y(e.pitch - 0.42)
        if x1 - x0 < 1.2:
            x1 = x0 + 1.2
        d.rectangle([x0, y0, x1, y1], fill=c)

    # 段落分隔 + 标题
    for name, b0, b1, desc in P.SECTIONS:
        xx = x(b0 * 4)
        d.line([(xx, PAD_T - 14), (xx, H - PAD_B)], fill=(230, 80, 80), width=2)
        d.text((xx + 6, 12), f"{name}  {desc}", fill=(240, 200, 120))
    d.line([(x(P.TOTAL_BEATS), PAD_T - 14), (x(P.TOTAL_BEATS), H - PAD_B)],
           fill=(230, 80, 80), width=2)

    # 时间刻度（秒）
    spb = 60.0 / P.BPM
    for sec in range(0, int(P.DURATION_SEC) + 1, 20):
        beat = sec / spb
        if beat > t_max:
            break
        xx = x(beat)
        d.line([(xx, H - PAD_B), (xx, H - PAD_B + 6)], fill=(120, 120, 130))
        d.text((xx - 12, H - PAD_B + 9), f"{sec}s", fill=(150, 150, 160))

    # 图例
    lx, ly = PAD_L, H - 26
    for ch in sorted(COLORS):
        d.rectangle([lx, ly, lx + 12, ly + 12], fill=COLORS[ch])
        d.text((lx + 16, ly), CH_LABEL.get(ch, str(ch + 1)), fill=(200, 200, 210))
        lx += 105

    d.text((PAD_L, 12), f"{P.__name__.split('.')[-1].upper()} · "
                        f"{P.TOTAL_BARS} 小节 @ {P.BPM} BPM · "
                        f"{P.DURATION_SEC:.0f} 秒 · {len(score.events)} 个音",
           fill=(230, 230, 230))

    img.save(path)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="画钢琴卷帘图")
    ap.add_argument("--piece", default="piece", help="piece / jrock")
    ap.add_argument("-o", "--out", default=None, help="输出 PNG 路径")
    a = ap.parse_args()
    P = importlib.import_module(f"ps5.{a.piece}")   # noqa: N806
    out = a.out or os.path.join(r"D:\音源\build", f"ps5_pianoroll_{a.piece}.png")
    print(f"已写出 {draw(out)}")
