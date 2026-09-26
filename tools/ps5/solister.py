#!/usr/bin/env python3
"""
solister.py — 「Dream Solister 风格」原创改编，给 PS5 的内藏 GM 音源。

================================ 重要说明 ================================
这不是《Dream Solister》原曲的复制。

原曲是商业作品（TRUE 演唱 / 唐沢美帆作词 / 加藤裕介作曲编曲 / Lantis），
我无法凭记忆把它的旋律准确复刻 —— 那既做不到，也不该做。

这一份是**同风格原创改编**：
  · 和声骨架参考该曲公开的副歌和弦走向（乐理信息，非旋律）
  · 配器与编曲手法照着曲风特征走（铜管乐队底的动漫 OP）
  · 旋律全部是原创的
======================================================================

原曲和声走向（副歌，C 大调）—— 这是全曲的骨架：
    │C   -│F     │G   - │C   G/B│
    │Am7   │D     │Gsus4 -│G     │
    │C   -│F     │E7  - │Am7 Gsus4 G│
    │F   -│C/E   │Dm7  -│Em7 A7│
    │Dm7 Em7│FM7 F#m7-5│Gsus4 -│G│

两处关键手法（热血系 OP 的标准配方）：
  · **E7**（V/vi 借和弦）—— 在小调 Am 前面制造紧张
  · **F#m7-5**（半减）—— F→F# 的半音上行，把 G 顶出来

★ 曲风要点（《吹响吧！上低音号》OP，铜管乐队底色）：
  1. **铜管是主角**：小号主奏 + 长号充当 euphonium 齐奏
  2. **快**：178 BPM 的摇滚底，踩镲八分音符不停
  3. **副歌要"炸"**：全奏 + 三度和声 + 上四度模进
  4. **间奏是 show 段**：上低音号 solo + 鼓 fill
  5. **最后一遍副歌整体升全音**：C 大调 → D 大调

曲式（178 BPM，4/4，138 小节 ≈ 3 分 6 秒）：

    段         小节    时长     内容
    INTRO      1-10    13.5s   铜管号角 + 鼓进
    V1        11-26    21.6s   主歌：钢琴 + 清音吉他，旋律弱进
    PRE       27-34    10.8s   预副歌：底鼓加密往上推
    CHO       35-50    21.6s   副歌（C 大调）全奏
    V2        51-62    16.2s   第二遍主歌，配器加厚
    PRE2      63-70    10.8s   预副歌再现
    CHO2      71-86    21.6s   副歌再现，加上三度和声
    SOLO      87-102   21.6s   间奏 show 段（上低音号 + 长号）
    CHO3     103-118   21.6s   副歌（C 大调）最后一次
    CHO4     119-134   21.6s   升全音副歌（D 大调）
    OUTRO    135-138    5.4s   铜管收尾
                              ─────────
                              186.0s ≈ 3 分 6 秒
"""
from __future__ import annotations

from dataclasses import dataclass

from . import gmnames as gm
from .slicing import dedupe
from .smf import Track, cc, note, program, write_smf

BPM = 178
BEATS_PER_BAR = 4.0


# ── 和声 ──────────────────────────────────────────────────────────────
# triad: 中音区三和弦（钢琴/弦乐/铜管）
# high:  高八度（吉他/马林巴）
# bass:  低音
# scale: 该小节的调式音阶（半音偏移），旋律取音用
CHORDS = {
    # ── C 大调区 ──
    "C":   {"triad": (60, 64, 67), "high": (72, 76, 79), "bass": 36,
            "scale": (0, 2, 4, 5, 7, 9, 11)},
    "F":   {"triad": (57, 60, 65), "high": (69, 72, 77), "bass": 29,
            "scale": (0, 2, 4, 5, 7, 9, 10)},
    "G":   {"triad": (55, 59, 62), "high": (67, 71, 74), "bass": 31,
            "scale": (0, 2, 4, 5, 7, 9, 10)},
    "Am":  {"triad": (57, 60, 64), "high": (69, 72, 76), "bass": 33,
            "scale": (0, 2, 3, 5, 7, 8, 10)},
    "Dm":  {"triad": (62, 65, 69), "high": (74, 77, 81), "bass": 38,
            "scale": (0, 2, 3, 5, 7, 8, 10)},
    "E7":  {"triad": (59, 64, 68), "high": (71, 76, 80), "bass": 28,
            "scale": (0, 1, 4, 5, 7, 8, 10)},
    "Em":  {"triad": (59, 64, 67), "high": (71, 76, 79), "bass": 28,
            "scale": (0, 2, 3, 5, 7, 8, 10)},
    "FM7": {"triad": (60, 65, 69), "high": (72, 77, 81), "bass": 29,
            "scale": (0, 2, 4, 5, 7, 9, 10)},
    "F#m7b5": {"triad": (59, 66, 69), "high": (71, 78, 81), "bass": 30,
               "scale": (0, 1, 3, 5, 6, 8, 10)},
    # ── D 大调区（升全音后的副歌）──
    "D":   {"triad": (62, 66, 69), "high": (74, 78, 81), "bass": 38,
            "scale": (0, 2, 4, 5, 7, 9, 11)},
    "G2":  {"triad": (59, 62, 67), "high": (71, 74, 79), "bass": 31,
            "scale": (0, 2, 4, 5, 7, 9, 10)},
    "A":   {"triad": (57, 61, 64), "high": (69, 73, 76), "bass": 33,
            "scale": (0, 2, 4, 5, 7, 9, 11)},
    "Bm":  {"triad": (59, 62, 66), "high": (71, 74, 78), "bass": 35,
            "scale": (0, 2, 3, 5, 7, 8, 10)},
    "Em2": {"triad": (64, 67, 71), "high": (76, 79, 83), "bass": 40,
            "scale": (0, 2, 3, 5, 7, 8, 10)},
    "F#7": {"triad": (61, 66, 70), "high": (73, 78, 82), "bass": 30,
            "scale": (0, 1, 4, 5, 7, 8, 10)},
}

# 每小节的进行（段内循环）
INTRO_PROG = ("C", "G", "Am", "F")           # 号角感
VERSE_PROG = ("C", "G", "Am", "F")
PRECHO_PROG = ("Am", "F", "Dm", "G")         # 往上顶到 G，直接推进副歌
CHORUS_PROG = ("C", "F", "G", "C")
SOLO_PROG = ("Am", "F", "C", "G")
CHO4_PROG = ("D", "G2", "A", "D")

# 预副歌里那个 Gsus4 → G 需要拆到半小节，这里用特殊标记处理
_SPECIAL = {"Gsus_G": ("G", "G")}


def _prog_of(section: str) -> tuple[str, ...]:
    return {
        "INTRO": INTRO_PROG, "V1": VERSE_PROG, "PRE": PRECHO_PROG,
        "CHO": CHORUS_PROG, "V2": VERSE_PROG, "PRE2": PRECHO_PROG,
        "CHO2": CHORUS_PROG, "SOLO": SOLO_PROG, "CHO3": CHORUS_PROG,
        "CHO4": CHO4_PROG, "OUTRO": CHO4_PROG,
    }[section]


def _section_of(bar: int) -> tuple[str, int]:
    """返回 (段名, 段内小节号)。

    ★ 越界要 **clamp**，不能静默兜底。
      原来的写法是"扫不到就返回 ('V1', 0)" —— 于是 chord_of(87) 这种
      越界查询会悄悄拿到 V1 段的和弦，把 SOLO 段算成 C 大调。
      更糟的是 melody() 会遍历乐句覆盖的小节号，一旦越界就取到错的
      音阶音，导致本该只在副歌响的旋律跑进间奏段，和 solo_show 撞车。
      正确做法：夹在最近的段边界上，并且对真正的越界大声报错。
    """
    if not SECTIONS:
        raise ValueError("SECTIONS 是空的")
    if bar < 0 or bar >= TOTAL_BARS:
        raise ValueError(
            f"小节号 {bar} 越界（曲子共 {TOTAL_BARS} 小节）—— "
            f"这通常说明某个循环的边界算错了")
    for name, b0, b1, _d in SECTIONS:
        if b0 <= bar < b1:
            return name, bar - b0
    # 段落之间有缝（不该发生）——夹到最近的前一段
    best = max((sec for sec in SECTIONS if sec[1] <= bar), key=lambda s: s[1])
    return best[0], bar - best[1]


def chord_of(bar: int) -> str:
    sec, i = _section_of(bar)
    prog = _prog_of(sec)
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


# ── 铜管：号角与 stab（这是这首曲子的灵魂）────────────────────────────
def fanfare(s: Score, beat: float, chord: str, vel: int = 112, dur: float = 0.42):
    """铜管号角：三和弦齐奏的短促重音。"""
    c = CHORDS[chord]
    s.chord(beat, dur, gm.CH_BRASS, c["triad"], vel)
    s.add(beat, dur, gm.CH_TROMBONE, c["bass"] + 12, vel - 6)


def brass_hits(s: Score, bar: int, pattern, chord: str | None = None,
               vel: int = 104):
    """按给定节奏型打铜管重音。pattern 是相对小节的拍位置。"""
    c = chord or chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    for off, dur in pattern:
        fanfare(s, b0 + off, c, vel, dur)


def brass_swell(s: Score, bar: int, vel: int = 96):
    """整小节铜管长音 —— 副歌的"炸"。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 3.85, gm.CH_BRASS, c["triad"], vel)
    s.add(b0, 3.85, gm.CH_TROMBONE, c["bass"] + 12, vel - 8)


# ── 钢琴 / 电钢 ───────────────────────────────────────────────────────
def piano_eighths(s: Score, bar: int, vel: int = 74, dens: int = 8):
    """八分音符柱式伴奏（右手）+ 低音（左手）—— 动漫 OP 的标准钢琴做法。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    step = BEATS_PER_BAR / dens
    for i in range(dens):
        # 反拍轻、正拍重
        v = vel if i % 2 == 0 else vel - 16
        s.chord(b0 + i * step, step * 0.9, gm.CH_PIANO, t[:2], v)
        if i % 2 == 0:
            s.add(b0 + i * step, step * 1.7, gm.CH_PIANO, t[2] + 12, v - 6)


def piano_power(s: Score, bar: int, vel: int = 92):
    """副歌钢琴：整拍重音 + 反拍推动。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    for i in range(4):
        s.chord(b0 + i, 0.9, gm.CH_PIANO, t, vel if i % 2 == 0 else vel - 14)
    s.add(b0 + 0.0, 1.8, gm.CH_PIANO, t[0] - 12, vel - 6)


def epiano_arp(s: Score, bar: int):
    """电钢 16 分琶音 —— 主歌的流动层。"""
    c = CHORDS[chord_of(bar)]["high"]
    b0 = bar * BEATS_PER_BAR
    order = (c[0], c[1], c[2], c[1] + 12)
    for i in range(16):
        s.add(b0 + i * 0.25, 0.28, gm.CH_EPIANO, order[i % 4], 52 if i % 4 else 64)


# ── 吉他 ──────────────────────────────────────────────────────────────
def gtr_eighths(s: Score, bar: int, vel: int = 72):
    """清音吉他八分音符切分。"""
    c = CHORDS[chord_of(bar)]["high"]
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        s.add(b0 + i * 0.5, 0.4, gm.CH_GTR, c[i % 3] - 12,
              vel if i % 2 == 0 else vel - 14)


def gtr_power(s: Score, bar: int, vel: int = 96):
    """副歌：八度加强的切分。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    root = c["bass"] + 24
    for off in (0.0, 1.5, 2.0, 3.5):
        s.add(b0 + off, 0.42, gm.CH_GTR, root, vel)
        s.add(b0 + off, 0.42, gm.CH_GTR, root + 7, vel - 10)


# ── 贝斯 ──────────────────────────────────────────────────────────────
def bass_eighths(s: Score, bar: int, pattern, vel: int = 104):
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    for i, off in enumerate(pattern):
        s.add(b0 + i * 0.5, 0.42, gm.CH_BASS, c["bass"] + off,
              vel if i % 2 == 0 else vel - 12)


def bass_verse(s: Score, bar: int):
    bass_eighths(s, bar, (0, 0, 12, 0, 0, 7, 0, 12), 100)


def bass_chorus(s: Score, bar: int):
    """副歌贝斯：更花，带八度和五度跳进。"""
    bass_eighths(s, bar, (0, 12, 0, 7, 0, 12, 7, 12), 108)


def bass_solo(s: Score, bar: int):
    bass_eighths(s, bar, (0, 0, 0, 12, 7, 0, 12, 7), 104)


# ── 铺底 ──────────────────────────────────────────────────────────────
def pad_upper(s: Score, bar: int, ch: int, octave: int = 0, vel: int = 44):
    """铺底只取三音五音（省根音），避免跟旋律和贝斯撞音高。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 3.9, ch, [p + 12 * octave for p in c["triad"][1:]], vel)


def strings_whole(s: Score, bar: int, vel: int = 56):
    c = CHORDS[chord_of(bar)]
    s.chord(bar * BEATS_PER_BAR, 3.9, gm.CH_STRINGS, c["triad"], vel)


# ── 鼓 ────────────────────────────────────────────────────────────────
def drums_verse(s: Score, bar: int):
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 1.5, 2.0, 3.5):
        s.add(b0 + k, 0.22, gm.CH_DRUMS, gm.KICK, 108 if k in (0.0, 2.0) else 92)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.22, gm.CH_DRUMS, gm.SNARE, 106)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.18, gm.CH_DRUMS, gm.CLOSED_HAT,
              66 if i % 2 == 0 else 48)
    if bar % 4 == 3:
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.2, gm.CH_DRUMS, p, 98 + j * 4)


def drums_chorus(s: Score, bar: int):
    """副歌：16 分踩镲 + 反拍底鼓 + 每 4 小节吊镲。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 0.75, 1.5, 2.0, 2.75, 3.5):
        s.add(b0 + k, 0.2, gm.CH_DRUMS, gm.KICK, 114 if k in (0.0, 2.0) else 100)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.22, gm.CH_DRUMS, gm.SNARE, 114)
    for i in range(16):
        hat = gm.OPEN_HAT if i == 15 else gm.CLOSED_HAT
        s.add(b0 + i * 0.25, 0.16, gm.CH_DRUMS, hat, 68 if i % 4 == 0 else 50)
    if bar % 4 == 0:
        s.add(b0, 0.9, gm.CH_DRUMS, gm.CRASH, 118)
    if bar % 8 == 7:
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.2, gm.CH_DRUMS, p, 102 + j * 4)


def drums_pre(s: Score, bar: int):
    """预副歌：底鼓切 16 分往上推。"""
    b0 = bar * BEATS_PER_BAR
    for i in range(16):
        if i % 2 == 0 or i % 4 == 1:
            s.add(b0 + i * 0.25, 0.18, gm.CH_DRUMS, gm.KICK,
                  98 if i % 4 == 0 else 84)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.22, gm.CH_DRUMS, gm.SNARE, 110)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.18, gm.CH_DRUMS, gm.CLOSED_HAT,
              72 if i % 2 == 0 else 52)
    if bar % 8 == 7:
        # ★ 过门必须从第 4 拍的**反拍**(3.25)起。
        #   从 3.0 起会和正拍军鼓同音高同时刻 —— 先到的 note-off
        #   会把过门的第一个音关掉，硬件上就是挂音。这个坑在
        #   piece / jrock / serene 里都踩过。
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.2, gm.CH_DRUMS, p, 104 + j * 4)


def drums_solo(s: Score, bar: int):
    """间奏：骑镲为主，每 4 小节一个 fill。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 2.0, 2.75, 3.5):
        s.add(b0 + k, 0.22, gm.CH_DRUMS, gm.KICK, 110 if k in (0.0, 2.0) else 96)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.22, gm.CH_DRUMS, gm.SNARE, 110)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.18, gm.CH_DRUMS, gm.RIDE, 64 if i % 2 == 0 else 50)
    if bar % 4 == 3:
        s.add(b0 + 3.0, 0.45, gm.CH_DRUMS, gm.CRASH, 116)
        for j, p in enumerate((gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.5 + j * 0.25, 0.2, gm.CH_DRUMS, p, 104 + j * 4)


# ── 旋律 ──────────────────────────────────────────────────────────────
# 格式 (相对起始拍, 时值, 音级, 力度)；音级 0 = 该小节调式主音
#
# 主歌：跳跃、有推进感，落音多在和弦音上
MEL_VERSE = (
    (0.0, 0.45, 4, 92), (0.5, 0.45, 4, 84), (1.0, 0.9, 5, 96),
    (2.0, 0.45, 7, 94), (2.5, 0.45, 5, 86), (3.0, 0.9, 4, 90),
    (4.0, 0.45, 2, 88), (4.5, 0.45, 4, 84), (5.0, 0.9, 5, 94),
    (6.0, 0.45, 4, 88), (6.5, 0.45, 2, 82), (7.0, 0.9, 0, 88),
    (8.0, 0.45, 4, 94), (8.5, 0.45, 5, 88), (9.0, 0.9, 7, 100),
    (10.0, 0.45, 5, 92), (10.5, 0.45, 4, 86), (11.0, 1.4, 2, 92),
    (12.5, 0.45, 7, 96), (13.0, 0.45, 5, 88), (13.5, 0.45, 4, 86),
    (14.0, 1.35, 0, 90),
)

# 预副歌：每 2 拍上行一格，往上顶
MEL_BUILD = (
    (0.0, 0.9, 2, 90), (1.0, 0.9, 4, 94), (2.0, 0.9, 5, 98), (3.0, 0.9, 7, 102),
    (4.0, 0.9, 4, 94), (5.0, 0.9, 5, 98), (6.0, 0.9, 7, 104), (7.0, 0.9, 9, 108),
)

# 副歌：长音多、音区高、能跟着唱 —— 热血 OP 的副歌就靠这个
#
# ★ 收尾必须留出空隙。这个乐句长 16 拍（4 小节），最后两个音原本是
#   (12.5, 0.45) 和 (14.0, 1.35×0.94=1.27)，即从第 14 拍延伸到 15.27 拍。
#   而下一句（或下一个段落的 solo_show）在第 16 拍就起了 —— 第 87 小节
#   就是这样和间奏的 solo 撞上（通道 5 和 15 共 4 处）。收到 14.0 + 1.0。
MEL_CHORUS = (
    (0.0, 0.45, 4, 104), (0.5, 0.45, 5, 96), (1.0, 0.9, 7, 110),
    (2.0, 0.9, 5, 100), (3.0, 0.9, 4, 104),
    (4.0, 0.45, 5, 102), (4.5, 0.45, 7, 98), (5.0, 1.4, 9, 114),
    (6.5, 0.45, 7, 100), (7.0, 0.9, 5, 102),
    (8.0, 0.45, 7, 108), (8.5, 0.45, 9, 102), (9.0, 0.9, 11, 118),
    (10.0, 0.9, 9, 106), (11.0, 1.4, 7, 110),
    (12.5, 0.45, 5, 100), (13.0, 0.45, 4, 96), (13.5, 0.45, 2, 94),
    (14.0, 1.0, 4, 108),
)

# 副歌高八度再现（最后两遍副歌用，把情绪顶满）
MEL_CHORUS_HI = tuple(
    (off, dur, deg, min(127, vel + 4)) for off, dur, deg, vel in MEL_CHORUS)

MELODY_INTERVAL_BARS = 4


def scale_note(chord: str, degree: int, octave: int = 0) -> int:
    sc = CHORDS[chord]["scale"]
    base = 60 + 12 * octave
    idx = degree
    while idx < 0:
        idx += 7
        base -= 12
    while idx > 6:
        idx -= 7
        base += 12
    return base + sc[idx]


def melody(s: Score, bar: int, phrase, ch: int, octave: int,
           vel_scale: float = 1.0, max_beats: float | None = None):
    """把 16 拍长的乐句放到 bar 开始处。

    max_beats：乐句只允许占用的拍数。**段边界处必须传这个参数** ——
    否则最后一句会跨过段线伸进下一段，和下一段的声部（比如间奏的
    solo_show）在同一通道同一音高上撞车。这个 bug 真出现过：
    CHO4 最后一句从第 118 小节起跨 16 拍，而 CHO4 到 134 小节就结束了。
    """
    b0 = bar * BEATS_PER_BAR
    for off, dur, deg, vel in phrase:
        if max_beats is not None and off >= max_beats:
            break
        if max_beats is not None:
            dur = min(dur, max_beats - off)
            if dur <= 0.05:
                continue
        local_bar = bar + int(off // BEATS_PER_BAR)
        pitch = scale_note(chord_of(local_bar), deg, octave)
        s.add(b0 + off, dur * 0.94, ch, pitch, min(127, int(vel * vel_scale)))


def melody_harmony3(s: Score, bar: int, phrase, octave: int,
                    vel_scale: float = 0.9, max_beats: float | None = None):
    """三度和声 —— 副歌"炸"的关键：主旋律下方叠一个三度。"""
    b0 = bar * BEATS_PER_BAR
    for off, dur, deg, vel in phrase:
        if max_beats is not None and off >= max_beats:
            break
        if max_beats is not None:
            dur = min(dur, max_beats - off)
            if dur <= 0.05:
                continue
        local_bar = bar + int(off // BEATS_PER_BAR)
        pitch = scale_note(chord_of(local_bar), deg - 2, octave)
        s.add(b0 + off, dur * 0.94, gm.CH_TROMBONE, pitch,
              min(127, int(vel * vel_scale)))


def solo_show(s: Score, bar: int):
    """间奏 show 段：上低音号（长号）领奏 + 小号对答。"""
    c = chord_of(bar)
    b0 = bar * BEATS_PER_BAR
    # 上低音号：连续八分音符，音区在中低
    leaps = (0, 0, 2, 4, 2, 0, -3, 0)
    for i, deg in enumerate(leaps):
        s.add(b0 + i * 0.5, 0.42, gm.CH_TROMBONE, scale_note(c, deg, 0), 100)
    # 小号在第 3-4 拍对答
    for off, deg in ((2.0, 7), (2.5, 9), (3.0, 7), (3.5, 5)):
        s.add(b0 + off, 0.42, gm.CH_LEAD, scale_note(c, deg, 0), 104)


# ── 曲式 ──────────────────────────────────────────────────────────────
SECTIONS = (
    ("INTRO", 0, 10, "铜管号角 + 鼓进"),
    ("V1", 10, 26, "主歌：钢琴 + 清音吉他"),
    ("PRE", 26, 34, "预副歌：底鼓加密往上推"),
    ("CHO", 34, 50, "副歌（C 大调）全奏"),
    ("V2", 50, 62, "第二遍主歌，配器加厚"),
    ("PRE2", 62, 70, "预副歌再现"),
    ("CHO2", 70, 86, "副歌再现 + 三度和声"),
    ("SOLO", 86, 102, "间奏 show 段（上低音号领奏）"),
    ("CHO3", 102, 118, "副歌（C 大调）最后一次"),
    ("CHO4", 118, 134, "升全音副歌（D 大调）"),
    ("OUTRO", 134, 138, "铜管收尾"),
)

TOTAL_BARS = SECTIONS[-1][2]
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR
DURATION_SEC = TOTAL_BEATS * 60.0 / BPM


def _is_final_chorus(bar: int) -> bool:
    return SECTIONS[9][1] <= bar < SECTIONS[9][2]


def build_score() -> Score:
    s = Score()

    # ── INTRO：铜管号角 ──
    for bar in range(0, 10):
        c = chord_of(bar)
        b0 = bar * BEATS_PER_BAR
        if bar < 4:
            # 前 4 小节：只有铜管号角和吊镲，制造"要来了"
            fanfare(s, b0, c, 116, 0.9)
            fanfare(s, b0 + 1.5, c, 104, 0.42)
            fanfare(s, b0 + 2.0, c, 110, 0.42)
            if bar == 0:
                s.add(b0, 1.2, gm.CH_DRUMS, gm.CRASH, 118)
        else:
            brass_hits(s, bar, ((0.0, 0.42), (1.0, 0.42), (2.0, 0.42), (3.5, 0.42)))
            drums_verse(s, bar)
            bass_verse(s, bar)
            piano_eighths(s, bar, 76)
            gtr_eighths(s, bar, 70)
        if bar >= 6:
            strings_whole(s, bar, 58)

    # ── V1 ──
    for bar in range(10, 26):
        piano_eighths(s, bar, 74)
        epiano_arp(s, bar)
        gtr_eighths(s, bar, 70)
        bass_verse(s, bar)
        drums_verse(s, bar)
        pad_upper(s, bar, gm.CH_PAD, 0, 42)
        if bar >= 14 and bar % MELODY_INTERVAL_BARS == 2:
            melody(s, bar, MEL_VERSE, gm.CH_LEAD, 0, 1.0)

    # ── PRE ──
    for bar in range(26, 34):
        piano_eighths(s, bar, 78)
        gtr_eighths(s, bar, 74)
        bass_verse(s, bar)
        drums_pre(s, bar)
        pad_upper(s, bar, gm.CH_PAD, 1, 46)
        if bar % 2 == 0:
            melody(s, bar, MEL_BUILD[:4] if bar % 4 == 0 else MEL_BUILD[4:],
                   gm.CH_LEAD, 0, 1.0)

    # ── 副歌（CHO / CHO2 / CHO3 用同一套，靠下面的层次区分）──
    for bar in range(34, 50):                    # CHO
        _chorus_bar(s, bar, harmony=False, hi=False)
    for bar in range(50, 62):                    # V2
        piano_eighths(s, bar, 76)
        epiano_arp(s, bar)
        gtr_eighths(s, bar, 72)
        bass_verse(s, bar)
        drums_verse(s, bar)
        pad_upper(s, bar, gm.CH_PAD, 0, 44)
        strings_whole(s, bar, 50)
        if bar >= 54 and bar % MELODY_INTERVAL_BARS == 2:
            melody(s, bar, MEL_VERSE, gm.CH_LEAD, 0, 1.0)
    for bar in range(62, 70):                    # PRE2
        piano_eighths(s, bar, 80)
        gtr_eighths(s, bar, 76)
        bass_verse(s, bar)
        drums_pre(s, bar)
        pad_upper(s, bar, gm.CH_PAD, 1, 48)
        if bar % 2 == 0:
            melody(s, bar, MEL_BUILD[:4] if bar % 4 == 0 else MEL_BUILD[4:],
                   gm.CH_LEAD, 0, 1.0)
    for bar in range(70, 86):                    # CHO2
        _chorus_bar(s, bar, harmony=True, hi=False)

    # ── SOLO ──
    for bar in range(86, 102):
        _solo_backing(s, bar)
        solo_show(s, bar)
        if bar % 8 == 4:
            brass_swell(s, bar, 92)

    for bar in range(102, 118):                  # CHO3
        _chorus_bar(s, bar, harmony=True, hi=False)
    for bar in range(118, 134):                  # CHO4（升全音）
        _chorus_bar(s, bar, harmony=True, hi=True)

    # ── OUTRO ──
    for bar in range(134, 138):
        c = chord_of(bar)
        b0 = bar * BEATS_PER_BAR
        if bar == 137:
            fanfare(s, b0, c, 120, 3.6)
            s.add(b0, 1.4, gm.CH_DRUMS, gm.CRASH, 120)
        else:
            brass_hits(s, bar, ((0.0, 0.42), (1.5, 0.42), (2.0, 0.42), (3.5, 0.42)))
            piano_power(s, bar, 90)
            bass_chorus(s, bar)
            drums_chorus(s, bar)

    return dedupe(s.events, Score)


def _solo_backing(s: Score, bar: int) -> None:
    """间奏段的伴奏层（**不含旋律**）。

    单独拆出来的原因：间奏是 show 段，只应该有 solo_show 在吹。
    原来直接复用了 _chorus_bar，把副歌的旋律和三度和声一起带了进来，
    结果同一通道上 solo_show 和 melody 撞车（第 87、88 小节，共 4 处）。
    """
    piano_power(s, bar, 82)
    bass_solo(s, bar)
    drums_solo(s, bar)
    gtr_power(s, bar, 88)
    pad_upper(s, bar, gm.CH_PAD, 1, 44)


def _chorus_bar(s: Score, bar: int, harmony: bool, hi: bool) -> None:
    """副歌一小节。hi=True 时旋律升八度（最后一遍用）。"""
    piano_power(s, bar, 92)
    bass_chorus(s, bar)
    drums_chorus(s, bar)
    gtr_power(s, bar, 96)
    strings_whole(s, bar, 62)
    pad_upper(s, bar, gm.CH_PAD, 1, 46)
    brass_swell(s, bar, 98)
    if bar % MELODY_INTERVAL_BARS != 0:
        return

    # ★ 乐句长 16 拍，必须确认它能完整放进本段。
    #   原来不检查，最后一句就跨过段线伸进下一段 —— CHO4 的最后一句
    #   从第 118 小节起跨 16 拍，而 CHO4 到 134 小节就结束了，
    #   于是旋律跑进 OUTRO，和别的声部撞车。
    sec_name, _idx = _section_of(bar)
    sec_end = next(b1 for nm, _b0, b1, _d in SECTIONS if nm == sec_name)
    room = (sec_end - bar) * BEATS_PER_BAR
    limit = room if room < 16 else None

    phrase = MEL_CHORUS_HI if hi else MEL_CHORUS
    octv = 1 if hi else 0
    melody(s, bar, phrase, gm.CH_LEAD, octv, 1.0, max_beats=limit)
    if harmony:
        melody_harmony3(s, bar, phrase, octv, 0.85, max_beats=limit)


# ── 声部设置 ──────────────────────────────────────────────────────────
# ★ 声像全部 = 64（居中），左右等量输出
# ★ 一个通道只能有一种乐器，ps5_channels.py 会检查
SETUP = (
    (gm.CH_PIANO, gm.PIANO, 100, 64, 44, "钢琴"),
    (gm.CH_EPIANO, gm.EPIANO, 78, 64, 52, "电钢"),
    (gm.CH_BASS, gm.PICK_BASS, 112, 64, 16, "贝斯"),
    (gm.CH_GTR, 27, 86, 64, 40, "清音吉他"),        # Electric Guitar (clean)
    (gm.CH_STRINGS, gm.STRINGS, 84, 64, 80, "弦乐"),
    (gm.CH_LEAD, 56, 104, 64, 48, "小号主奏"),      # Trumpet
    (gm.CH_BRASS, gm.BRASS, 92, 64, 44, "铜管组"),
    (gm.CH_TROMBONE, 57, 96, 64, 50, "长号/上低音号"),  # Trombone
    (gm.CH_PAD, gm.WARM_PAD, 74, 64, 70, "暖垫"),
    (gm.DRUM_CHANNEL, 0, 110, 64, 22, "鼓组"),
)

# 段落层次：(拍, 通道, 音量, 说明) —— 全部用 SECTIONS 边界推导，
# 避免曲式改动后残留越界的指令（这个 bug 真出现过）
_B = {name: b0 * BEATS_PER_BAR for name, b0, _b1, _d in SECTIONS}
SECTION_MIX = (
    (_B["CHO"], gm.CH_STRINGS, 90, "副歌：弦乐顶出来"),
    (_B["SOLO"], gm.CH_LEAD, 96, "间奏：小号对答"),
    (_B["CHO3"], gm.CH_BRASS, 100, "最后两遍副歌：铜管加码"),
    (_B["CHO4"], gm.CH_LEAD, 110, "升全音副歌：主奏再推"),
)
assert max(b for b, _c, _v, _l in SECTION_MIX) < TOTAL_BEATS, \
    "SECTION_MIX 里有超出曲子长度的事件 —— 会让播放多出一截静音"


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

    for ev in score.events:
        note(tr(ev.ch), ev.beat, ev.dur, ev.ch, ev.pitch, ev.vel)

    write_smf(path, [tracks[k] for k in sorted(tracks)],
              tempo_us=int(60_000_000 / BPM), time_sig=(4, 4))
