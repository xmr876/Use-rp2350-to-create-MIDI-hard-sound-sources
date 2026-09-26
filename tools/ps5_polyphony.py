#!/usr/bin/env python3
"""
ps5_polyphony.py — 数出曲子每个时刻的**同时发声数**（复音数），并验证设计要求。

为什么需要单独一个工具：
  "16 复音"是个具体的技术指标，光看代码堆了几个乐器是看不出来的 ——
  得实际扫一遍所有 note-on / note-off，算出每一刻到底有几个音在响。
  同时它还能顺带查出：
    · 复音数超出音源上限的段落（GM 音源通常 64 复音，单通道也要留意）
    · 复音数的段落变化是否符合设计（副歌应该最密）

用法：
    python tools/ps5_polyphony.py                  # 所有曲子
    python tools/ps5_polyphony.py --piece jpop     # 只查一首
"""
from __future__ import annotations

import argparse
import importlib
import sys

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

PIECES = [("piece", "流行曲"), ("jrock", "日摇"), ("serene", "悠扬"),
          ("solister", "Dream Solister 风"), ("jpop", "J-POP（16 复音）")]
BAR = "─" * 74


def polyphony_timeline(P) -> tuple[list[tuple[float, int]], list]:
    """返回 (采样点, 事件表)。采样点 = 每个改变复音数的事件时刻。"""
    evs = P.build_score().events
    marks: list[tuple[float, int]] = []      # (拍, 变化量)
    for e in evs:
        marks.append((e.beat, 1))
        marks.append((e.beat + e.dur, -1))
    marks.sort()
    timeline = []
    cur = 0
    for beat, delta in marks:
        cur += delta
        timeline.append((beat, cur))
    return timeline, evs


def active_at(P, evs, beat: float) -> dict[int, int]:
    """某一拍上各通道正在响的音数。"""
    out: dict[int, int] = {}
    for e in evs:
        if e.beat <= beat < e.beat + e.dur - 1e-9:
            out[e.ch] = out.get(e.ch, 0) + 1
    return out


def report(P, mod_name: str, label: str, target: int | None) -> int:
    timeline, evs = polyphony_timeline(P)
    peak = max((n for _b, n in timeline), default=0)
    peak_beat = max(timeline, key=lambda x: x[1])[0] if timeline else 0.0
    peak_bar = int(peak_beat // 4)
    try:
        sec = next((nm for nm, b0, b1, _d in P.SECTIONS if b0 <= peak_bar < b1),
                   "?")
    except Exception:
        sec = "?"

    # 每段的峰值
    size = getattr(P, "BEATS_PER_BAR", 4.0)
    per_sec: dict[str, int] = {}
    for name, b0, b1, _d in P.SECTIONS:
        lo, hi = b0 * size, b1 * size
        vals = [n for b, n in timeline if lo <= b < hi]
        per_sec[name] = max(vals) if vals else 0

    status = "✅"
    note = ""
    if target is not None:
        if peak < target:
            status = "❌"
            note = f"  峰值 {peak} < 目标 {target}"
        else:
            note = f"  峰值 {peak} ≥ 目标 {target} ✅"
    print(f"\n{status} {label}（--piece {mod_name}）")
    print(f"   峰值复音 {peak} 音  出现在第 {peak_bar + 1} 小节（{sec} 段）{note}")
    print(f"   各段峰值：" + "  ".join(f"{k}={v}" for k, v in per_sec.items()))

    act = active_at(P, evs, peak_beat)
    detail = "  ".join(
        f"ch{c + 1}×{n}" for c, n in sorted(act.items(), key=lambda x: -x[1]))
    print(f"   峰值那一刻的构成：{detail}")

    if target is not None and peak < target:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="统计同时发声数（复音数）")
    ap.add_argument("--piece", default="all", help="曲名 / all")
    ap.add_argument("--target", type=int, default=None,
                    help="要求的最小峰值复音数（只对单曲有意义）")
    args = ap.parse_args()

    names = ([p for p, _l in PIECES] if args.piece == "all" else [args.piece])
    rc = 0
    for mod_name in names:
        label = dict(PIECES).get(mod_name, mod_name)
        try:
            P = importlib.import_module(f"ps5.{mod_name}")   # noqa: N806
            tgt = args.target if args.piece != "all" else (
                16 if mod_name == "jpop" else None)
            rc |= report(P, mod_name, label, tgt)
        except Exception as e:
            print(f"\n❌ {label}: {type(e).__name__}: {e}")
            rc = 1
    print(f"\n{BAR}")
    print("✅ 全部达标" if rc == 0 else "❌ 有不达标项，见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
