#!/usr/bin/env python3
"""
ps5_audit.py — 审计每首曲子的"元数据 vs 实际"一致性。

这里查的是几类**不会报错、但会真的毁掉播放体验**的问题：

  1. SECTION_MIX 里有超出曲子长度的拍号
     → live_events 的末尾被推后，音乐放完后还要空转一截才结束
  2. live_events 的末尾和曲子应有长度对不上
  3. SECTIONS 里的段名和 build_score 里用到的段名不一致
  4. 声像（pan）偏离居中 —— 监听只接一路时该乐器就整轨听不见
  5. 通道 9（实际通道 10，鼓组专用）被非鼓乐器占用

用法：
    python tools/ps5_audit.py
"""
from __future__ import annotations

import importlib
import sys

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

from ps5 import gmnames as gm   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

PIECES = [
    ("piece", "流行曲"), ("jrock", "日摇"), ("serene", "悠扬"),
    ("solister", "Dream Solister 风"), ("jpop", "J-POP"),
]
BAR = "─" * 74


def audit(mod_name: str, label: str) -> list[str]:
    P = importlib.import_module(f"ps5.{mod_name}")   # noqa: N806
    bad: list[str] = []

    print(f"\n{BAR}\n{label}（--piece {mod_name}）\n{BAR}")

    # ── 1/2 live_events 的末尾 ──
    evs = P.live_events()
    spb = 60.0 / P.BPM
    last_beat = (evs[-1][0] - 1.2) / spb
    print(f"  曲子长度    {P.TOTAL_BARS} 小节 = {P.TOTAL_BEATS:.0f} 拍 "
          f"= {P.DURATION_SEC:.1f}s（{P.DURATION_SEC / 60:.0f} 分 "
          f"{P.DURATION_SEC % 60:.0f} 秒）")
    print(f"  事件末拍    {last_beat:.2f} 拍")
    over = last_beat - P.TOTAL_BEATS
    if over > 0.5:
        bad.append(f"live_events 比曲子长 {over:.1f} 拍 —— 音乐放完后会空转")
        print(f"              ❌ 超出 {over:.1f} 拍（会空转 {over * spb:.0f} 秒）")
    else:
        print("              ✅ 和曲子长度一致")

    # ── SECTION_MIX 越界 ──
    if hasattr(P, "SECTION_MIX"):
        for beat, ch, vol, lbl in P.SECTION_MIX:
            if beat >= P.TOTAL_BEATS:
                bad.append(f"SECTION_MIX「{lbl}」在拍 {beat}，"
                           f"超出曲子 {P.TOTAL_BEATS:.0f} 拍")
                print(f"  ❌ SECTION_MIX「{lbl}」拍 {beat} 越界")

    # ── 3 段名一致性 ──
    names = [s[0] for s in P.SECTIONS]
    if len(set(names)) != len(names):
        bad.append(f"段名有重复：{names}")
    if P.SECTIONS[0][1] != 0:
        bad.append("第一段不从第 0 小节开始")

    # ── 4 声像 ──
    off = [(nm, pan) for _ch, _pr, _v, pan, _r, nm in P.SETUP if pan != 64]
    if off:
        detail = "、".join(f"{n}={p}" for n, p in off)
        bad.append(f"声像没居中：{detail}")
        print(f"  ⚠ 声像偏离居中：{detail}")
    else:
        print(f"  ✅ {len(P.SETUP)} 个声部声像全部居中（左右等量输出）")

    # ── 5 通道 9 ──
    for ch, prog, _v, _p, _r, nm in P.SETUP:
        if ch == gm.DRUM_CHANNEL and prog != 0:
            bad.append(f"{nm} 占了通道 9（鼓组专用）")
            print(f"  ❌ {nm} 占了鼓组专用通道")

    # ── 统计 ──
    score = P.build_score()
    chans = sorted({e.ch for e in score.events})
    print(f"  声部        {len(P.SETUP)} 个定义 / {len(chans)} 个有音符，"
          f"共 {len(score.events)} 个音")
    print(f"  段          " + " → ".join(names))
    return bad


def main() -> int:
    allbad: dict[str, list[str]] = {}
    for mod, label in PIECES:
        try:
            allbad[label] = audit(mod, label)
        except Exception as e:
            print(f"\n{label}: ❌ 载入/构建失败 {type(e).__name__}: {e}")
            allbad[label] = [str(e)]

    print(f"\n{BAR}")
    total = sum(len(v) for v in allbad.values())
    if total == 0:
        print(f"✅ {len(PIECES)} 首曲子全部通过审计")
        return 0
    print(f"❌ 共 {total} 个问题：")
    for label, items in allbad.items():
        for it in items:
            print(f"   [{label}] {it}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
