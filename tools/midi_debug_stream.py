#!/usr/bin/env python3
"""
midi_debug_stream.py — 通过 USB 转 MIDI 线持续发送变化的 MIDI，用来调试音源

不需要装任何库（直接用 Windows 的 winmm）。

用法：
    python tools/midi_debug_stream.py                    # 自动选 USB 转 MIDI 设备
    python tools/midi_debug_stream.py --device 1
    python tools/midi_debug_stream.py --mode programs    # 只测换音色
    python tools/midi_debug_stream.py --mode all         # 轮流跑所有测试

模式：
    programs   换音色巡游 0→127：**光看数码管就能判断 MIDI 通没通**
    scale      C 大调音阶（钢琴）
    chromatic  128 个半音全扫：测全音域音高
    chords     I-IV-V-I 三和弦：测复音
    drums      鼓组节奏（通道 10）
    polyphony  逐步叠加到 16 个音：测复音上限
    all        轮流跑上面全部

★ 接线要点：USB 转 MIDI 线上有 MIDI IN / MIDI OUT 两个 DIN 头，
  要把 **MIDI OUT** 那个接到音源板的 **MIDI IN** 插座上。
  ★ 这类廉价线的 IN/OUT 丝印经常是反的 —— 如果完全没反应，
    先把两个 DIN 头对调试一下，再怀疑电路。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
import io
from ctypes import wintypes

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

winmm = ctypes.WinDLL("winmm")

# ──────────────────────────────────────────────────────────────────────
# winmm MIDI 输出封装
# ──────────────────────────────────────────────────────────────────────
class MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
        ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


def list_devices() -> list[tuple[int, str]]:
    out = []
    for i in range(winmm.midiOutGetNumDevs()):
        caps = MIDIOUTCAPS()
        if winmm.midiOutGetDevCapsW(i, ctypes.byref(caps),
                                    ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname))
    return out


def pick_device(want: int | None) -> tuple[int, str]:
    devs = list_devices()
    if want is not None:
        for i, n in devs:
            if i == want:
                return i, n
        raise SystemExit(f"错误：没有编号 {want} 的 MIDI 设备")
    # 优先挑 USB 转 MIDI（排除微软自带合成器）
    for i, n in devs:
        if "Microsoft" not in n and "GS Wavetable" not in n:
            return i, n
    raise SystemExit("错误：只找到微软自带合成器，没看到 USB 转 MIDI 设备")


class MidiOut:
    def __init__(self, dev_id: int):
        self.h = wintypes.HANDLE()
        r = winmm.midiOutOpen(ctypes.byref(self.h), dev_id, 0, 0, 0)
        if r != 0:
            raise SystemExit(f"打不开 MIDI 设备 {dev_id}（错误码 {r}）")

    def send(self, status: int, d1: int, d2: int) -> None:
        msg = (d2 << 16) | (d1 << 8) | status
        winmm.midiOutShortMsg(self.h, ctypes.c_ulong(msg))

    # --- 常用消息 ---
    def note_on(self, ch, note, vel=100):
        self.send(0x90 | ch, note & 127, vel & 127)

    def note_off(self, ch, note):
        self.send(0x80 | ch, note & 127, 0)

    def program(self, ch, prog):
        self.send(0xC0 | ch, prog & 127, 0)

    def cc(self, ch, num, val):
        self.send(0xB0 | ch, num & 127, val & 127)

    def all_notes_off(self, ch=None):
        for c in range(16):
            if ch is None or c == ch:
                self.send(0xB0 | c, 123, 0)
                self.send(0xB0 | c, 120, 0)

    def close(self):
        try:
            self.all_notes_off()
        except Exception:
            pass
        winmm.midiOutClose(self.h)


# ──────────────────────────────────────────────────────────────────────
# 各种测试序列
# ──────────────────────────────────────────────────────────────────────
GM_NAMES = [
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano",
    "Honky-tonk Piano", "Electric Piano 1", "Electric Piano 2", "Harpsichord",
    "Clavinet", "Celesta", "Glockenspiel", "Music Box", "Vibraphone",
    "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ",
    "Reed Organ", "Accordion", "Harmonica", "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)",
    "Electric Guitar (clean)", "Electric Guitar (muted)", "Overdriven Guitar",
    "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)",
    "Fretless Bass", "Slap Bass 1", "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings",
    "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "SynthStrings 1", "SynthStrings 2",
    "Choir Aahs", "Voice Oohs", "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn",
    "Brass Section", "SynthBrass 1", "SynthBrass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe",
    "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle",
    "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)",
    "Lead 5 (charang)", "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass+lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)",
    "Pad 5 (bowed)", "Pad 6 (metallic)", "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)",
    "FX 5 (brightness)", "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bag pipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum",
    "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet",
    "Telephone Ring", "Helicopter", "Applause", "Gunshot",
]


def mode_programs(m: MidiOut) -> None:
    """换音色巡游 —— 数码管上的数字应该跟着变"""
    print("  【换音色巡游】数码管应该从 00 数到 99 再数回来")
    print("    ★ 固件的 ui_display_number 只支持 0~99；程序号 >=100 时")
    print("      数码管会显示两个中间横杠（UI_SEG_MINUS）—— 那不是故障，")
    print("      反而是 MIDI 通了的证据。这里只扫 0~99 保证可读。")
    for prog in list(range(0, 100, 1)) + list(range(99, -1, -1)):
        m.program(0, prog)
        name = GM_NAMES[prog] if prog < len(GM_NAMES) else "?"
        print(f"    音色 {prog:3d}  {name}")
        # 每换一个音色弹一下，听音色变化
        m.note_on(0, 60, 90)
        m.note_on(0, 64, 90)
        m.note_on(0, 67, 90)
        time.sleep(0.55)
        m.note_off(0, 60); m.note_off(0, 64); m.note_off(0, 67)
        time.sleep(0.15)


def mode_scale(m: MidiOut) -> None:
    print("  【C 大调音阶】钢琴，上行再下行")
    m.program(0, 0)
    time.sleep(0.1)
    scale = [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65, 64, 62, 60]
    for n in scale:
        print(f"    note {n}")
        m.note_on(0, n, 100)
        time.sleep(0.45)
        m.note_off(0, n)
        time.sleep(0.08)
    time.sleep(0.6)


def mode_chromatic(m: MidiOut) -> None:
    print("  【半音全扫 0..127】测全音域音高（低音到高音）")
    m.program(0, 0)
    time.sleep(0.1)
    for n in range(0, 128, 2):
        m.note_on(0, n, 95)
        time.sleep(0.16)
        m.note_off(0, n)
    for n in range(120, -1, -2):
        m.note_on(0, n, 95)
        time.sleep(0.16)
        m.note_off(0, n)
    time.sleep(0.5)


def mode_chords(m: MidiOut) -> None:
    print("  【和弦 I-IV-V-I】测复音（同时 3~4 个音）")
    m.program(0, 0)
    time.sleep(0.1)
    prog = [
        ("C",  [60, 64, 67, 72]),
        ("F",  [65, 69, 72, 77]),
        ("G",  [67, 71, 74, 79]),
        ("C",  [60, 64, 67, 72]),
    ]
    for name, notes in prog:
        print(f"    和弦 {name}: {notes}")
        for n in notes:
            m.note_on(0, n, 90)
        time.sleep(1.2)
        for n in notes:
            m.note_off(0, n)
        time.sleep(0.25)


def mode_polyphony(m: MidiOut) -> None:
    print("  【复音叠加】从 1 个音加到 16 个音（48 复音上限内）")
    m.program(0, 0)
    time.sleep(0.1)
    for k in range(1, 17):
        m.note_on(0, 47 + k, 90)
        print(f"    同时 {k:2d} 个音")
        time.sleep(0.5)
    time.sleep(1.0)
    for k in range(1, 17):
        m.note_off(0, 47 + k)
    time.sleep(0.8)


def mode_drums(m: MidiOut) -> None:
    print("  【鼓组】通道 10，基础节奏（kick/snare/hihat）")
    KICK, SNARE, HIHAT, OPENHAT, CRASH = 36, 38, 42, 46, 49
    for bar in range(8):
        for step in range(8):
            if step % 4 == 0:
                m.note_on(9, KICK, 110)
            if step % 4 == 2:
                m.note_on(9, SNARE, 100)
            m.note_on(9, HIHAT if step % 2 == 0 else OPENHAT, 70)
            time.sleep(0.125)
        if bar % 4 == 3:
            m.note_on(9, CRASH, 110)
        print(f"    第 {bar + 1} 小节")
    time.sleep(0.6)


def mode_hold(m: MidiOut) -> None:
    """一直按住一个和弦，让 I2S 上**始终有数据在跑**。

    ★ 这个模式是专门给"拿万用表查线"用的。
      别的模式音符断断续续，万用表读数会跳；
      这里持续发声，BCK/LRCK 上一直是活跃波形，
      直流档量出来是稳定的平均值，一眼就能看出 PIO 在不在跑。

      判据（3.3V 逻辑，PIO 正常翻转时）：
        BCK  占空比 30%  → 直流约 1.0V
        LRCK 占空比 50%  → 直流约 1.65V
        DIN  随数据变化  → 直流约 1.6V 上下
      如果量到 0V 或 3.3V 纹丝不动 → PIO 没在跑（固件问题）
      如果三个脚都正常 → 问题在 PCM5102 那一侧（接线或模块）
    """
    print("  【持续按住】一直响一个 C 和弦 —— 拿万用表量波形用")
    print("    量 BCK  对 GND：应约 1.0V（30% 占空）")
    print("    量 LRCK 对 GND：应约 1.65V")
    print("    量 DIN  对 GND：应在 1.6V 附近")
    print("    → 三个都正常 = PIO 在跑，问题在 PCM5102 侧")
    m.program(0, 0)
    time.sleep(0.1)
    notes = [48, 55, 60, 64, 67, 72]
    round_no = 0
    while True:
        round_no += 1
        for n in notes:
            m.note_on(0, n, 95)
        print(f"    第 {round_no} 次触发 —— 现在可以量了")
        time.sleep(8.0)          # 长音保持 8 秒，够量几个点
        for n in notes:
            m.note_off(0, n)
        time.sleep(0.05)


MODES = {
    "programs": mode_programs,
    "scale": mode_scale,
    "chromatic": mode_chromatic,
    "chords": mode_chords,
    "polyphony": mode_polyphony,
    "drums": mode_drums,
    "hold": mode_hold,
}
ORDER = ["programs", "scale", "chords", "drums", "chromatic", "polyphony"]


# ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="持续发送变化的 MIDI 用于调试")
    ap.add_argument("--device", type=int, default=None, help="MIDI 设备编号")
    ap.add_argument("--mode", default="all",
                    choices=["all"] + ORDER + ["hold"], help="测什么")
    ap.add_argument("--rounds", type=int, default=0,
                    help="跑几轮（0 = 一直跑）")
    args = ap.parse_args(argv)

    dev, name = pick_device(args.device)
    print(f"=== MIDI 调试流 ===")
    print(f"  设备 [{dev}] {name}")
    print(f"  模式 {args.mode}")
    print()
    print("  ★ 接线：USB 转 MIDI 线的 **MIDI OUT** 头 → 音源板的 MIDI IN 插座")
    print("  ★ 这类线的 IN/OUT 丝印常常是反的，没反应就先把两个头对调")
    print()

    m = MidiOut(dev)
    print(f"  已打开，开始发送…（Ctrl+C 或停掉任务即结束）")
    print()

    try:
        rnd = 0
        while args.rounds == 0 or rnd < args.rounds:
            rnd += 1
            modes = ORDER if args.mode == "all" else [args.mode]
            for name_ in modes:
                print(f"── 第 {rnd} 轮 / {name_} ──")
                MODES[name_](m)
    except KeyboardInterrupt:
        print("\n  收到中断")
    finally:
        m.close()
        print("  已关闭 MIDI 设备，所有音符已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
