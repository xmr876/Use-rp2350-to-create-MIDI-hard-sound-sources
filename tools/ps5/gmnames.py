#!/usr/bin/env python3
"""
gmnames.py — General MIDI 音色名表 + Pocketstudio 5 的 MIDI 常量。

PS5 的音源是标准的 128 GM 音色 + 鼓组，见 Reference Manual
「5 – Standard MIDI files and the Pocketstudio 5」一节：
  "there are 128 instruments and 5 drumkits ... corresponding to the
   settings defined in the General MIDI list"
"""
from __future__ import annotations

# ── GM 128 音色（0-127）────────────────────────────────────────────────
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

# 常用音色号，写曲时用名字比用数字清楚
PIANO = 0            # Acoustic Grand Piano
EPIANO = 4           # Electric Piano 1
NYLON_GTR = 24       # Acoustic Guitar (nylon)
STEEL_GTR = 25       # Acoustic Guitar (steel)
FINGER_BASS = 33     # Electric Bass (finger)
PICK_BASS = 34       # Electric Bass (pick)
STRINGS = 48         # String Ensemble 1
SLOW_STRINGS = 51    # SynthStrings 2
CHOIR = 52           # Choir Aahs
BRASS = 61           # Brass Section
MUTED_TRUMPET = 59
ALTO_SAX = 65
FLUTE = 73
WARM_PAD = 89        # Pad 2 (warm)
NEWAGE_PAD = 88      # Pad 1 (new age)
MARIMBA = 12         # Marimba
HARP = 46            # Orchestral Harp（竖琴）
GLOCKEN = 9          # Glockenspiel（钟琴/管钟）
LOW_STRINGS = 43     # Contrabass（低音弦乐）

# ── 通道分配（PS5 是 16 声部多音色，通道 10 只能放鼓组）──────────────
# 手册："you can only assign drum kits to part 10 (traditionally reserved
#        for drums)"
#
# 注意每个通道是「一个声部 = 一个音色」，所以同一个通道里不能混两种乐器。
# 特别是**铺底不要跟旋律共用通道**：同音高重叠时会互相抢音。
#
# 通道 9（0 起算）= 实际通道 10 = 鼓组专用，**任何曲子的任何乐器都不能占用**。
# 没有鼓的曲子（比如 serene）就让通道 10 空着。
CH_PIANO = 0
CH_BASS = 1
CH_STRINGS = 2        # 弦乐
CH_GTR = 3            # 吉他（piece 用木吉他分解，jrock 用失真强力和弦）
CH_CLEAN = CH_GTR     # 别名：jrock 里"清音吉他"借用同一个通道（换段切音色）
CH_LEAD = 4           # 主奏（小号 / 长笛 / 主音合成器）
CH_BRASS = 5          # 铜管组
CH_CHOIR = 6          # 合唱
CH_MARIMBA = 7        # 马林巴
CH_PAD = 8            # 暖垫
# ── 9 是鼓组专用，绝对不能用 ──
CH_CHIME = 10         # 钟琴（serene 用）
CH_HARP = 11          # 竖琴（serene 用）
CH_LOW = 12           # 低音弦乐（serene 用）
CH_EPIANO = 13        # 电钢（solister / jpop 用）
CH_TROMBONE = 14      # 长号 / 上低音号（solister 用）
CH_SYNTHPAD = 15      # 合成弦乐垫（jpop 用，16 复音的主力铺底层）

DRUM_CHANNEL = 9      # 0 起算 → 实际通道 10
CH_DRUMS = DRUM_CHANNEL   # 别名，写曲时读起来顺一点

# ── GM 打击乐音高（通道 10）──────────────────────────────────────────
KICK = 36
SNARE = 38
CLAP = 39
SNARE2 = 40
LOW_TOM = 41
CLOSED_HAT = 42
PEDAL_HAT = 44
OPEN_HAT = 46
HIGH_TOM = 48
CRASH = 49
RIDE = 51
RIDE_BELL = 53
TAMBOURINE = 54
COWBELL = 56

# ── PS5 音源参数范围（手册 "Setting part parameters in the SMF"）──────
LEVEL_MIN, LEVEL_MAX = 0, 127
CHO_SEND_MAX = 127
REV_SEND_MAX = 127
KEYTRANS_MIN, KEYTRANS_MAX = -36, 36
SUPPORTED_DRUMKITS = 5


def name(prog: int) -> str:
    if 0 <= prog < len(GM_NAMES):
        return GM_NAMES[prog]
    return f"Program {prog}"
