#!/usr/bin/env python3
"""
ps5_play.py — 把 3 分钟曲子实时推给 TASCAM Pocketstudio 5 的内藏音源。

链路：
    PC  ──USB──▶ USB 转 MIDI 线（MIDI OUT 那头）──5 针 DIN──▶ PS5 的 MIDI IN
    PS5 内藏 GM 音源发声 ──▶ LINE OUT / PHONES 出声

PS5 侧要做的设置见 docs/Pocketstudio5_设置与播放.md；最要紧的两条：
    1. 线的 **MIDI OUT** 插头 → PS5 的 **MIDI IN** 插座
    2. TG（音源）推子拉起来，MASTER 推子拉起来

用法：
    python tools/ps5_play.py --list                 # 看有哪些 MIDI 输出设备
    python tools/ps5_play.py --selftest             # 先出声确认链路通
    python tools/ps5_play.py --dry-run              # 只看时间表，不发 MIDI
    python tools/ps5_play.py                        # 完整 3 分 12 秒
    python tools/ps5_play.py --from A --to B        # 只放 A 到 B 段
    python tools/ps5_play.py --device 1             # 指定设备编号
    python tools/ps5_play.py --all-devices          # 同时往所有非微软设备发
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time

sys.path.insert(0, r"D:\音源\tools")
sys.path.insert(0, r"D:\音源")

from ps5.midiwin import (MidiOut, list_devices, pick_device,  # noqa: E402
                         usb_devices, open_devices)
from ps5 import gmnames as gm                                            # noqa: E402

# 曲子在 main() 里按 --piece 动态载入
P = None

# 不要重新包 sys.stdout —— 会让底层 buffer 提前被回收（老 bug）
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def fmt_time(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def section_timeline() -> list[tuple[str, float, float, str]]:
    """(段名, 起始秒, 结束秒, 说明)，含 1.2 秒前导。"""
    spb = 60.0 / P.BPM
    lead = 1.2
    out = []
    for name, b0, b1, desc in P.SECTIONS:
        out.append((name, b0 * P.BEATS_PER_BAR * spb + lead,
                    b1 * P.BEATS_PER_BAR * spb + lead, desc))
    return out


def print_timeline() -> None:
    print("  段      起始      结束      说明")
    for name, t0, t1, desc in section_timeline():
        print(f"  {name:<7}{fmt_time(t0):>8}  {fmt_time(t1):>8}   {desc}")
    print(f"  {'纯曲长':<7}{'':>8}  {fmt_time(P.DURATION_SEC):>8}   "
          f"{P.TOTAL_BARS} 小节 @ {P.BPM} BPM ≈ "
          f"{P.DURATION_SEC // 60:.0f} 分 {P.DURATION_SEC % 60:.0f} 秒")
    print(f"  {'含前后缀':<6}{fmt_time(0):>8}  {fmt_time(P.DURATION_SEC + 3.7):>8}   "
          f"1.2 秒前导 + 收尾的余韵")


# ── 自检：确认 USB-MIDI 线和 PS5 音源这条链路是通的 ────────────────────
def selftest(m: MidiOut, name: str) -> None:
    print(f"\n=== 自检：往 [{name}] 发几个明确的测试信号 ===")
    print("  每一步都告诉你**应该听到/看到什么**，对不上就是链路问题。\n")

    print("  1) GM System On + 各声部初始化（PS5 屏幕如果停在 TG 页，"
          "各声部会闪一下）")
    m.sysex((0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7))
    time.sleep(0.3)

    print("  2) 通道 1 钢琴（Acoustic Grand Piano）—— 应该听到清亮的钢琴 C 大调音阶")
    m.program(gm.CH_PIANO, gm.PIANO)
    m.cc(gm.CH_PIANO, 7, 110)
    m.cc(gm.CH_PIANO, 10, 64)
    for n in (60, 62, 64, 65, 67, 69, 71, 72):
        m.note_on(gm.CH_PIANO, n, 100)
        time.sleep(0.32)
        m.note_off(gm.CH_PIANO, n)
        time.sleep(0.05)
    time.sleep(0.4)

    print("  3) 通道 10 鼓组 —— 应该听到 底鼓/军鼓/踩镲 的节奏")
    m.program(gm.DRUM_CHANNEL, 0)
    m.cc(gm.DRUM_CHANNEL, 7, 105)
    for _ in range(2):
        for step in range(8):
            if step % 4 == 0:
                m.note_on(gm.DRUM_CHANNEL, gm.KICK, 110)
            if step % 4 == 2:
                m.note_on(gm.DRUM_CHANNEL, gm.SNARE, 100)
            m.note_on(gm.DRUM_CHANNEL, gm.CLOSED_HAT, 65)
            time.sleep(0.14)
    time.sleep(0.3)

    print(f"  4) 换音色对比 —— 同一句旋律，听音色有没有变：")
    for prog, label in ((gm.STRINGS, "String Ensemble 1 弦乐"),
                        (gm.MARIMBA, "Marimba 马林巴"),
                        (gm.PIANO, "Acoustic Grand Piano 钢琴")):
        print(f"     → Program Change {prog:3d}  {label}")
        m.program(gm.CH_PIANO, prog)
        time.sleep(0.2)
        for n in (60, 64, 67):
            m.note_on(gm.CH_PIANO, n, 95)
        time.sleep(1.4)
        for n in (60, 64, 67):
            m.note_off(gm.CH_PIANO, n)
        time.sleep(0.3)

    print("\n  ✅ 如果上面每一步都听到了 → 链路没问题，直接跑完整曲子：")
    print("       python tools/ps5_play.py")
    print("  ❌ 完全没声音 → 按这个顺序查：")
    print("       ① 线两个 DIN 头对调（廉价线的 IN/OUT 丝印经常是反的）")
    print("       ② 确认插的是 PS5 的 MIDI IN，不是 MIDI OUT")
    print("       ③ PS5 的 TG 推子和 MASTER 推子拉起来，耳机/线路输出接对")
    print("       ④ 用 --device 2 换另一个端口试（这类线常枚举出两个输出口）")


# ── 发送 ──────────────────────────────────────────────────────────────
def send_raw(m: MidiOut, msg: tuple[int, ...]) -> None:
    st = msg[0]
    if st == 0xF0:
        m.sysex(msg)
        return
    hi = st & 0xF0
    if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
        m.send(st, msg[1], msg[2])
    elif hi in (0xC0, 0xD0):
        m.send(st, msg[1], 0)
    else:
        # 未知状态字节：按三字节发，别静默吞掉
        m.send(st, msg[1] if len(msg) > 1 else 0, msg[2] if len(msg) > 2 else 0)


def play(targets, evs, offset: float) -> None:
    print(f"\n=== 开始播放（{len(evs)} 个事件，实时 {fmt_time(evs[-1][0] - offset)}）===")
    tl = section_timeline()
    announced: set[str] = set()
    t_start = time.perf_counter()
    last_report = 0.0

    try:
        for t, msg, label in evs:
            now = time.perf_counter() - t_start + offset
            wait = t - now
            if wait > 0.0005:
                time.sleep(wait)

            # 段落提示
            for name, s0, s1, desc in tl:
                if s0 <= t < s1 and name not in announced:
                    announced.add(name)
                    print(f"\n▶ {name}  {desc}   [{fmt_time(t)}]")

            if label:
                print(f"    · {label}  [{fmt_time(t)}]")

            for m in targets:
                send_raw(m, msg)

            # 进度
            if t - last_report >= 10.0:
                last_report = t
                print(f"    … {fmt_time(t)} / {fmt_time(evs[-1][0])}")
    except KeyboardInterrupt:
        print("\n\n收到中断，停在这里。")
    finally:
        for m in targets:
            m.all_notes_off()
        print(f"\n已结束，耗时 {fmt_time(time.perf_counter() - t_start)}。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把曲子实时推给 TASCAM Pocketstudio 5",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piece", default="piece",
                    help="piece（流行曲，3分20秒）/ jrock（日摇，2分27秒）")
    ap.add_argument("--list", action="store_true", help="只列出 MIDI 输出设备")
    ap.add_argument("--device", type=int, default=None, help="MIDI 输出设备编号")
    ap.add_argument("--all-devices", action="store_true",
                    help="同时往所有非微软设备发送（排除发错端口的可能）")
    ap.add_argument("--selftest", action="store_true", help="只做链路自检")
    ap.add_argument("--dry-run", action="store_true", help="只打印时间表，不发 MIDI")
    ap.add_argument("--from", dest="start", default=None,
                    help="从哪段开始（段名或数字，默认第一段）")
    ap.add_argument("--to", dest="end", default=None,
                    help="到哪段结束（含，段名或数字，默认最后一段）")
    args = ap.parse_args(argv)

    global P
    P = importlib.import_module(f"ps5.{args.piece}")
    label = {"piece": "流行曲", "jrock": "日摇 J-ROCK"}.get(args.piece, args.piece)

    print(f"=== TASCAM Pocketstudio 5 · {label} ===")
    print(f"  {P.TOTAL_BARS} 小节，{P.BPM} BPM，"
          f"{P.DURATION_SEC:.0f} 秒（{P.DURATION_SEC // 60:.0f} 分 "
          f"{P.DURATION_SEC % 60:.0f} 秒）")
    print_timeline()
    print()

    if args.list:
        print("=== MIDI 输出设备 ===")
        for i, n in list_devices():
            tag = "  ← 微软自带，不是你的 USB-MIDI" if (
                "Microsoft" in n or "GS Wavetable" in n) else "  ← USB-MIDI"
            print(f"  [{i}] {n}{tag}")
        return 0

    evs_all = P.live_events()

    # 选段
    names = [s[0] for s in P.SECTIONS]
    def resolve(x: str) -> int:
        if x.isdigit():
            return max(0, min(len(names) - 1, int(x) - 1))
        up = x.upper()
        if up not in names:
            raise SystemExit(f"没有叫 {x} 的段。可选：{', '.join(names)}")
        return names.index(up)

    i0 = resolve(args.start) if args.start is not None else 0
    i1 = resolve(args.end) if args.end is not None else len(names) - 1
    if i0 > i1:
        i0, i1 = i1, i0
    spb = 60.0 / P.BPM
    lead = 1.2
    t_lo = P.SECTIONS[i0][1] * P.BEATS_PER_BAR * spb + lead
    t_hi = P.SECTIONS[i1][2] * P.BEATS_PER_BAR * spb + lead

    if args.dry_run:
        print(f"=== 干跑：{names[i0]} → {names[i1]}，"
              f"{fmt_time(t_lo)} ~ {fmt_time(t_hi)} ===")
        for t, msg, label in evs_all:
            if t_lo - 2.0 <= t <= t_hi:
                tag = f"   # {label}" if label else ""
                print(f"  {fmt_time(t):>9}  {bytes(msg).hex(' ')}{tag}")
        print(f"\n共 {sum(1 for t, _, _ in evs_all if t_lo - 2.0 <= t <= t_hi)} 个事件")
        return 0

    # 打开设备（带重试 —— 上一次播放的残留进程会短暂占住 winmm 句柄，
    # 表现为"两个端口只打开一个"，只有一路出声）
    targets, name = open_devices(args.all_devices, args.device)

    try:
        if args.selftest:
            selftest(targets[0], name)
            return 0

        # 只放一段时，前面补一套初始化，保证音色/音量是对的。
        # 切片逻辑统一放在 piece.slice_events（和验证脚本共用一份），
        # 它会保证片段收尾时每个音都配上 note-off —— 不会留下挂音。
        kept, offset = P.slice_events(i0, i1)
        play(targets, kept, offset)
    finally:
        for m in targets:
            m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
