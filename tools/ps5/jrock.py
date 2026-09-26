#!/usr/bin/env python3
"""
jrock.py — 日摇（J-Rock）风格测试曲，给 TASCAM Pocketstudio 5 的内藏 GM 音源。

和 piece.py 接口完全一致（同样的 SECTIONS / live_events / slice_events /
write_midi_file），所以播放器、验证脚本、钢琴卷帘图都能直接套用。

日摇这个曲风的几个标志性做法，这里都照着做了：

  1. **主调 Am，副歌转 C 大调**（关系大调）—— 段落之间靠调性色彩拉开对比
  2. **最后一遍副歌整体升全音**（C→D）—— 日摇最典型的煽情手法
  3. **失真吉他只用强力和弦**（根音 + 五度 + 八度，**不加三度**）：
     失真下叠三度会糊成一团，这是摇滚编曲的基本常识
  4. **八分音符驱动贝斯** —— 日摇的推进力八成来自贝斯而不是吉他
  5. **副歌切到 16 分踩镲 + 反拍底鼓** —— 密度直接翻倍做出"开"的感觉
  6. **间奏是 16 分音符独奏**，4 拍一句、逐句往上爬

曲式（170 BPM，4/4，104 小节 ≈ 2 分 47 秒）：

    段      小节    时长     内容
    INTRO    1-12   16.9s   强力和弦动机 + 鼓进入
    V1      13-28   22.6s   主歌：闷音和弦 + 八分贝斯 + 旋律
    PRE     29-36   11.3s   预副歌：底鼓加密，往上推
    CHO     37-52   22.6s   副歌：C 大调，16 分踩镲 + 弦乐，全奏
    V2      53-64   16.9s   第二遍主歌增密
    SOLO    65-76   16.9s   16 分独奏
    CHO2    77-92   22.6s   副歌再现（升全音，D 大调）
    OUTRO   93-104  16.9s   收尾，动机再现
                          ─────────
                          146.8s ≈ 2 分 47 秒
"""
from __future__ import annotations

from dataclasses import dataclass

from . import gmnames as gm
from .slicing import dedupe
from .smf import Track, cc, note, program, write_smf

BPM = 170
BEATS_PER_BAR = 4.0


# ── 和声 ──────────────────────────────────────────────────────────────
# power: 强力和弦音高（根音 A2 起，纯五度 + 八度，不加三度）
# scale: 该小节的调式音阶（半音偏移），旋律取音用
CHORDS = {
    "Am": {"root": 45, "power": (45, 52, 57), "scale": (0, 2, 3, 5, 7, 8, 10)},
    "F":  {"root": 41, "power": (41, 48, 53), "scale": (0, 2, 4, 5, 7, 9, 10)},
    "Dm": {"root": 38, "power": (38, 45, 50), "scale": (0, 2, 3, 5, 7, 8, 10)},
    "E":  {"root": 40, "power": (40, 47, 52), "scale": (0, 1, 4, 5, 7, 8, 10)},
    "C":  {"root": 36, "power": (48, 55, 60), "scale": (0, 2, 4, 5, 7, 9, 11)},
    "G":  {"root": 43, "power": (43, 50, 55), "scale": (0, 2, 4, 5, 7, 9, 10)},
    # 升全音后的副歌（C→D、G→A、Am→Bm、F→G）
    "D":  {"root": 38, "power": (50, 57, 62), "scale": (0, 2, 4, 5, 7, 9, 11)},
    "A":  {"root": 45, "power": (45, 52, 57), "scale": (0, 2, 4, 5, 7, 9, 11)},
    "Bm": {"root": 47, "power": (47, 54, 59), "scale": (0, 2, 3, 5, 7, 8, 10)},
}

# 每小节的进行
INTRO_PROG = ("Am", "F", "C", "G")           # 4 小节一循环
VERSE_PROG = ("Am", "F", "Dm", "E")          # 4 小节一循环
PRECHO_PROG = ("Am", "F", "Dm", "E")         # 同上，但每和弦只用 2 小节一次
CHORUS_PROG = ("C", "G", "Am", "F")          # 副歌 4 小节一循环
SOLO_PROG = ("Am", "F", "Dm", "E")
CHO2_PROG = ("D", "A", "Bm", "G")            # 升全音版副歌
OUTRO_PROG = ("Am", "F", "C", "G")


def _prog_for(bar: int) -> tuple[tuple[str, ...], int]:
    """返回 (进行, 段内第几小节)。按 SECTIONS 的边界判断当前在哪一段。"""
    for name, b0, b1, _desc in SECTIONS:
        if b0 <= bar < b1:
            table = {
                "INTRO": INTRO_PROG, "V1": VERSE_PROG, "PRE": PRECHO_PROG,
                "CHO": CHORUS_PROG, "V2": VERSE_PROG, "SOLO": SOLO_PROG,
                "CHO2": CHO2_PROG, "OUTRO": OUTRO_PROG,
            }[name]
            return table, bar - b0
    return VERSE_PROG, bar % 4


def chord_of(bar: int) -> str:
    prog, i = _prog_for(bar)
    return prog[i % len(prog)]


@dataclass
class NoteEvent:
    beat: float
    dur: float
    ch: int
    pitch: int
    vel: int


class Score:
    def __init__(self) -> None:
        self.events: list[NoteEvent] = []
        self.dropped: int = 0

    def add(self, beat, dur, ch, pitch, vel=90):
        if 0 <= pitch <= 127:
            self.events.append(NoteEvent(beat, dur, ch, int(pitch), int(vel)))

    def chord(self, beat, dur, ch, pitches, vel=90):
        for p in pitches:
            self.add(beat, dur, ch, p, vel)


# ── 吉他：强力和弦节奏（日摇的核心）────────────────────────────────────
def pwr(s: Score, beat, dur, chord, vel=100, ch=None):
    """一个强力和弦。"""
    ch = gm.CH_GTR if ch is None else ch
    s.chord(beat, dur, ch, CHORDS[chord]["power"], vel)


def guitar_intro(s: Score, bar: int):
    """动机：| 八分闷音 ×4 | 长和弦 | 八分闷音 ×4 | 长和弦 |
    每小节两个和弦交替，做出"哒哒哒哒——"的推进感。
    """
    c = chord_of(bar)
    nxt = chord_of(bar + 1)
    b0 = bar * BEATS_PER_BAR
    for i in range(4):
        pwr(s, b0 + i * 0.5, 0.42, c, 88 if i % 2 == 0 else 74)
    pwr(s, b0 + 2.0, 1.8, nxt, 104)


def guitar_verse(s: Score, bar: int):
    """主歌：闷音八分音符，第 4 拍留白给贝斯。"""
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    for i in range(6):
        pwr(s, b0 + i * 0.5, 0.42, c, 86 if i % 2 == 0 else 70)
    pwr(s, b0 + 3.0, 0.9, c, 96)


def guitar_pre(s: Score, bar: int):
    """预副歌：八分音符不停，力度往上推。"""
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        pwr(s, b0 + i * 0.5, 0.42, c, 78 + i * 5)


def guitar_chorus(s: Score, bar: int):
    """副歌：全音符长和弦 + 反拍补一下，把空间让给旋律。"""
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    pwr(s, b0, 1.95, c, 112)
    pwr(s, b0 + 2.0, 1.45, c, 106)
    pwr(s, b0 + 3.5, 0.42, c, 118)


def guitar_solo(s: Score, bar: int):
    """独奏段：吉他让位，只留每小节头上一记重和弦。"""
    pwr(s, bar * BEATS_PER_BAR, 0.9, chord_of(bar), 108)


def guitar_outro(s: Score, bar: int):
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    if bar % 4 == 3:
        pwr(s, b0, 3.9, c, 100)              # 最后一小节拖长
    else:
        pwr(s, b0, 1.95, c, 104)
        pwr(s, b0 + 2.0, 1.45, c, 96)
        pwr(s, b0 + 3.5, 0.42, c, 110)


# ── 清音吉他：主歌的分解，做层次 ───────────────────────────────────────
def clean_arp(s: Score, bar: int):
    """清音吉他 16 分分解 —— 只在 V2 用，让第二遍主歌比第一遍厚。

    注意走的是 gm.CH_PAD（通道 9），因为 V2 段暖垫是停的，这个通道
    在 52-64 小节被 SECTION_SWAP 借给清音吉他。
    """
    c = CHORDS[chord_of(bar)]["power"]
    b0 = bar * BEATS_PER_BAR
    order = (c[0] + 12, c[1] + 12, c[2] + 12, c[1] + 12)
    for i in range(16):
        s.add(b0 + i * 0.25, 0.3, gm.CH_PAD, order[i % 4] + 12,
              58 if i % 4 else 70)


# ── 贝斯：八分音符驱动（日摇的推进力主要来自这里）───────────────────────
def bass_eighths(s: Score, bar: int, pattern, vel_base=100, ch=None):
    """按给定音程图案走八分音符。pattern 是相对根音的半音偏移。"""
    ch = gm.CH_BASS if ch is None else ch
    root = CHORDS[chord_of(bar)]["root"]
    b0 = bar * BEATS_PER_BAR
    for i, off in enumerate(pattern):
        vel = vel_base if i % 2 == 0 else vel_base - 14
        s.add(b0 + i * 0.5, 0.42, ch, root + off, vel)


def bass_intro(s: Score, bar: int):
    bass_eighths(s, bar, (0, 0, 12, 0, 0, 0, 12, 7), 96)


def bass_verse(s: Score, bar: int):
    """根音为主 + 第 7 个八分音符上加五度，末尾走高音。"""
    bass_eighths(s, bar, (0, 0, 0, 12, 0, 0, 7, 12), 104)


def bass_pre(s: Score, bar: int):
    """预副歌：全根音猛推，最后两拍上行。"""
    bass_eighths(s, bar, (0, 0, 0, 0, 0, 0, 7, 12), 108)


def bass_chorus(s: Score, bar: int):
    """副歌：更花，带八度跳。"""
    bass_eighths(s, bar, (0, 12, 0, 7, 0, 12, 7, 12), 110)


def bass_solo(s: Score, bar: int):
    bass_eighths(s, bar, (0, 0, 12, 0, 7, 0, 12, 7), 106)


# ── 鼓 ────────────────────────────────────────────────────────────────
def drums_intro(s: Score, bar: int, full: bool = False):
    """前 4 小节只有踩镲 + 军鼓（制造"要进来了"的期待），后 4 小节全进来。"""
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.CLOSED_HAT, 58 if i % 2 == 0 else 46)
    if full:
        for k in (0.0, 2.0):
            s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 108)
        for sn in (1.0, 3.0):
            s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 106)
    else:
        for sn in (1.0, 3.0):
            s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 92)
    if bar % 8 == 7:
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 94 + j * 5)


def drums_verse(s: Score, bar: int):
    """主歌：标准的 8-beat，底鼓 1/3 拍，军鼓 2/4 拍，八分踩镲。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 1.5, 2.0, 3.5):
        s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 106 if k in (0.0, 2.0) else 92)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 104)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.CLOSED_HAT, 64 if i % 2 == 0 else 48)
    if bar % 4 == 3:
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 96 + j * 5)


def drums_pre(s: Score, bar: int):
    """预副歌：底鼓切成 16 分（"咚咚咚咚"），密度往上堆。"""
    b0 = bar * BEATS_PER_BAR
    for i in range(16):
        if i % 2 == 0 or i % 4 == 1:
            s.add(b0 + i * 0.25, 0.2, gm.CH_DRUMS, gm.KICK, 96 if i % 4 == 0 else 82)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 108)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.CLOSED_HAT, 70 if i % 2 == 0 else 52)
    if bar % 8 == 7:                          # 进副歌前的过门
        # 从第 4 拍的反拍(3.25)起 —— 不能用 3.0，那和正拍军鼓同音高同时刻，
        # 前一个 note-off 会把过门的第一个音关掉（硬件上就是挂音）
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 100 + j * 5)


def drums_chorus(s: Score, bar: int):
    """副歌：16 分踩镲 + 反拍底鼓 + 每 4 小节一记吊镲。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 0.75, 1.5, 2.0, 2.75, 3.5):
        s.add(b0 + k, 0.22, gm.CH_DRUMS, gm.KICK, 112 if k in (0.0, 2.0) else 98)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 112)
    for i in range(16):
        hat = gm.OPEN_HAT if i == 15 else gm.CLOSED_HAT
        s.add(b0 + i * 0.25, 0.18, gm.CH_DRUMS, hat, 66 if i % 4 == 0 else 50)
    if bar % 4 == 0:
        s.add(b0, 1.0, gm.CH_DRUMS, gm.CRASH, 118)
    if bar % 4 == 3:
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 100 + j * 4)


def drums_solo(s: Score, bar: int):
    """独奏：骑镲为主，给吉他让路。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 2.0, 3.5):
        s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 108 if k in (0.0, 2.0) else 94)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 108)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.RIDE, 62 if i % 2 == 0 else 48)
    if bar % 4 == 3:
        s.add(b0 + 3.0, 0.5, gm.CH_DRUMS, gm.CRASH, 114)
        for j, p in enumerate((gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.5 + j * 0.25, 0.22, gm.CH_DRUMS, p, 102 + j * 4)


def drums_outro(s: Score, bar: int):
    b0 = bar * BEATS_PER_BAR
    if bar % 4 == 3:
        s.add(b0, 1.0, gm.CH_DRUMS, gm.CRASH, 116)
        s.add(b0, 2.5, gm.CH_DRUMS, gm.KICK, 110)
        return
    for k in (0.0, 2.0):
        s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 106)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 102)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.CLOSED_HAT, 58 if i % 2 == 0 else 44)


# ── 旋律 ──────────────────────────────────────────────────────────────
# 格式：(相对起始拍, 时值, 音级, 力度)；音级 0 = 该小节调式主音
# 主歌 / 预副歌用 Am 音阶，副歌用 C 大调 → 副歌天然比主歌亮
MEL_LOW = (
    (0.0, 0.45, 4, 100), (0.5, 0.45, 4, 90), (1.0, 0.9, 2, 102),
    (2.0, 0.45, 0, 96), (2.5, 0.45, 2, 94), (3.0, 0.9, 4, 104),
    (4.0, 0.45, 4, 100), (4.5, 0.45, 4, 88), (5.0, 0.9, 5, 102),
    (6.0, 0.45, 4, 98), (6.5, 0.45, 2, 94), (7.0, 0.9, 0, 100),
    (8.0, 0.45, 2, 98), (8.5, 0.45, 4, 96), (9.0, 0.9, 5, 106),
    (10.0, 0.45, 5, 100), (10.5, 0.45, 4, 94), (11.0, 1.4, 2, 102),
    (12.5, 0.45, 7, 108), (13.0, 0.45, 5, 100), (13.5, 0.45, 4, 96),
    (14.0, 1.35, 2, 104),
)

# 副歌主题：高八度、长音多、落在和弦音上 —— 要"能跟着唱"
MEL_HIGH = (
    (0.0, 0.45, 4, 112), (0.5, 0.45, 5, 104), (1.0, 0.9, 7, 116),
    (2.0, 0.9, 5, 108), (3.0, 0.9, 4, 112),
    (4.0, 0.45, 4, 110), (4.5, 0.45, 5, 102), (5.0, 0.9, 9, 118),
    (6.0, 0.9, 7, 110), (7.0, 0.9, 5, 108),
    (8.0, 0.45, 7, 114), (8.5, 0.45, 9, 106), (9.0, 0.9, 11, 120),
    (10.0, 0.9, 9, 112), (11.0, 1.4, 7, 114),
    (12.5, 0.45, 5, 108), (13.0, 0.45, 4, 104), (13.5, 0.45, 2, 102),
    (14.0, 1.35, 4, 116),
)

# 预副歌：每 4 拍往上爬一格，做出"推上去"的感觉
MEL_BUILD = (
    (0.0, 0.9, 2, 100), (1.0, 0.9, 4, 104), (2.0, 0.9, 5, 108), (3.0, 0.9, 7, 112),
    (4.0, 0.9, 4, 104), (5.0, 0.9, 5, 108), (6.0, 0.9, 7, 112), (7.0, 0.9, 9, 116),
)

# 独奏：16 分音符乐句，4 拍一句逐句上行
SOLO_PHRASES = (
    ((0.0, 0.22, 0), (0.25, 0.22, 2), (0.5, 0.22, 3), (0.75, 0.22, 4),
     (1.0, 0.22, 5), (1.25, 0.22, 4), (1.5, 0.22, 3), (1.75, 0.22, 2),
     (2.0, 0.45, 0), (2.5, 0.45, 4), (3.0, 0.9, 7)),
    ((0.0, 0.22, 2), (0.25, 0.22, 4), (0.5, 0.22, 5), (0.75, 0.22, 7),
     (1.0, 0.22, 8), (1.25, 0.22, 7), (1.5, 0.22, 5), (1.75, 0.22, 4),
     (2.0, 0.45, 2), (2.5, 0.45, 5), (3.0, 0.9, 9)),
    ((0.0, 0.22, 4), (0.25, 0.22, 5), (0.5, 0.22, 7), (0.75, 0.22, 9),
     (1.0, 0.22, 10), (1.25, 0.22, 9), (1.5, 0.22, 7), (1.75, 0.22, 5),
     (2.0, 0.45, 4), (2.5, 0.45, 7), (3.0, 0.9, 11)),
    ((0.0, 0.22, 5), (0.25, 0.22, 7), (0.5, 0.22, 9), (0.75, 0.22, 11),
     (1.0, 0.22, 12), (1.25, 0.22, 11), (1.5, 0.22, 9), (1.75, 0.22, 7),
     (2.0, 0.9, 5), (3.0, 0.9, 4)),
)


def scale_note(chord: str, degree: int, octave: int = 0) -> int:
    """把音级映射成实际音高。以该和弦所在调式的主音为 0 级。"""
    sc = CHORDS[chord]["scale"]
    base = 60 + 12 * octave                  # 60 = 中央 C
    idx = degree
    while idx < 0:
        idx += 7
        base -= 12
    while idx > 6:
        idx -= 7
        base += 12
    return base + sc[idx]


def melody(s: Score, bar: int, phrase, ch: int, octave: int, vel_scale: float = 1.0):
    """把 16 拍长的乐句放到 bar 开始处。乐句跨 4 小节，逐音取当前和弦的音阶。"""
    b0 = bar * BEATS_PER_BAR
    for off, dur, deg, vel in phrase:
        local_bar = bar + int(off // BEATS_PER_BAR)
        pitch = scale_note(chord_of(local_bar), deg, octave)
        s.add(b0 + off, dur * 0.92, ch, pitch, min(127, int(vel * vel_scale)))


def solo_line(s: Score, bar: int):
    """独奏：每个小节取一句 16 分乐句，4 句一循环逐句上行。"""
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    phrase = SOLO_PHRASES[bar % 4]
    for off, dur, deg in phrase:
        # 独奏音区高一个八度，用吉他音色
        s.add(b0 + off, dur * 0.85, gm.CH_LEAD, scale_note(c, deg, 1),
              106 if off % 1.0 == 0 else 92)


# ── 铺垫（弦乐 / 暖垫）：副歌用，撑住高频 ──────────────────────────────
def pad_upper(s: Score, bar: int, ch: int, octave: int = 0, vel: int = 40):
    """铺底只取三音五音（省掉根音），避免跟贝斯和旋律撞音高。"""
    c = chord_of(bar)
    sc = CHORDS[c]["scale"]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 3.95, ch, [60 + 12 * octave + sc[2], 60 + 12 * octave + sc[4]], vel)


# ── 曲式 ──────────────────────────────────────────────────────────────
SECTIONS = (
    ("INTRO", 0, 12, "强力和弦动机 + 鼓进入"),
    ("V1", 12, 28, "主歌：闷音和弦 + 八分贝斯"),
    ("PRE", 28, 36, "预副歌：底鼓加密往上推"),
    ("CHO", 36, 52, "副歌：C 大调 + 16 分踩镲"),
    ("V2", 52, 64, "第二遍主歌增密"),
    ("SOLO", 64, 76, "16 分独奏"),
    ("CHO2", 76, 92, "副歌再现（升全音，D 大调）"),
    ("OUTRO", 92, 104, "收尾，动机再现"),
)

TOTAL_BARS = SECTIONS[-1][2]
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR
DURATION_SEC = TOTAL_BEATS * 60.0 / BPM


def build_score() -> Score:
    s = Score()

    for bar in range(0, 12):                     # INTRO
        guitar_intro(s, bar)
        bass_intro(s, bar)
        drums_intro(s, bar, full=(bar >= 4))
        if bar >= 8:
            melody(s, bar, MEL_LOW[:6], gm.CH_LEAD, 0, 0.9)

    for bar in range(12, 28):                    # V1
        guitar_verse(s, bar)
        bass_verse(s, bar)
        drums_verse(s, bar)
        if bar >= 16 and bar % 4 == 0:
            melody(s, bar, MEL_LOW, gm.CH_LEAD, 0, 1.0)

    for bar in range(28, 36):                    # PRE
        guitar_pre(s, bar)
        bass_pre(s, bar)
        drums_pre(s, bar)
        # 预副歌每 2 小节一短句，往上顶
        if bar % 2 == 0:
            melody(s, bar, MEL_BUILD[:4] if bar % 4 == 0 else MEL_BUILD[4:],
                   gm.CH_LEAD, 0, 1.05)

    for bar in range(36, 52):                    # CHO
        guitar_chorus(s, bar)
        bass_chorus(s, bar)
        drums_chorus(s, bar)
        pad_upper(s, bar, gm.CH_PAD, octave=1, vel=42)
        if bar % 4 == 0:
            melody(s, bar, MEL_HIGH, gm.CH_LEAD, 0, 1.0)

    for bar in range(52, 64):                    # V2
        guitar_verse(s, bar)
        clean_arp(s, bar)
        bass_verse(s, bar)
        drums_verse(s, bar)
        if bar >= 56 and bar % 4 == 0:
            melody(s, bar, MEL_LOW, gm.CH_LEAD, 0, 1.0)

    for bar in range(64, 76):                    # SOLO
        guitar_solo(s, bar)
        bass_solo(s, bar)
        drums_solo(s, bar)
        solo_line(s, bar)

    for bar in range(76, 92):                    # CHO2（升全音）
        guitar_chorus(s, bar)
        bass_chorus(s, bar)
        drums_chorus(s, bar)
        pad_upper(s, bar, gm.CH_PAD, octave=1, vel=46)
        if bar % 4 == 0:
            melody(s, bar, MEL_HIGH, gm.CH_LEAD, 0, 1.05)

    for bar in range(92, 104):                   # OUTRO
        guitar_outro(s, bar)
        bass_verse(s, bar)
        drums_outro(s, bar)
        if bar % 4 == 0:
            melody(s, bar, MEL_LOW[:6], gm.CH_LEAD, 0, 0.95)

    return dedupe(s.events, Score)


# ── 声部设置 ──────────────────────────────────────────────────────────
# ★ 一个通道只能有一种乐器（PS5 每个声部一个音色）。
#   这里**每个通道只允许出现一次** —— 重复定义会让后一条 Program Change
#   覆盖前一条，声部听起来就是错的。ps5_check_piece.py 会检查这一点。
#
# 通道 9（暖垫）在 V2 段借给清音吉他用：两段在时间上完全不重叠
# （V2 是 52-64 小节，暖垫只在 CHO/CHO2 响），所以同一个通道放两种音色
# 不会打架，靠 SECTION_SWAP 在段边界切音色。
# 声像全部 = 64（居中）—— 左右声道等量输出。监听只接一路时，
# 推到一边的乐器会整轨听不见。
SETUP = (
    (gm.CH_GTR, 30, 104, 64, 30, "失真节奏吉他"),      # Overdriven Guitar
    (gm.CH_BASS, gm.PICK_BASS, 116, 64, 12, "贝斯"),   # Electric Bass (pick)
    (gm.CH_LEAD, 81, 102, 64, 44, "主音合成器"),       # Lead 2 (sawtooth)
    (gm.CH_PAD, gm.WARM_PAD, 78, 64, 58, "暖垫/清音吉他"),
    (gm.DRUM_CHANNEL, 0, 108, 64, 22, "鼓组"),
)

# V2 段把通道 9 从暖垫切成清音吉他
SECTION_SWAP = (
    (52 * 4, gm.CH_PAD, 27, 84),
    (64 * 4, gm.CH_PAD, gm.WARM_PAD, 78),
)

# 换段时的音量变化：(拍, 通道, 音量, 说明)
SECTION_MIX = (
    (28 * 4, gm.CH_GTR, 96, "预副歌：吉他收一点"),
    (36 * 4, gm.CH_GTR, 112, "副歌：吉他顶上去"),
    (36 * 4, gm.CH_DRUMS, 114, "副歌：鼓顶上去"),
    (52 * 4, gm.CH_GTR, 100, "V2：回来"),
    (52 * 4, gm.CH_DRUMS, 108, "V2：回来"),
    (64 * 4, gm.CH_LEAD, 112, "独奏：主音推出来"),
    (64 * 4, gm.CH_GTR, 78, "独奏：节奏吉他让位"),
    (76 * 4, gm.CH_LEAD, 100, "副歌再现"),
    (76 * 4, gm.CH_GTR, 112, "副歌再现"),
    (92 * 4, gm.CH_DRUMS, 100, "收尾"),
)


# ── 输出 ──────────────────────────────────────────────────────────────
def live_events() -> list[tuple[float, tuple[int, ...], str]]:
    out: list[tuple[float, tuple[int, ...], str]] = []
    spb = 60.0 / BPM

    def at(beat: float) -> float:
        return beat * spb + 1.2

    init = [(0.0, (0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7), "GM System On")]
    t0 = 0.15
    for ch, prog, vol, pan, rev, label in SETUP:
        init.append((t0, (0xC0 | ch, prog), f"{label} 音色 {gm.name(prog)}"))
        init.append((t0, (0xB0 | ch, 7, vol), f"{label} 音量 {vol}"))
        init.append((t0, (0xB0 | ch, 10, pan), f"{label} 声像 {pan}"))
        init.append((t0, (0xB0 | ch, 91, rev), f"{label} 混响 {rev}"))
        t0 += 0.01
    out.extend(init)

    for ev in build_score().events:
        out.append((at(ev.beat), (0x90 | ev.ch, ev.pitch, ev.vel), ""))
        out.append((at(ev.beat + ev.dur), (0x90 | ev.ch, ev.pitch, 0), ""))

    for beat, ch, vol, label in SECTION_MIX:
        out.append((at(beat), (0xB0 | ch, 7, vol), label))

    # 段边界换音色（通道 9 在 V2 段借给清音吉他）
    for beat, ch, prog, vol in SECTION_SWAP:
        out.append((at(beat), (0xC0 | ch, prog),
                    f"换音色 → {gm.name(prog)}"))
        out.append((at(beat), (0xB0 | ch, 7, vol), f"音量 {vol}"))

    out.sort(key=lambda x: x[0])
    return [(round(t, 4), msg, label) for t, msg, label in out]


def slice_events(i0: int, i1: int, tail_sec: float = 2.5):
    from .slicing import slice_events as _slice
    return _slice(live_events(), SECTIONS, BEATS_PER_BAR, BPM, i0, i1,
                  tail_sec=tail_sec)


def write_midi_file(path: str) -> None:
    score = build_score()
    tracks: dict[int, Track] = {}

    def tr(ch: int) -> Track:
        if ch not in tracks:
            tracks[ch] = Track(f"Ch{ch + 1:02d}")
        return tracks[ch]

    def ev_cc(ch, beat, num, val):
        cc(tr(ch), beat, ch, num, val)

    for ch, prog, vol, pan, rev, _label in SETUP:
        program(tr(ch), 0.0, ch, prog)
        ev_cc(ch, 0.0, 7, vol)
        ev_cc(ch, 0.0, 10, pan)
        ev_cc(ch, 0.0, 91, rev)

    for beat, ch, vol, _label in SECTION_MIX:
        ev_cc(ch, float(beat) - 0.02, 7, vol)

    # 段边界换音色（通道 9 在 V2 段借给清音吉他）
    for beat, ch, prog, vol in SECTION_SWAP:
        program(tr(ch), float(beat) - 0.02, ch, prog)
        ev_cc(ch, float(beat) - 0.02, 7, vol)

    for ev in score.events:
        note(tr(ev.ch), ev.beat, ev.dur, ev.ch, ev.pitch, ev.vel)

    write_smf(path, [tracks[k] for k in sorted(tracks)],
              tempo_us=int(60_000_000 / BPM), time_sig=(4, 4))
