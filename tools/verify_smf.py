#!/usr/bin/env python3
"""verify_smf.py — 检查 build/SMF 里所有 .MID 是否符合 PS5 的要求。

自动发现目录里的文件，所以新加曲子不用改这个脚本。
时长用 SMF 自己的速度事件换算（而不是猜 BPM）。
"""
from __future__ import annotations

import glob
import os
import struct
import sys

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

from ps5 import smf  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

SMF_DIR = r"D:\音源\build\SMF"


def tempo_of(info: dict) -> float:
    """从第一轨的 0x51 速度事件读 BPM；读不到就按 120。"""
    for tick, kind, mt, data in info["tracks"][0]:
        if kind == "meta" and mt == 0x51 and len(data) == 3:
            us = struct.unpack(">I", b"\x00" + bytes(data))[0]
            return 60_000_000.0 / us if us else 120.0
    return 120.0


def main() -> int:
    files = sorted(glob.glob(os.path.join(SMF_DIR, "*.MID")))
    if not files:
        print(f"❌ {SMF_DIR} 里没有 .MID 文件")
        return 1

    print(f"{'文件':<15}{'格式':<6}{'轨数':<6}{'事件':<8}"
          f"{'BPM':<6}{'时长':<12}{'GM复位':<8}{'8.3':<5}{'大小'}")
    print("-" * 82)
    rc = 0
    for path in files:
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]
        info = smf.read_smf(path)
        bpm = tempo_of(info)
        secs = info["end_beat"] * 60.0 / bpm
        sysex = any(e[1] == "sysex" for e in info["tracks"][0])
        name_ok = len(stem) <= 8 and " " not in base and base.upper().endswith(".MID")
        size = os.path.getsize(path)

        # 时长格式化：先四舍五入到 0.1 秒再拆分，否则 119.96 秒会
        # 显示成 "1:60.0" 而不是 "2:00.0"
        tenths = round(secs * 10)
        mins, rem = divmod(tenths, 600)
        dur = f"{mins}:{rem / 10:04.1f}"
        flags = []
        if not sysex:
            flags.append("缺 GM 复位")
            rc = 1
        if not name_ok:
            flags.append("文件名不合 8.3")
            rc = 1
        empty = [i for i, t in enumerate(info["tracks"]) if len(t) <= 2]
        if empty:
            flags.append(f"空轨 {empty}")
            rc = 1

        print(f"{base:<15}{info['format']:<6}{info['ntracks']:<6}"
              f"{info['events']:<8}{bpm:<6.0f}{dur:<12}"
              f"{'有' if sysex else '缺':<8}{'✓' if name_ok else '✗':<5}"
              f"{size / 1024:.1f} KB"
              + (f"   {'; '.join(flags)}" if flags else ""))

    print("-" * 82)
    print(f"✅ {len(files)} 个文件全部符合 PS5 的要求" if rc == 0
          else "❌ 有文件不符合，见上")
    print()
    print("放进卡里：")
    print("  · 拖到卡根目录的 SMF 文件夹（放别处 PS5 看不见）")
    print("  · PS5 上：按住 ENTER 开机 → USB MODE → 插 USB 线 → 当外置磁盘")
    print("  · 加载：主菜单 → CARD → SMF LOAD → 选文件 → ENTER")
    return rc


if __name__ == "__main__":
    sys.exit(main())
