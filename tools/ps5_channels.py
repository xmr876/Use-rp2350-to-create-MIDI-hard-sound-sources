#!/usr/bin/env python3
"""
ps5_channels.py — 列出所有曲子的通道占用，检查有没有冲突。

这个检查解决的是一类真出过的 bug：
  · 两个乐器共用同一个通道（PS5 每个声部只有一个音色，后设的覆盖前面的）
  · 有乐器占了通道 9（0 起算）= 实际通道 10 = 鼓组专用，手册明确规定
    "you can only assign drum kits to part 10"
  · SETUP 里配了音色但整首曲子一个音都没发（通道常量写错）

用法：
    python tools/ps5_channels.py
"""
from __future__ import annotations

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

PIECES = [("piece", "流行曲"), ("jrock", "日摇"), ("serene", "悠扬"),
          ("solister", "Dream Solister 风"), ("jpop", "J-POP（16 复音）")]
BARS = "─" * 72


def main() -> int:
    print(BARS)
    print("通道常量表（0 起算 → 实际通道号）")
    print(BARS)
    consts = sorted(
        ((n, getattr(gm, n)) for n in dir(gm)
         if n.startswith("CH_") and isinstance(getattr(gm, n), int)),
        key=lambda x: (x[1], x[0]))
    by_ch: dict[int, list[str]] = defaultdict(list)
    for n, v in consts:
        by_ch[v].append(n)
    for v in sorted(by_ch):
        tag = "   ← 鼓组专用，任何乐器都不能占！" if v == gm.DRUM_CHANNEL else ""
        print(f"  通道 {v:>2}（实际 {v + 1:>2}）: {', '.join(by_ch[v])}{tag}")

    rc = 0
    for mod_name, label in PIECES:
        print(f"\n{BARS}")
        print(f"{label}（--piece {mod_name}）")
        print(BARS)
        try:
            P = importlib.import_module(f"ps5.{mod_name}")   # noqa: N806
        except Exception as e:
            print(f"  ❌ 载入失败：{type(e).__name__} {e}")
            rc = 1
            continue

        score = P.build_score()
        used: dict[int, int] = defaultdict(int)
        for e in score.events:
            used[e.ch] += 1

        print(f"  {'通道':<8}{'实际':<6}{'音色':<26}{'名字':<14}{'音符数':>8}")
        print(f"  {'-' * 64}")
        bad = []
        seen_ch: dict[int, str] = {}
        for ch, prog, vol, pan, rev, nm in P.SETUP:
            n = used.get(ch, 0)
            flag = ""
            if ch == gm.DRUM_CHANNEL and prog != 0:
                flag = "  ❌ 占了鼓组通道！"
                bad.append(f"{nm} 用了通道 {ch}（鼓组专用）")
            if ch in seen_ch:
                flag += f"  ❌ 与「{seen_ch[ch]}」撞通道"
                bad.append(f"{nm} 和 {seen_ch[ch]} 共用通道 {ch}")
            seen_ch[ch] = nm
            if n == 0:
                flag += "  ⚠ 整曲 0 个音"
                bad.append(f"{nm} 配了音色但一个音都没发")
            actual = f"{ch + 1}"
            print(f"  {ch:<8}{actual:<6}{gm.name(prog):<26}{nm:<14}{n:>8}{flag}")

        # 有音符但没在 SETUP 里的通道
        for ch, n in sorted(used.items()):
            if ch not in seen_ch:
                print(f"  {ch:<8}{ch + 1:<6}{'—':<26}{'未定义':<14}{n:>8}"
                      f"  ❌ SETUP 里没给它音色")
                bad.append(f"通道 {ch} 有 {n} 个音但 SETUP 没定义")

        if bad:
            rc = 1
            print()
            for b in bad:
                print(f"  ❌ {b}")
        else:
            print(f"\n  ✅ {len(P.SETUP)} 个声部，通道无冲突")

        secs = P.DURATION_SEC
        print(f"  曲长 {secs:.0f}s = {secs // 60:.0f} 分 {secs % 60:.0f} 秒"
              f"  @ {P.BPM} BPM，{P.TOTAL_BARS} 小节")
        if getattr(score, "dropped", 0):
            print(f"  ⚠ 去重叠丢弃 {score.dropped} 个音")
            rc = 1
    print(f"\n{BARS}")
    print("✅ 全部无冲突" if rc == 0 else "❌ 有冲突，见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
