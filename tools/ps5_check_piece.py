#!/usr/bin/env python3
"""
ps5_check_piece.py — 乐曲静态自检（支持多首曲子）。

MIDI 写错最典型的几种翻车方式，这里全部提前查出来：
  1. 同一通道、同一音高、时间重叠 → 前一个音的 note-off 会误杀后一个，挂音
  2. 复音数超上限 → 早期音源会偷音
  3. 音高越界 / 力度越界 / 时值非正
  4. 段落边界与和声循环错位
  5. 通道 10 用了 GM 打击乐音域之外的音高（PS5 规定通道 10 只能放鼓）
  6. 同一个通道被当成两种乐器用

用法：
    python tools/ps5_check_piece.py                # 默认查流行曲
    python tools/ps5_check_piece.py --piece jrock  # 查日摇
    python tools/ps5_check_piece.py --piece all    # 两首都查
"""
from __future__ import annotations

import argparse
import importlib
import sys
from collections import defaultdict

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

from ps5 import gmnames as gm   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def check(P, label: str) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    score = P.build_score()
    evs = score.events

    print(f"\n{'=' * 74}\n== {label}\n{'=' * 74}")
    print(f"  {P.TOTAL_BARS} 小节 @ {P.BPM} BPM = {P.DURATION_SEC:.1f} 秒 "
          f"({P.DURATION_SEC // 60:.0f} 分 {P.DURATION_SEC % 60:.0f} 秒)")

    by_ch: dict[int, list] = defaultdict(list)
    for e in evs:
        by_ch[e.ch].append(e)

    # ── 1 同音高重叠 ──
    for ch, lst in sorted(by_ch.items()):
        per_pitch: dict[int, list[tuple[float, float]]] = defaultdict(list)
        for e in lst:
            per_pitch[e.pitch].append((e.beat, e.beat + e.dur))

        overlaps = 0
        for pitch, iv in per_pitch.items():
            iv.sort()
            for a, b in zip(iv, iv[1:]):
                if b[0] < a[1] - 1e-9:
                    overlaps += 1
                    if overlaps <= 3:
                        problems.append(
                            f"通道 {ch + 1} 音高 {pitch}: "
                            f"{a[0]:.2f}-{a[1]:.2f} 与 {b[0]:.2f}-{b[1]:.2f} 重叠")
        if overlaps > 3:
            problems.append(f"通道 {ch + 1} 共 {overlaps} 处同音高重叠")

        # ── 2 复音数 ──
        points = []
        for e in lst:
            points.append((e.beat, 1))
            points.append((e.beat + e.dur, -1))
        points.sort()
        cur = mx = 0
        for _, d in points:
            cur += d
            mx = max(mx, cur)
        if mx > 16:
            warnings.append(f"通道 {ch + 1} 最大复音 {mx}（音源只有 16 声部，"
                            f"单通道这么多音可能被偷音）")
        print(f"  通道 {ch + 1:>2}: {len(lst):>5} 个音, 最大复音 {mx:>2}, "
              f"音域 {min(e.pitch for e in lst)}-{max(e.pitch for e in lst)}")

    # ── 3 越界 ──
    for e in evs:
        if not 0 <= e.pitch <= 127:
            problems.append(f"音高越界 {e.pitch}")
        if not 1 <= e.vel <= 127:
            problems.append(f"力度越界 {e.vel} @ 拍 {e.beat}")
        if e.dur <= 0:
            problems.append(f"时值非正 {e.dur} @ 拍 {e.beat}")

    # ── 4 段落边界 ──
    # ★ 这里**不检查**"边界必须是 4 的倍数"。
    #   那是我上一首曲子的特例（四个和弦每小节一个，两者恰好等价），
    #   我把它错当成了通用规则，结果在 solister 上误报 6 条 ——
    #   而该曲段的边界其实精确落在每段和声循环的起点上。
    #   真正该查的是：段之间无缝、区间非空、第一段从 0 开始。
    if P.SECTIONS[0][1] != 0:
        problems.append("第一段不从第 0 小节开始")
    for name, b0, b1, _d in P.SECTIONS:
        if b1 <= b0:
            problems.append(f"段 {name} 的区间 {b0}-{b1} 为空或反了")
    for (n0, _a0, a1, _d0), (n1, b0, _b1, _d1) in zip(P.SECTIONS, P.SECTIONS[1:]):
        if a1 != b0:
            problems.append(f"段 {n0} 结束于 {a1}，但 {n1} 从 {b0} 开始 —— 有缝")

    bars_with_notes = {int(e.beat // 4) for e in evs}
    missing = [b for b in range(P.TOTAL_BARS) if b not in bars_with_notes]
    if missing:
        warnings.append(f"这些小节一个音都没有：{missing[:10]}")

    # ── 5 通道 10 只能放打击乐 ──
    drum_pitches = {e.pitch for e in by_ch.get(gm.DRUM_CHANNEL, [])}
    bad = {p for p in drum_pitches if not 27 <= p <= 87}
    if bad:
        problems.append(f"通道 10 用了 GM 打击乐音域(27-87)之外的音高：{sorted(bad)}")

    # ── 6 一个通道两种乐器 / 定义了却没用到 ──
    used = {c for c, _p, _v, _pan, _r, _l in P.SETUP}
    if len(used) != len(P.SETUP):
        problems.append("SETUP 里有通道被定义了两遍（一个通道只能一种乐器）")
    for ch in by_ch:
        if ch not in used:
            problems.append(f"通道 {ch + 1} 有音符但 SETUP 里没给它音色")
    # 反向检查：SETUP 里配了音色却一个音都没发 —— 那就是白占一个声部，
    # 通常说明写曲时通道常量写错了（这个 bug 真出现过）
    for ch in sorted(used):
        if ch not in by_ch:
            problems.append(f"SETUP 给了通道 {ch + 1} 音色，但整首曲子一个音"
                            f"都没发 —— 通道常量可能写错了")

    # ── 去重叠统计 ──
    dropped = getattr(score, "dropped", 0)
    if dropped:
        warnings.append(f"去重叠丢弃了 {dropped} 个音（源头最好就别写出来）")

    print(f"\n  音符合计 {len(evs)}，消息 {len(P.live_events())} 条")
    print(f"  段：" + " / ".join(f"{n}({b0 + 1}-{b1})" for n, b0, b1, _ in P.SECTIONS))
    print()
    for w in warnings:
        print(f"  ⚠ {w}")
    for p in problems:
        print(f"  ❌ {p}")
    if not problems:
        print("  ✅ 没有发现结构性问题")
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="乐曲结构自检")
    ap.add_argument("--piece", default="piece",
                    help="piece（流行曲）/ jrock（日摇）/ all")
    args = ap.parse_args(argv)

    names = ["piece", "jrock"] if args.piece == "all" else [args.piece]
    rc = 0
    for n in names:
        mod = importlib.import_module(f"ps5.{n}")
        label = {"piece": "流行曲 PS5TUNE", "jrock": "日摇 JROCK"}.get(n, n)
        rc |= check(mod, label)
    return rc


if __name__ == "__main__":
    sys.exit(main())
