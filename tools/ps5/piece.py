#!/usr/bin/env python3
"""
piece.py — 给 TASCAM Pocketstudio 5 内藏音源写的 3 分钟曲子。

为什么这么编：
  · PS5 音源是 GM 标准的 16 声部多音色，128 音色 + 5 套鼓组
  · 通道 10 只能放鼓（手册明确规定），所以鼓单独占通道 10
  · PS5 的 MIDI Implementation Chart：Note On 有力度、Note Off 用
    note-on velocity 0 最稳；CC7(音量)/CC10(声像)/CC91(混响) 都识别
  · 所以这里每个声部都显式设好 音量/声像/混响，换段时做出音量层次

曲式（100 BPM，4/4，每和弦一小节，4 小节一个循环 C-Am-F-G）：

    段   小节      时长     内容
    INTRO  1- 8    19.2s   钢琴分解和弦 + 弦乐垫
    A     9-24    38.4s   全乐队（贝斯 + 鼓刷 + 钢琴）
    B    25-40    38.4s   加入旋律、木吉他、铜管点缀、鼓更满
    BREAK 41-48   19.2s   抽掉鼓和贝斯，钢琴 + 弦乐 + 合唱
    C    49-80    76.8s   全奏，旋律移到高八度，加长笛对答
                       ─────────
                       192.0s ≈ 3 分 12 秒

输出是一串 (起始拍, 时值拍, 通道, 音高, 力度) 事件，实时发送和
写 SMF 两条路共用这一份数据 —— 保证听到的和存下来的是同一首。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import gmnames as gm
from .smf import Track, cc, chord, note, program, write_smf

BPM = 96
BEATS_PER_BAR = 4.0


# ── 和声 ──────────────────────────────────────────────────────────────
# 4 小节一循环：C - Am - F - G
CHORDS = {
    "C":  {"triad": (60, 64, 67), "bass": 36, "bass_hi": 48, "scale": (0, 2, 4, 5, 7, 9, 11)},
    "Am": {"triad": (57, 60, 64), "bass": 33, "bass_hi": 45, "scale": (0, 2, 3, 5, 7, 8, 10)},
    "F":  {"triad": (57, 60, 65), "bass": 29, "bass_hi": 41, "scale": (0, 2, 4, 5, 7, 9, 10)},
    "G":  {"triad": (55, 59, 62), "bass": 31, "bass_hi": 43, "scale": (0, 2, 4, 5, 7, 9, 10)},
}
PROGRESSION = ("C", "Am", "F", "G")          # 每小节一个


def chord_of(bar_index: int) -> str:
    """给全局小节号（0 起算）返回和弦名。"""
    return PROGRESSION[bar_index % 4]


# 旋律主题长度：一"呼"一"应"两句，每句 2 小节 → 整句 4 小节 16 拍。
# 所以旋律**每 4 小节才能放一次**；放得比这密就会自我重叠，同音高互相
# 抢 note-off，在硬件音源上表现为挂音。
MELODY_INTERVAL_BARS = 4


@dataclass
class NoteEvent:
    """起始拍 / 时值拍 / 通道 / 音高 / 力度。"""
    beat: float
    dur: float
    ch: int
    pitch: int
    vel: int


# ── 声部写作 ──────────────────────────────────────────────────────────
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


def _swing_off(bar_beat: float) -> float:
    return bar_beat


# ── 钢琴 ──────────────────────────────────────────────────────────────
def piano_intro(s: Score, bar: int):
    """分解和弦：根-五-三-五，每小节 8 个八分音符，柔和。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    voicing = (t[0], t[1], t[2], t[1] + 12)
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        p = voicing[i % 4] + (12 if i >= 4 else 0)
        vel = 52 if i % 2 == 0 else 40
        s.add(b0 + i * 0.5, 0.9, gm.CH_PIANO, p, vel)


def piano_a(s: Score, bar: int):
    """柱式和弦 + 切分低音，稳定推进。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 0.95, gm.CH_PIANO, (t[0], t[1], t[2]), 78)
    s.chord(b0 + 1.5, 0.45, gm.CH_PIANO, (t[1] + 12, t[2] + 12), 58)
    s.chord(b0 + 2.0, 0.95, gm.CH_PIANO, (t[0] + 12, t[1] + 12, t[2] + 12), 68)
    s.chord(b0 + 3.5, 0.45, gm.CH_PIANO, (t[0], t[1], t[2]), 62)


def piano_break(s: Score, bar: int):
    """空旷：整小节长和弦，留给弦乐和合唱。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 3.9, gm.CH_PIANO, (c["triad"][0], c["triad"][1], c["triad"][2]), 50)


def piano_c(s: Score, bar: int):
    """八分音符律动，把能量顶起来。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        p = (t[i % 3] + 12) if i >= 4 else t[i % 3]
        s.add(b0 + i * 0.5, 0.42, gm.CH_PIANO, p, 70 if i % 2 == 0 else 52)


# ── 贝斯 ──────────────────────────────────────────────────────────────
def bass_a(s: Score, bar: int):
    """根音为主，第 3 拍加五度。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.add(b0, 1.4, gm.CH_BASS, c["bass"], 92)
    s.add(b0 + 2.0, 0.9, gm.CH_BASS, c["bass"] + 7, 78)
    s.add(b0 + 3.0, 0.9, gm.CH_BASS, c["bass"] + 12, 72)


def bass_b(s: Score, bar: int):
    """八分音符驱动，节奏更积极。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    pat = (0, 0, 7, 0, 12, 7, 0, 5)
    for i, off in enumerate(pat):
        s.add(b0 + i * 0.5, 0.4, gm.CH_BASS, c["bass"] + off, 88 if i % 2 == 0 else 70)


def bass_c(s: Score, bar: int):
    """走动贝斯，小节末尾加经过音。"""
    c = CHORDS[chord_of(bar)]
    nxt = CHORDS[chord_of(bar + 1)]
    b0 = bar * BEATS_PER_BAR
    s.add(b0, 0.9, gm.CH_BASS, c["bass"], 96)
    s.add(b0 + 1.0, 0.9, gm.CH_BASS, c["bass"] + 7, 80)
    s.add(b0 + 2.0, 0.9, gm.CH_BASS, c["bass"] + 12, 84)
    s.add(b0 + 3.0, 0.5, gm.CH_BASS, c["bass"] + 7, 76)
    # 经过音：走向下一个和弦根音
    step = -1 if nxt["bass"] < c["bass"] else 1
    s.add(b0 + 3.5, 0.45, gm.CH_BASS, nxt["bass"] - step, 72)


# ── 铺底（弦乐 / 暖垫）────────────────────────────────────────────────
def pad_whole(s: Score, bar: int, ch: int, octave: int = 0, vel: int = 48):
    """整小节长音垫，跟着和弦走（完整三和弦）。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    pitches = [p + 12 * octave for p in c["triad"]]
    s.chord(b0, 3.95, ch, pitches, vel)


def pad_upper(s: Score, bar: int, ch: int, octave: int = 0, vel: int = 34):
    """铺底只取三音和五音（省掉根音）。

    为什么要这样：铺底如果跟旋律落在同一个音高和时间上，两个音会互相
    抢 note-off；而根音又常常正好在贝斯或旋律的落音上。省掉根音、只留
    三音五音，铺底就乖乖待在中间层，不会跟任何声部打架。
    """
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, 3.95, ch, [p + 12 * octave for p in c["triad"][1:]], vel)


# ── 吉他 ──────────────────────────────────────────────────────────────
def gtr_arp(s: Score, bar: int):
    """木吉他分解：16 分音符琶音。

    刻意用 16 分网格而不是 32 分 —— 32 分网格上的 0.25/0.75 这类时刻
    换算成秒是无限小数，实时发送时会出现四舍五入抖动；16 分网格干净。
    """
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    order = (t[0], t[1], t[2], t[1] + 12, t[2], t[1], t[0] + 12, t[1])
    for i in range(16):
        s.add(b0 + i * 0.25, 0.5, gm.CH_GTR, order[i % 8] + 12, 54 if i % 4 else 64)


# ── 鼓（通道 10，只能用 GM 打击乐音高）────────────────────────────────
def drums_a(s: Score, bar: int):
    """基础 8-beat：底鼓 1/3 拍，军鼓 2/4 拍，闭合踩镲八分。"""
    b0 = bar * BEATS_PER_BAR
    for i in range(8):
        t = b0 + i * 0.5
        if i % 4 == 0:
            s.add(t, 0.25, gm.CH_DRUMS, gm.KICK, 104)
        if i % 4 == 2:
            s.add(t, 0.25, gm.CH_DRUMS, gm.SNARE, 96)
        s.add(t, 0.2, gm.CH_DRUMS, gm.CLOSED_HAT, 62 if i % 2 == 0 else 46)
    if bar % 8 == 7:
        s.add(b0 + 3.5, 0.4, gm.CH_DRUMS, gm.LOW_TOM, 90)
        s.add(b0 + 3.75, 0.4, gm.CH_DRUMS, gm.HIGH_TOM, 92)


def drums_b(s: Score, bar: int):
    """加切分底鼓 + 开放踩镲 + 每 4 小节一个过门。"""
    b0 = bar * BEATS_PER_BAR
    kicks = (0.0, 0.75, 1.5, 2.0, 2.75, 3.5) if bar % 2 else (0.0, 1.5, 2.0, 3.25)
    snares = (1.0, 3.0)
    for k in kicks:
        s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 108 if k in (0.0, 2.0) else 96)
    for sn in snares:
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 102)
    for i in range(8):
        t = b0 + i * 0.5
        hat = gm.OPEN_HAT if i == 7 else gm.CLOSED_HAT
        s.add(t, 0.2, gm.CH_DRUMS, hat, 60 if i % 2 == 0 else 44)
    if bar % 4 == 3:
        # 过门从第 4 拍的反拍起 —— 不能从 3.0 起，那会和正拍军鼓
        # (pitch 38, 时值到 3.25) 撞在同一个音高上
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 88 + j * 4)


def drums_c(s: Score, bar: int):
    """全奏：骑镲 + 更多底鼓，每 8 小节加 crash。"""
    b0 = bar * BEATS_PER_BAR
    for k in (0.0, 1.5, 2.0, 3.5):
        s.add(b0 + k, 0.25, gm.CH_DRUMS, gm.KICK, 110 if k == 0.0 else 98)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.25, gm.CH_DRUMS, gm.SNARE, 106)
    for i in range(8):
        s.add(b0 + i * 0.5, 0.2, gm.CH_DRUMS, gm.RIDE, 58 if i % 2 == 0 else 46)
    if bar % 8 == 0:
        s.add(b0, 1.2, gm.CH_DRUMS, gm.CRASH, 112)
    if bar % 8 == 7:
        # 同上：过门避开正拍军鼓所在的 3.0
        for j, p in enumerate((gm.HIGH_TOM, gm.HIGH_TOM, gm.LOW_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.22, gm.CH_DRUMS, p, 94 + j * 3)


def drums_break(s: Score, bar: int):
    """Break 段：只有铃鼓和踩镲点，留白。"""
    b0 = bar * BEATS_PER_BAR
    s.add(b0, 0.3, gm.CH_DRUMS, gm.TAMBOURINE, 58)
    s.add(b0 + 2.0, 0.3, gm.CH_DRUMS, gm.TAMBOURINE, 50)


# ── 旋律 ──────────────────────────────────────────────────────────────
# 注意：乐句里**不能有音在自己结束前就撞上下一个音**（时值要收在下一个音
# 的起点之前）。同音高重叠时先到的 note-off 会关掉后一个音，硬件音源上
# 就是挂音。下面每一句末尾的长音都故意留了一点空隙。
MELODY_A_PHRASE = (
    # (相对起始拍, 时值, 相对音级偏移, 力度)
    (0.0, 0.75, 4, 92), (0.75, 0.25, 2, 80), (1.0, 1.0, 0, 86),
    (2.0, 0.75, 2, 88), (2.75, 0.25, 4, 82), (3.0, 0.95, 5, 90),
    (4.0, 0.75, 7, 95), (4.75, 0.25, 5, 82), (5.0, 1.0, 4, 86),
    (6.0, 0.5, 2, 84), (6.5, 0.45, 0, 80), (7.0, 0.85, -3, 88),
)

# B 段主题：更跳，音区更高
MELODY_B_PHRASE = (
    (0.0, 0.5, 7, 96), (0.5, 0.5, 9, 88), (1.0, 1.45, 11, 100),
    (2.5, 0.5, 9, 88), (3.0, 0.85, 7, 94),
    (4.0, 0.5, 9, 96), (4.5, 0.5, 11, 90), (5.0, 1.45, 12, 102),
    (6.5, 0.5, 11, 88), (7.0, 0.85, 9, 94),
)


def scale_note(chord: str, degree: int, octave: int = 1) -> int:
    """把音级（可越界）映射成实际音高：以和弦根音所在大调音阶为骨架。"""
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


def melody_phrase(s: Score, bar: int, phrase, ch: int, octave: int, vel_scale: float = 1.0):
    """把 8 拍长的乐句放到 bar 开始处，每小节按当前和弦取音。"""
    b0 = bar * BEATS_PER_BAR
    for i, (off, dur, deg, vel) in enumerate(phrase):
        # 乐句跨越 2 小节，每个音用所在小节的和弦
        local_bar = bar + int(off // BEATS_PER_BAR)
        pitch = scale_note(chord_of(local_bar), deg + 2, octave)
        s.add(b0 + off, dur * 0.92, ch, pitch, min(127, int(vel * vel_scale)))


def flute_answer(s: Score, bar: int):
    """长笛对答句：第 3-4 拍进来的短动机。"""
    b0 = bar * BEATS_PER_BAR
    c = chord_of(bar)
    motif = ((2.0, 0.5, 4), (2.5, 0.5, 5), (3.0, 1.0, 7))
    for off, dur, deg in motif:
        s.add(b0 + off, dur * 0.9, gm.CH_LEAD, scale_note(c, deg, 2), 84)


def brass_stabs(s: Score, bar: int):
    """铜管短促重音，落在第 2、4 拍的反拍上。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0 + 1.5, 0.3, gm.CH_BRASS, (c["triad"][0] + 12, c["triad"][1] + 12), 88)
    s.chord(b0 + 3.5, 0.3, gm.CH_BRASS, (c["triad"][1] + 12, c["triad"][2] + 12), 84)


# ── 全曲拼装 ──────────────────────────────────────────────────────────
SECTIONS = (
    # 名字      起始小节  结束小节(不含)  说明
    ("INTRO", 0, 8, "钢琴分解 + 弦乐垫"),
    ("A", 8, 24, "全乐队进入"),
    ("B", 24, 40, "旋律 + 吉他 + 铜管"),
    ("BREAK", 40, 48, "抽掉鼓和贝斯"),
    ("C", 48, 80, "全奏 + 长笛对答"),
)


def _dedupe(s: Score) -> Score:
    """去掉「同通道 + 同音高 + 时间重叠」的音。

    为什么必须做：两个同音高的事件重叠时，**先到的那个 note-off 会把
    后一个音也关掉**，剩下一个永远关不掉的 note-on —— 在硬件音源上
    就是挂音（stuck note）。这里让先开始的音优先，后来的重叠音直接丢。

    最容易产生重叠的地方：铺底和弦的音正好落在旋律声部里（钢琴/弦乐
    跟旋律共用通道时），以及鼓的过门军鼓撞上正拍军鼓。
    """
    out = Score()
    last_end: dict[tuple[int, int], float] = {}
    dropped = 0
    for e in sorted(s.events, key=lambda x: (x.beat, x.ch, x.pitch)):
        k = (e.ch, e.pitch)
        if e.beat < last_end.get(k, -1e9) - 1e-9:
            dropped += 1
            continue
        last_end[k] = e.beat + e.dur
        out.events.append(e)
    out.dropped = dropped
    return out


# 给外部工具一个统一的入口名（jrock / serene / solister 都叫 dedupe）
def dedupe(events, score_cls):        # noqa: D401
    """(events, Score) 形式的入口，转发到本地实现。"""
    s = score_cls()
    s.events = list(events)
    return _dedupe(s)


def build_score() -> Score:
    s = Score()

    for bar in range(0, 8):                      # INTRO
        piano_intro(s, bar)
        # 铺底放合唱通道：弦乐通道在 BREAK 段要用整三和弦垫，INTRO 段
        # 的弦乐留空，避免后面几段出现同通道换音色的麻烦
        pad_whole(s, bar, gm.CH_CHOIR, octave=0, vel=40)
        if bar >= 4:
            s.add(bar * 4 + 0.0, 3.9, gm.CH_BASS, CHORDS[chord_of(bar)]["bass"], 66)

    for bar in range(8, 24):                     # A
        piano_a(s, bar)
        bass_a(s, bar)
        drums_a(s, bar)
        pad_upper(s, bar, gm.CH_PAD, octave=0, vel=34)
        # 乐句长 8 拍，所以必须**每 4 小节放一次**（8 拍 = 2 小节，
        # 但主题本身是两小节一句、一呼一应共 4 小节）。每 2 小节放一次
        # 会让同一句在相邻小节重复叠加 → 同音高撞车 → 挂音。
        if bar >= 16 and bar % 4 == 0:
            melody_phrase(s, bar, MELODY_A_PHRASE, gm.CH_STRINGS, 1, 0.85)

    for bar in range(24, 40):                    # B
        piano_a(s, bar)
        bass_b(s, bar)
        drums_b(s, bar)
        gtr_arp(s, bar)
        pad_upper(s, bar, gm.CH_PAD, octave=0, vel=36)
        if bar % MELODY_INTERVAL_BARS == 0:
            melody_phrase(s, bar, MELODY_B_PHRASE, gm.CH_STRINGS, 1, 1.0)
        if bar % 4 == 3:
            brass_stabs(s, bar)
        if bar >= 32:
            s.chord(bar * 4, 3.9, gm.CH_CHOIR, (60, 64, 67), 44)

    for bar in range(40, 48):                    # BREAK
        piano_break(s, bar)
        pad_whole(s, bar, gm.CH_STRINGS, octave=0, vel=54)
        pad_whole(s, bar, gm.CH_CHOIR, octave=0, vel=46)
        drums_break(s, bar)
        if bar % MELODY_INTERVAL_BARS == 0:
            melody_phrase(s, bar, MELODY_A_PHRASE, gm.CH_MARIMBA, 1, 0.8)

    for bar in range(48, 80):                    # C
        piano_c(s, bar)
        bass_c(s, bar)
        drums_c(s, bar)
        gtr_arp(s, bar)
        s.chord(bar * 4, 3.9, gm.CH_CHOIR, (60, 64, 67), 44)
        # 厚度交给暖垫通道（CH_PAD），不要放弦乐通道也不要放合唱通道 ——
        # 弦乐通道同时在高八度跑旋律，合唱通道有上面的和弦长音，
        # 任何一个同音高重叠都会互相抢 note-off，在硬件音源上就是挂音。
        pad_whole(s, bar, gm.CH_PAD, octave=0, vel=32)
        if bar % MELODY_INTERVAL_BARS == 0:
            melody_phrase(s, bar,
                          MELODY_B_PHRASE if (bar // MELODY_INTERVAL_BARS) % 2
                          else MELODY_A_PHRASE,
                          gm.CH_STRINGS, 2, 0.95)
        if bar % 4 == 2:
            flute_answer(s, bar)
        if bar % 4 == 3:
            brass_stabs(s, bar)

    return _dedupe(s)


TOTAL_BARS = SECTIONS[-1][2]
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR
DURATION_SEC = TOTAL_BEATS * 60.0 / BPM


# ── 声部设置：实时发送和 SMF 两条路共用这一张表 ──────────────────────
# (通道, 音色号, 音量, 声像, 混响量, 名字)
# 只写一处，两条输出路径都从这里取，避免改了音色只改了一边。
SETUP = (
    (gm.CH_PIANO, gm.PIANO, 102, 64, 40, "钢琴"),
    (gm.CH_BASS, gm.FINGER_BASS, 110, 64, 18, "贝斯"),
    (gm.CH_STRINGS, gm.STRINGS, 92, 64, 72, "弦乐"),
    (gm.CH_PAD, gm.WARM_PAD, 78, 64, 64, "暖垫"),
    (gm.CH_GTR, gm.NYLON_GTR, 84, 64, 48, "木吉他"),
    (gm.CH_LEAD, gm.FLUTE, 88, 64, 60, "长笛"),
    (gm.CH_BRASS, gm.BRASS, 86, 64, 40, "铜管"),
    (gm.CH_CHOIR, gm.CHOIR, 80, 64, 80, "合唱"),
    (gm.CH_MARIMBA, gm.MARIMBA, 88, 64, 56, "马林巴"),
    (gm.DRUM_CHANNEL, 0, 100, 64, 24, "鼓组"),
)

# 换段时的音量变化：(拍, 通道, 音量, 说明)
SECTION_MIX = (
    (40 * 4, gm.CH_DRUMS, 0, "BREAK：鼓停"),
    (40 * 4, gm.CH_BASS, 0, "BREAK：贝斯停"),
    (40 * 4, gm.CH_GTR, 0, "BREAK：吉他停"),
    (48 * 4, gm.CH_DRUMS, 100, "C 段：鼓回"),
    (48 * 4, gm.CH_BASS, 110, "C 段：贝斯回"),
    (48 * 4, gm.CH_GTR, 84, "C 段：吉他回"),
)


# ── 输出 ──────────────────────────────────────────────────────────────
@dataclass
class LiveEvent:
    """给实时发送用：绝对秒 + 原始 MIDI 字节序列。"""
    t: float
    msg: tuple[int, ...]
    label: str = ""


def live_events() -> list[tuple[float, tuple[int, ...], str]]:
    """把乐谱展开成实时发送序列（秒, MIDI 字节, 说明）。

    开头先来一套初始化：GM System On、各声部音色/音量/声像/混响。
    PS5 的 chart 里 Program Change 和 CC 都是 o（识别）。
    """
    out: list[tuple[float, tuple[int, ...], str]] = []
    spb = 60.0 / BPM

    def at(beat: float) -> float:
        return beat * spb + 1.2          # 留 1.2 秒给用户/设备准备

    # ── 初始化 ──
    init: list[tuple[float, tuple[int, ...], str]] = [
        (0.0, (0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7), "GM System On"),
    ]
    t0 = 0.15
    for ch, prog, vol, pan, rev, label in SETUP:
        init.append((t0, (0xC0 | ch, prog), f"{label} 音色 {gm.name(prog)}"))
        init.append((t0, (0xB0 | ch, 7, vol), f"{label} 音量 {vol}"))
        init.append((t0, (0xB0 | ch, 10, pan), f"{label} 声像 {pan}"))
        init.append((t0, (0xB0 | ch, 91, rev), f"{label} 混响 {rev}"))
        t0 += 0.01
    out.extend(init)

    # ── 音符 ──
    for ev in build_score().events:
        out.append((at(ev.beat), (0x90 | ev.ch, ev.pitch, ev.vel), ""))
        out.append((at(ev.beat + ev.dur), (0x90 | ev.ch, ev.pitch, 0), ""))

    # 段落实时音量变化，做出层次
    for beat, ch, vol, label in SECTION_MIX:
        out.append((at(beat), (0xB0 | ch, 7, vol), label))

    out.sort(key=lambda x: x[0])
    return [(round(t, 4), msg, label) for t, msg, label in out]


def slice_events(i0: int, i1: int, tail_sec: float = 2.5
                 ) -> tuple[list[tuple[float, tuple[int, ...], str]], float]:
    """把实时事件切成 [段 i0 .. 段 i1] 的片段。

    返回 (事件序列, offset)，offset 是片段里时间 0 对应的原曲时刻。

    ★ 关键是**收尾要配对闭合**，不能只按时间硬切：
      如果切在某个音的 note-on 之后、note-off 之前，那个音就永远关不掉
      （硬件音源上就是挂音）。做法和 DAW 里剪切一个 MIDI 片段一样：
      片段末尾统一给所有仍在按着的音补一个 note-off。
    """
    evs = live_events()
    spb = 60.0 / BPM
    lead = 1.2
    t_lo = SECTIONS[i0][1] * BEATS_PER_BAR * spb + lead
    t_hi = SECTIONS[i1][2] * BEATS_PER_BAR * spb + lead
    offset = t_lo - lead

    # 初始化消息永远保留，并挪到最前面
    out = [(0.05, msg, lbl) for t, msg, lbl in evs if t < lead + 0.5]

    # 主体
    body = [(t - offset, msg, lbl) for t, msg, lbl in evs
            if lead + 0.5 <= t <= t_hi]
    out.extend(body)

    # 尾部自然收尾的事件（最后一个音的余韵）
    tail = [(t - offset, msg, lbl) for t, msg, lbl in evs
            if t_hi < t <= t_hi + tail_sec]
    out.extend(tail)

    # ── 统一收尾 ──
    # 为什么不"找回每个音真实的余音"：长音垫子的真实 note-off 可能落在
    # 几百秒之后（下一段），而主体之后的 note-on 又绝对不能带进来（那属于
    # 下一段）。两者一混就会出现"补了 A 音的 off、却漏了 B 音的 off"这种
    # 边界 bug（同一时刻存在两种音高，靠计数配对很容易错位）。
    # 与其绕，不如在片段末尾统一关掉：数学上保证干净；听感上只是长音
    # 被切短一点，而且只在用 --from/--to 放片段时才会碰到。
    def count_open(events) -> dict[tuple[int, int], int]:
        bal: dict[tuple[int, int], int] = {}
        for _t, msg, _lbl in events:
            if len(msg) == 3 and (msg[0] & 0xF0) == 0x90:
                k = (msg[0] & 0x0F, msg[1])
                bal[k] = bal.get(k, 0) + (1 if msg[2] > 0 else -1)
        return bal

    still_open = count_open(out)
    end_t = max((t for t, _m, _l in out), default=0.0) + 0.05
    n_fixed = 0
    for (ch, pitch), cnt in sorted(still_open.items()):
        for i in range(max(0, cnt)):
            out.append((end_t + i * 0.002, (0x90 | ch, pitch, 0),
                        "片段收尾：关掉仍在按着的音"))
            n_fixed += 1

    out.sort(key=lambda x: x[0])
    return out, offset


def write_midi_file(path: str) -> None:
    """写成 SMF（给 PS5 的 SMF 文件夹用，文件名必须 8.3 + .MID）。"""
    score = build_score()

    tracks: dict[int, Track] = {}

    def tr(ch: int) -> Track:
        if ch not in tracks:
            tracks[ch] = Track(f"Ch{ch + 1:02d}")
        return tracks[ch]

    def ev_cc(ch, beat, num, val):
        cc(tr(ch), beat, ch, num, val)

    # 初始化：音色 / 音量 / 声像 / 混响（和实时发送共用 SETUP）
    for ch, prog, vol, pan, rev, _label in SETUP:
        program(tr(ch), 0.0, ch, prog)
        ev_cc(ch, 0.0, 7, vol)
        ev_cc(ch, 0.0, 10, pan)
        ev_cc(ch, 0.0, 91, rev)

    # 段落的音量层次
    for beat, ch, vol, _label in SECTION_MIX:
        ev_cc(ch, float(beat) - 0.02, 7, vol)

    # 音符
    for ev in score.events:
        note(tr(ev.ch), ev.beat, ev.dur, ev.ch, ev.pitch, ev.vel)

    write_smf(path, [tracks[k] for k in sorted(tracks)],
              tempo_us=int(60_000_000 / BPM), time_sig=(4, 4))
