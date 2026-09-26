#!/usr/bin/env python3
"""
jpop.py — J-POP 风格曲，**专门为 16 复音设计**，给 PS5 的内藏 GM 音源。

================================ 16 复音是怎么做到的 ================================
不是"多堆几个乐器"就行 —— 得让**某个瞬间真的有 16 个音同时响**。
做法是让两层以上的长音铺底在副歌叠起来，每一层都是完整四音和弦：

    副歌最密处的同时发声数：
      钢琴 4 音 + 电钢 3 音 + 合成垫 3 音 + 弦乐 4 音 + 合唱 2 音
      = 16 个长音（全是和弦音，互相不打架）
      + 贝斯 1 + 主奏 1 + 吉他 2 + 鼓 = 整个 mix 超过 20 个发声

    ★ 关键在于**省掉根音**：钢琴负责根音，电钢/合成垫/弦乐只取
      三音五音，各自占不同的音区，这样 16 个音同时响也不会糊成一团，
      也不会因为同音高重叠而互相抢 note-off（那会变成挂音）。

`tools/ps5_polyphony.py` 会实际数出每个时刻的同时发声数。

==================================================================================

和声用 J-POP 最经典的 **王道进行**（小室進行／royal road）：
    F - G - Em - Am   （IV - V - iii - vi）
    F - G - C         （IV - V - I，落回主和弦）
放在 C 大调上，八小节一循环。这个进行之所以是"J-POP 味"的灵魂，是因为
它让 IV 起头（不是主和弦），最后才落到 I —— 那种"绕一圈才回家"的感觉。

曲式（120 BPM，4/4，60 小节 = 正好 2 分 00 秒）：

    段         小节    时长     同时发声数（设计值）
    INTRO      1-6     12s      6    钢琴 + 电钢，极简
    V1         7-18    24s      8-10 贝斯/鼓/弦乐进
    PRE        19-22    8s      12   往上堆
    CHO        23-34   24s      **16+** 全奏（16 复音在这里）
    BREAK      35-38    8s      6    抽空，只有钢琴 + 合唱
    V2         39-50   24s      10-12 比 V1 厚
    CHO2       51-58   16s      **16+** 全奏 + 主奏升八度
    OUTRO      59-60    4s      8    收尾
                              ─────────
                              120s = 2 分 00 秒
"""
from __future__ import annotations

from dataclasses import dataclass

from . import gmnames as gm
from .slicing import dedupe
from .smf import Track, cc, note, program, write_smf

BPM = 120
BEATS_PER_BAR = 4.0


# ── 和声：王道进行 ────────────────────────────────────────────────────
# root: 根音（钢琴左手/贝斯用）
# triad: 完整三和弦（中音区，钢琴右手/弦乐）
# upper: 只取三音五音（电钢/合成垫用 —— 省掉根音，避免和别层撞音高）
# high:  高八度（吉他/钟琴用）
# scale: 该小节的调式音阶（半音偏移），旋律取音用
CHORDS = {
    "F":  {"root": 41, "triad": (65, 69, 72), "upper": (69, 72),
           "high": (77, 81, 84), "scale": (0, 2, 4, 5, 7, 9, 10)},
    "G":  {"root": 43, "triad": (67, 71, 74), "upper": (71, 74),
           "high": (79, 83, 86), "scale": (0, 2, 4, 5, 7, 9, 10)},
    "Em": {"root": 40, "triad": (64, 67, 71), "upper": (67, 71),
           "high": (76, 79, 83), "scale": (0, 2, 3, 5, 7, 8, 10)},
    "Am": {"root": 45, "triad": (64, 69, 72), "upper": (69, 72),
           "high": (76, 81, 84), "scale": (0, 2, 3, 5, 7, 8, 10)},
    # 落回主和弦时用 C，并加一个 Dm7 做经过，避免结尾太"干"
    "C":  {"root": 36, "triad": (60, 64, 67), "upper": (64, 67),
           "high": (72, 76, 79), "scale": (0, 2, 4, 5, 7, 9, 11)},
    "Dm7": {"root": 38, "triad": (62, 65, 69), "upper": (65, 69),
            "high": (74, 77, 81), "scale": (0, 2, 3, 5, 7, 9, 10)},
}

# 8 小节一循环的王道进行
ROYAL_ROAD = ("F", "G", "Em", "Am", "F", "G", "C", "Dm7")

# 各段用哪套进行（都基于王道进行，只改长度）
PROGS = {
    "INTRO": ("F", "G", "Em", "Am"),
    "V1": ROYAL_ROAD,
    "PRE": ("F", "G", "Em", "Am"),
    "CHO": ROYAL_ROAD,
    "BREAK": ("F", "G", "C", "Dm7"),
    "V2": ROYAL_ROAD,
    "CHO2": ROYAL_ROAD,
    "OUTRO": ("F", "G"),
}


def _section_of(bar: int) -> tuple[str, int]:
    """返回 (段名, 段内小节号)。越界**大声报错**，不静默兜底。

    静默兜底真出过事：越界查询会悄悄拿到别的段的和弦，把旋律音阶算错，
    导致本该只在副歌响的旋律跑进间奏段和其他声部撞车。
    """
    if bar < 0 or bar >= TOTAL_BARS:
        raise ValueError(
            f"小节号 {bar} 越界（曲子共 {TOTAL_BARS} 小节）—— "
            f"某个循环的边界算错了")
    for name, b0, b1, _d in SECTIONS:
        if b0 <= bar < b1:
            return name, bar - b0
    best = max((s for s in SECTIONS if s[1] <= bar), key=lambda s: s[1])
    return best[0], bar - best[1]


def chord_of(bar: int) -> str:
    name, i = _section_of(bar)
    prog = PROGS[name]
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


# ── 16 复音预算：每一层占几个音，加起来必须 ≤ 16 ──────────────────────
#
# 这是硬约束，所以每层都得"窄"：长音层只取和弦音的一部分，
# 而且**各层音区错开**，不堆在同一个八度里（既省复音又不糊）。
#
#   层            音符数   内容                      音区
#   ─────────────────────────────────────────────────────────
#   钢琴          3       根音 + 三音 + 五音          F3-A4
#   电钢          2       三音 + 五音（高八度）       高一个八度
#   合成垫        2       五音 + 根音（高八度）       更高，做空气感
#   弦乐          2       根音 + 三音（低八度）       低八度，做厚度
#   （以上长音层小计 = 9）
#   贝斯          1       根音
#   主奏旋律      1-3     单音线条（长音处会和下一个音叠到 2-3）
#   铜管和声      1       三度，只在副歌
#   吉他          1-2     16 分切分（音符短，重叠少）
#   鼓            ~4      打击乐
#   ─────────────────────────────────────────────────────────
#   副歌最密处实测 ≈ 16（用 tools/ps5_polyphony.py 验证）
#
# ★ 音高一律**不重复**：同一个音高如果被两层同时按下，先到的 note-off
#   会把后一个音也关掉 —— 硬件音源上就是挂音。所以每层取的和弦音
#   都是不同的一批。


def piano_layer(s: Score, bar: int, vel: int = 84):
    """钢琴：根音 + 三音 + 五音。占 3 个音。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]                     # 例如 F: (65, 69, 72)
    s.chord(bar * BEATS_PER_BAR, 3.9, gm.CH_PIANO, (t[0], t[1], t[2]), vel)


def epiano_layer(s: Score, bar: int, vel: int = 66):
    """电钢：三音 + 五音（高八度）。占 2 个音。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    s.chord(bar * BEATS_PER_BAR, 3.9, gm.CH_EPIANO, (t[1] + 12, t[2] + 12), vel)


def synthpad_layer(s: Score, bar: int, vel: int = 58):
    """合成垫：五音 + 高八度根音。占 2 个音 —— 中高频的空气感。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    s.add(b0, 3.9, gm.CH_SYNTHPAD, t[2] + 12, vel)
    s.add(b0, 3.9, gm.CH_SYNTHPAD, c["root"] + 36, vel - 8)


def strings_layer(s: Score, bar: int, vel: int = 54):
    """弦乐：根音 + 三音（低八度）。占 2 个音 —— 厚度的底。"""
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    s.chord(bar * BEATS_PER_BAR, 3.9, gm.CH_STRINGS,
            (t[0] - 12, t[1] - 12), vel)


def choir_layer(s: Score, bar: int, vel: int = 46):
    """合唱：高八度五音。占 1 个音 —— 只在高频点一下，不占预算。"""
    c = CHORDS[chord_of(bar)]
    s.add(bar * BEATS_PER_BAR, 3.9, gm.CH_CHOIR, c["triad"][2] + 24, vel)


# ── 节奏声部 ──────────────────────────────────────────────────────────
def piano_comp(s: Score, bar: int, dens: int = 8, vel: int = 78):
    """钢琴切分伴奏（比长音更有律动）—— 主歌用。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    step = BEATS_PER_BAR / dens
    for i in range(dens):
        v = vel if i % 2 == 0 else vel - 16
        s.chord(b0 + i * step, step * 0.9, gm.CH_PIANO, c["triad"][:2], v)
        if i % 2 == 0:
            s.add(b0 + i * step, step * 1.6, gm.CH_PIANO, c["root"], v - 8)


def gtr_comp(s: Score, bar: int, vel: int = 70):
    """清音吉他从第 2 个八分音符起切分。占 1 个音。

    ★ 刻意**避开正拍**：正拍上已经有钢琴、贝斯、底鼓、吊镲，吉他再叠
      一个音就会把复音数顶破 16。从反拍进既省预算，也更接近 J-POP 里
      吉他扫弦的实际编法。
    """
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    for i in range(1, 8):
        s.add(b0 + i * 0.5, 0.4, gm.CH_GTR, c["high"][0],
              vel if i % 2 == 1 else vel - 12)


def bass_line(s: Score, bar: int, vel: int = 108, busy: bool = False):
    """贝斯：八分音符，副歌更花。"""
    c = CHORDS[chord_of(bar)]
    root = c["root"]
    pat = (0, 12, 0, 7, 0, 12, 7, 12) if busy else (0, 0, 12, 0, 0, 7, 0, 12)
    b0 = bar * BEATS_PER_BAR
    for i, off in enumerate(pat):
        s.add(b0 + i * 0.5, 0.42, gm.CH_BASS, root + off,
              vel if i % 2 == 0 else vel - 12)


def drums(s: Score, bar: int, dense: bool = False):
    b0 = bar * BEATS_PER_BAR
    kicks = (0.0, 0.75, 1.5, 2.0, 2.75, 3.5) if dense else (0.0, 1.5, 2.0, 3.5)
    for k in kicks:
        s.add(b0 + k, 0.22, gm.CH_DRUMS, gm.KICK,
              112 if k in (0.0, 2.0) else 96)
    for sn in (1.0, 3.0):
        s.add(b0 + sn, 0.22, gm.CH_DRUMS, gm.SNARE, 108)
    n = 16 if dense else 8
    for i in range(n):
        t = b0 + i * (0.25 if dense else 0.5)
        hat = gm.OPEN_HAT if (dense and i == 15) else gm.CLOSED_HAT
        s.add(t, 0.16, gm.CH_DRUMS, hat, 66 if i % 4 == 0 else 48)
    if dense and bar % 4 == 0:
        # 吊镲刻意做短（0.4 拍而非 0.9）：正拍上钢琴/贝斯/底鼓/铜管已经
        # 挤在一起，镲再拖长半拍就会把复音数顶过 16。
        s.add(b0, 0.4, gm.CH_DRUMS, gm.CRASH, 116)
    if bar % 8 == 7:
        # 过门从第 4 拍反拍(3.25)起 —— 从 3.0 起会撞正拍军鼓同音高，
        # 先到的 note-off 把后一个音关掉 = 挂音。这个坑踩过太多次。
        for j, p in enumerate((gm.SNARE2, gm.HIGH_TOM, gm.LOW_TOM)):
            s.add(b0 + 3.25 + j * 0.25, 0.2, gm.CH_DRUMS, p, 100 + j * 4)


# ── 旋律（16 拍一句，每 4 小节放一次）──────────────────────────────────
MEL_VERSE = (
    (0.0, 0.45, 4, 92), (0.5, 0.45, 4, 84), (1.0, 0.9, 5, 96),
    (2.0, 0.45, 7, 94), (2.5, 0.45, 5, 86), (3.0, 0.9, 4, 90),
    (4.0, 0.45, 2, 88), (4.5, 0.45, 4, 86), (5.0, 1.4, 5, 94),
    (6.5, 0.45, 4, 88), (7.0, 0.9, 2, 86),
    (8.0, 0.45, 4, 94), (8.5, 0.45, 5, 88), (9.0, 0.9, 7, 100),
    (10.0, 0.45, 5, 92), (10.5, 0.45, 4, 88), (11.0, 1.3, 2, 92),
    (12.5, 0.45, 7, 96), (13.0, 0.45, 5, 88), (13.5, 0.45, 4, 86),
    # 收尾留空隙：乐句长 16 拍，最后一个音必须在第 16 拍前结束
    (14.0, 1.0, 0, 90),
)

MEL_CHORUS = (
    (0.0, 0.45, 7, 108), (0.5, 0.45, 9, 100), (1.0, 0.9, 11, 116),
    (2.0, 0.9, 9, 106), (3.0, 0.9, 7, 110),
    (4.0, 0.45, 9, 106), (4.5, 0.45, 11, 102), (5.0, 1.4, 12, 118),
    (6.5, 0.45, 11, 104), (7.0, 0.9, 9, 108),
    (8.0, 0.45, 11, 112), (8.5, 0.45, 12, 106), (9.0, 0.9, 14, 120),
    (10.0, 0.9, 12, 110), (11.0, 1.4, 11, 114),
    (12.5, 0.45, 9, 104), (13.0, 0.45, 7, 100), (13.5, 0.45, 5, 98),
    (14.0, 1.0, 7, 112),
)

MEL_PRE = (
    (0.0, 0.9, 4, 92), (1.0, 0.9, 5, 96), (2.0, 0.9, 7, 100), (3.0, 0.9, 9, 104),
    (4.0, 0.9, 5, 96), (5.0, 0.9, 7, 100), (6.0, 0.9, 9, 106), (7.0, 0.9, 11, 110),
)

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
    """把 16 拍乐句放到 bar 处。

    max_beats 用于段边界处截断 —— 不截的话最后一句会跨过段线伸进下一段，
    和下一段的声部在同一通道同音高撞车（这个 bug 在 solister 上真发生过）。
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


def melody_third(s: Score, bar: int, phrase, octave: int, vel_scale: float = 0.82,
                 max_beats: float | None = None):
    """副歌的三度和声（长号），叠在旋律下方三度。"""
    b0 = bar * BEATS_PER_BAR
    for off, dur, deg, vel in phrase:
        if max_beats is not None and off >= max_beats:
            break
        if max_beats is not None:
            dur = min(dur, max_beats - off)
            if dur <= 0.05:
                continue
        local_bar = bar + int(off // BEATS_PER_BAR)
        s.add(b0 + off, dur * 0.94, gm.CH_TROMBONE,
              scale_note(chord_of(local_bar), deg - 2, octave),
              min(127, int(vel * vel_scale)))


# ── 曲式 ──────────────────────────────────────────────────────────────
SECTIONS = (
    ("INTRO", 0, 6, "钢琴 + 电钢，极简"),
    ("V1", 6, 18, "贝斯/鼓/弦乐进"),
    ("PRE", 18, 22, "往上堆"),
    ("CHO", 22, 34, "全奏 —— 16 复音"),
    ("BREAK", 34, 38, "抽空，钢琴 + 合唱"),
    ("V2", 38, 50, "比 V1 厚"),
    ("CHO2", 50, 58, "全奏 + 主奏升八度"),
    ("OUTRO", 58, 60, "收尾"),
)

TOTAL_BARS = SECTIONS[-1][2]
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR
DURATION_SEC = TOTAL_BEATS * 60.0 / BPM

# 哪些段用"全奏"（16 复音在这里）
FULL_SECTIONS = ("CHO", "CHO2")


def _sec_end(name: str) -> int:
    return next(b1 for nm, _b0, b1, _d in SECTIONS if nm == name)


def _melody_limit(bar: int, interval: int = MELODY_INTERVAL_BARS) -> float | None:
    """乐句 16 拍，段内放不下就返回可用的拍数。"""
    name, _i = _section_of(bar)
    room = (_sec_end(name) - bar) * BEATS_PER_BAR
    return room if room < 16 else None


def build_score() -> Score:
    s = Score()

    # ── INTRO：只有钢琴 + 电钢 ──
    for bar in range(0, 6):
        piano_comp(s, bar, dens=8, vel=72)
        epiano_layer(s, bar, 60)
        if bar >= 3:
            synthpad_layer(s, bar, 54)

    # ── V1：贝斯/鼓/弦乐进 ──
    for bar in range(6, 18):
        piano_layer(s, bar, 82)
        epiano_layer(s, bar, 62)
        strings_layer(s, bar, 52)
        bass_line(s, bar, 104)
        drums(s, bar, dense=False)
        if bar >= 10 and bar % MELODY_INTERVAL_BARS == 2:
            melody(s, bar, MEL_VERSE, gm.CH_LEAD, 0, 1.0,
                   max_beats=_melody_limit(bar))

    # ── PRE：往上堆，加合成垫 ──
    for bar in range(18, 22):
        piano_layer(s, bar, 86)
        epiano_layer(s, bar, 66)
        synthpad_layer(s, bar, 58)
        strings_layer(s, bar, 54)
        bass_line(s, bar, 108)
        drums(s, bar, dense=False)
        if bar % 2 == 0:
            melody(s, bar, MEL_PRE[:4] if bar % 4 == 0 else MEL_PRE[4:],
                   gm.CH_LEAD, 0, 1.0)

    # ── CHO：全奏，16 复音 ──
    for bar in range(22, 34):
        _full_bar(s, bar, lead_octave=0, harmony=True)

    # ── BREAK：抽空，只留钢琴 + 合唱 ──
    for bar in range(34, 38):
        piano_layer(s, bar, 66)
        choir_layer(s, bar, 50)
        # ★ 乐句长 16 拍（4 小节），所以**必须每 4 小节放一次**。
        #   原来写 bar % 2 == 0，相邻两次乐句重叠一半，同音高撞车 → 挂音。
        #   这个坑在 piece / serene / jpop 里都踩过。
        if bar % MELODY_INTERVAL_BARS == 0:
            melody(s, bar, MEL_VERSE, gm.CH_LEAD, 0, 0.85,
                   max_beats=_melody_limit(bar))

    # ── V2：比 V1 厚一层（加合成垫和吉他）──
    for bar in range(38, 50):
        piano_layer(s, bar, 84)
        epiano_layer(s, bar, 64)
        synthpad_layer(s, bar, 56)
        strings_layer(s, bar, 54)
        gtr_comp(s, bar, 66)
        bass_line(s, bar, 108)
        drums(s, bar, dense=False)
        if bar >= 42 and bar % MELODY_INTERVAL_BARS == 2:
            melody(s, bar, MEL_VERSE, gm.CH_LEAD, 0, 1.0,
                   max_beats=_melody_limit(bar))

    # ── CHO2：全奏 + 主奏升八度 ──
    for bar in range(50, 58):
        _full_bar(s, bar, lead_octave=1, harmony=True)

    # ── OUTRO ──
    for bar in range(58, 60):
        piano_layer(s, bar, 78)
        synthpad_layer(s, bar, 56)
        strings_layer(s, bar, 56)
        choir_layer(s, bar, 48)
        s.add(bar * BEATS_PER_BAR, 3.6, gm.CH_BASS,
              CHORDS[chord_of(bar)]["root"], 96)
        if bar == 59:
            s.add(bar * BEATS_PER_BAR, 1.2, gm.CH_DRUMS, gm.CRASH, 116)

    return dedupe(s.events, Score)


def _full_bar(s: Score, bar: int, lead_octave: int, harmony: bool) -> None:
    """全奏一小节 —— 16 复音的铺底全在这。

    同时发声数（设计值）：
      钢琴 4 + 电钢 2 + 合成垫 3 + 弦乐 3 + 合唱 2 = 14 个长音
      + 贝斯 1 + 主奏 1（+ 三度和声 1）+ 吉他（16 分，密集）
    """
    piano_layer(s, bar, 88)
    epiano_layer(s, bar, 68)
    synthpad_layer(s, bar, 62)
    strings_layer(s, bar, 58)
    choir_layer(s, bar, 52)
    bass_line(s, bar, 112, busy=True)
    drums(s, bar, dense=True)
    gtr_comp(s, bar, 74)
    if bar % MELODY_INTERVAL_BARS == 0:
        lim = _melody_limit(bar)
        melody(s, bar, MEL_CHORUS, gm.CH_LEAD, lead_octave, 1.0, max_beats=lim)
        if harmony:
            melody_third(s, bar, MEL_CHORUS, lead_octave, 0.8, max_beats=lim)


# ── 声部设置 ──────────────────────────────────────────────────────────
# 声像全部 64（居中）；一个通道一种乐器（ps5_channels.py 会检查）
SETUP = (
    (gm.CH_PIANO, gm.PIANO, 100, 64, 40, "钢琴"),
    (gm.CH_EPIANO, gm.EPIANO, 76, 64, 54, "电钢"),
    (gm.CH_SYNTHPAD, gm.SLOW_STRINGS, 68, 64, 66, "合成垫"),
    (gm.CH_STRINGS, gm.STRINGS, 76, 64, 78, "弦乐"),
    (gm.CH_CHOIR, gm.CHOIR, 64, 64, 84, "合唱"),
    (gm.CH_BASS, gm.PICK_BASS, 112, 64, 14, "贝斯"),
    (gm.CH_GTR, 27, 78, 64, 44, "清音吉他"),
    (gm.CH_LEAD, 81, 104, 64, 46, "主音合成器"),
    (gm.CH_TROMBONE, gm.BRASS, 82, 64, 48, "铜管和声"),
    (gm.DRUM_CHANNEL, 0, 108, 64, 20, "鼓组"),
)

_B = {name: b0 * BEATS_PER_BAR for name, b0, _b1, _d in SECTIONS}
SECTION_MIX = (
    (_B["CHO"], gm.CH_STRINGS, 84, "副歌：弦乐顶出来"),
    (_B["BREAK"], gm.CH_STRINGS, 0, "BREAK：弦乐停"),
    (_B["BREAK"], gm.CH_SYNTHPAD, 0, "BREAK：合成垫停"),
    (_B["V2"], gm.CH_STRINGS, 80, "V2：弦乐回"),
    (_B["V2"], gm.CH_SYNTHPAD, 68, "V2：合成垫回"),
    (_B["CHO2"], gm.CH_LEAD, 112, "最后一遍副歌：主奏再推"),
)
assert max(b for b, _c, _v, _l in SECTION_MIX) < TOTAL_BEATS, \
    "SECTION_MIX 越界 —— 会让播放多出一截静音"


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
