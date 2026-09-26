#!/usr/bin/env python3
"""
ps5_collisions.py — 找出某首曲子里所有「同通道 + 同音高 + 时间重叠」的音。

为什么需要这个工具（而不是每次临时写脚本）：
  这种重叠是**最隐蔽的 MIDI 写错方式** —— 文件完全合法，播放器也不报错，
  但在硬件音源上，**先到的 note-off 会把后一个音一起关掉**，留下一个
  永远关不掉的 note-on，听起来就是"挂音"（一直响不停）。

  它又是最容易犯的：旋律乐句收尾撞下一句、铺底撞旋律、长音跨小节、
  两个分支判断写岔导致同一小节被处理两次…… 每一种我都真踩过。

用法：
    python tools/ps5_collisions.py                  # 检查所有曲子
    python tools/ps5_collisions.py --piece jpop     # 只查一首
"""
from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from collections import Counter

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

PIECES = [
    ("piece", "流行曲"), ("jrock", "日摇"), ("serene", "悠扬"),
    ("solister", "Dream Solister 风"), ("jpop", "J-POP"),
]


def collect(P, mod_name: str) -> list[tuple]:
    """跑一遍 build_score，并给每个音符标上"由哪个函数产生"。"""
    orig_add = P.Score.add
    orig_dedupe = P.dedupe
    rows: list[tuple] = []
    src_file = f"{mod_name}.py"

    def tagged_add(self, beat, dur, ch, pitch, vel=90):
        src = "?"
        for fr in traceback.extract_stack()[:-1][::-1]:
            if src_file in fr.filename and fr.name not in (
                    "tagged_add", "chord", "add", "build_score",
                    "_chorus_bar", "_full_bar"):
                src = fr.name
                break
        rows.append((beat, ch, pitch, dur, vel, src))
        return orig_add(self, beat, dur, ch, pitch, vel)

    def passthrough(evs, cls):
        o = cls()
        o.events = list(evs)
        o.dropped = 0
        return o

    P.Score.add = tagged_add
    P.dedupe = passthrough
    try:
        P.build_score()
    finally:
        P.Score.add = orig_add
        P.dedupe = orig_dedupe
    return rows


def report(P, mod_name: str, label: str) -> int:
    rows = collect(P, mod_name)
    last: dict[tuple[int, int], tuple[float, float, str]] = {}
    cols: list[tuple] = []
    for beat, ch, pitch, dur, vel, src in sorted(
            rows, key=lambda x: (x[0], x[1], x[2])):
        k = (ch, pitch)
        prev = last.get(k)
        if prev and beat < prev[0] + prev[1] - 1e-9:
            cols.append((beat, ch, pitch, dur, src, prev))
        else:
            last[k] = (beat, dur, src)

    dropped = getattr(P.build_score(), "dropped", 0)
    status = "✅" if not cols else "❌"
    print(f"\n{status} {label}（--piece {mod_name}）："
          f"{len(rows)} 个音，{len(cols)} 处重叠"
          + (f"，去重叠丢弃 {dropped} 个" if dropped else ""))

    if not cols:
        return 0

    by = Counter((c[1], c[2]) for c in cols)
    for (ch, p), n in by.most_common(6):
        print(f"     通道 {ch + 1} 音高 {p}: {n} 处")

    for beat, ch, pitch, dur, src, prev in cols[:8]:
        bar = int(beat // 4)
        sec = next((nm for nm, b0, b1, _d in P.SECTIONS if b0 <= bar < b1), "?")
        print(f"     ── 小节 {bar + 1}（{sec}）通道 {ch + 1} 音高 {pitch}")
        print(f"        前一个 拍 {prev[0]:.2f} 时值 {prev[1]:.2f} "
              f"→ 结束 {prev[0] + prev[1]:.2f}  来源 {prev[2]}")
        print(f"        后一个 拍 {beat:.2f} 时值 {dur:.2f} "
              f"→ 结束 {beat + dur:.2f}  来源 {src}")
    if len(cols) > 8:
        print(f"     … 还有 {len(cols) - 8} 处")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="检查同音高重叠（挂音）")
    ap.add_argument("--piece", default="all",
                    help="piece / jrock / serene / solister / jpop / all")
    args = ap.parse_args()

    names = ([p for p, _l in PIECES] if args.piece == "all" else [args.piece])
    rc = 0
    for mod_name in names:
        label = dict(PIECES).get(mod_name, mod_name)
        try:
            P = importlib.import_module(f"ps5.{mod_name}")   # noqa: N806
            rc |= report(P, mod_name, label)
        except Exception as e:
            print(f"\n❌ {label}: {type(e).__name__}: {e}")
            rc = 1
    print()
    print("✅ 没有重叠" if rc == 0 else "❌ 有重叠，见上")
    return rc


if __name__ == "__main__":
    sys.exit(main())
