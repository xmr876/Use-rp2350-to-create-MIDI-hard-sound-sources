#!/usr/bin/env python3
"""
ps5_make_smf.py — 生成 3 分钟的 .MID 文件，给 Pocketstudio 5 自己播放。

为什么要走 SMF 而不是一直用 USB-MIDI 实时推：
  手册原文 —— "You can also play MIDI directly into the Pocketstudio 5
  from a keyboard or a sequencer, using the internal tone generator.
  However, you cannot record MIDI sequences on the Pocketstudio 5
  using this setup."
  PS5 **不能把收到的 MIDI 录成曲子**；它唯一能自己播放 MIDI 的方式，
  是读卡上 SMF 文件夹里的 .MID 文件。所以想要"它自己会放"，
  就必须生成 SMF。

拷进卡的步骤见 docs/Pocketstudio5_设置与播放.md，要点：
  · 文件放进卡根目录的 **SMF** 文件夹
  · 文件名必须 **8.3 格式**（<=8 字符）+ 扩展名 **.MID**
  · PS5 按住 ENTER 开机 -> USB MODE -> 插 USB 线，它就变成外置磁盘

用法：
    python tools/ps5_make_smf.py                     # 写到 build/SMF/PS5TUNE.MID
    python tools/ps5_make_smf.py -o "D:\\SMF\\MYTUNE.MID"
    python tools/ps5_make_smf.py --verify            # 写完再解析回来验证
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

sys.path.insert(0, r"D:\音源\tools")
sys.path.insert(0, r"D:\音源")

from ps5 import smf               # noqa: E402

# 曲子在 main() 里按 --piece 载入
P = None

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def check_83(path: str) -> tuple[bool, str]:
    """PS5 要求 8.3 文件名。"""
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    msgs = []
    ok = True
    if len(stem) > 8:
        ok = False
        msgs.append(f"主名 {len(stem)} 字符 > 8，PS5 会截断成 {stem[:6]}~1")
    if ext.upper() != ".MID":
        ok = False
        msgs.append(f"扩展名是 {ext}，PS5 只认 .MID")
    if " " in base:
        ok = False
        msgs.append("文件名里有空格，PS5 会去掉空格")
    return ok, "；".join(msgs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成给 Pocketstudio 5 播放的 SMF")
    ap.add_argument("--piece", default="piece",
                    help="piece / jrock / serene / solister")
    ap.add_argument("-o", "--out", default=None,
                    help="输出路径（默认 build/SMF/<曲子名>.MID）")
    ap.add_argument("--verify", action="store_true", help="写完后解析回来验证合法性")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    global P
    P = importlib.import_module(f"ps5.{args.piece}")
    if args.out is None:
        # PS5 要求 8.3 文件名 + .MID
        args.out = os.path.join(
            r"D:\音源\build\SMF",
            {"piece": "PS5TUNE.MID",
             "jrock": "JROCK.MID",
             "serene": "SERENE.MID",
             "solister": "SOLISTR.MID",
             "jpop": "JPOP.MID"}.get(args.piece, "PS5TUNE.MID"))

    evs = P.live_events()
    notes = [e for e in evs if not e[2] and e[1][0] & 0xF0 == 0x90 and e[1][2] > 0]
    print(f"=== {args.piece} · 统计 ===")
    print(f"  曲长      {P.DURATION_SEC:.1f} 秒 = "
          f"{P.DURATION_SEC // 60:.0f} 分 {P.DURATION_SEC % 60:.0f} 秒")
    print(f"  小节/速度 {P.TOTAL_BARS} 小节 @ {P.BPM} BPM")
    print(f"  音符事件  {len(notes)} 个 Note On")
    print(f"  MIDI 消息 {len(evs)} 条")
    print("  段        " + " / ".join(f"{n}({b0 + 1}-{b1})" for n, b0, b1, _ in P.SECTIONS))

    if args.stats:
        return 0

    ok, why = check_83(args.out)
    if not ok:
        print(f"\n  ⚠ 文件名不符合 8.3：{why}")
        print("     → PS5 会自己改名，可能跟卡上别的曲子撞名，建议改短")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    P.write_midi_file(args.out)
    size = os.path.getsize(args.out)
    print(f"\n  已写出 {args.out}  （{size:,} 字节）")

    if args.verify:
        info = smf.read_smf(args.out)
        print("\n=== 解析回来验证 ===")
        print(f"  format={info['format']}  轨数={info['ntracks']}  "
              f"分辨率={info['division']} ticks/四分音符")
        print(f"  事件总数={info['events']}  末拍={info['end_beat']:.1f} "
              f"→ {info['end_beat'] * 60 / P.BPM:.1f} 秒")
        for i, t in enumerate(info["tracks"]):
            kinds = {}
            for ev in t:
                # ev[1] 可能是 "meta"/"sysex" 字符串，也可能是状态字节整数
                k = ev[1] if isinstance(ev[1], str) else f"0x{ev[1]:02x}"
                kinds[k] = kinds.get(k, 0) + 1
            print(f"    轨{i}: {len(t):>5} 事件  {kinds}")
        assert info["format"] == 1, "应该是 Format 1"
        # 容差按"拍"给，不按秒：SMF 的 end_beat 包含最后一小节长音的收尾，
        # 而 DURATION_SEC 只算到最后一小节的开头，两者天然会差一个音的长度。
        # 按秒卡 1.0 会在慢速曲子上误报（66 BPM 下一拍就是 0.9 秒）。
        actual = info["end_beat"] * 60 / P.BPM
        tol_beats = 4.0
        drift = abs(info["end_beat"] - P.TOTAL_BEATS)
        assert drift <= tol_beats, (
            f"结束时间对不上：文件末拍 {info['end_beat']:.2f}，"
            f"曲子应有 {P.TOTAL_BEATS:.0f} 拍，差 {drift:.2f} 拍"
            f"（容差 {tol_beats} 拍）；换算秒数 {actual:.1f}s vs "
            f"{P.DURATION_SEC:.1f}s")
        print("  ✅ 结构自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
