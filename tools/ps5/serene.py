#!/usr/bin/env python3
"""
serene.py — 悠扬风格曲，给 TASCAM Pocketstudio 5 的内藏 GM 音源。

和 piece.py / jrock.py 接口完全一致，所以播放器、验证脚本、钢琴卷帘图
都能直接套用（--piece serene）。

"悠扬"这个感觉是靠这几件事做出来的，不是随便放慢就能有：

  1. **速度极慢**：66 BPM。悠扬的前提是每个音都有时间衰减完
  2. **不用鼓**。打击乐一进来，绵长的感觉立刻断掉。
     铺底改用竖琴分解 + 合唱/弦乐长音
  3. **主调 g 小调，中段转关系大调 ♭B** —— 小调的忧郁 + 大调的开阔
  4. **尾段回小调，但最后落在 G 大调**（皮卡迪三度），收得有提拉感
  5. **旋律以级进和长音为主**，跳过四度以上的地方都留了缓冲音
  6. **大量留白**：INTRO 和 OUTRO 几乎只有钢琴，让空间说话

曲式（66 BPM，4/4，80 小节 ≈ 4 分 51 秒）：

    段        小节    时长     内容
    INTRO     1-12    43.6s   钢琴 + 竖琴分解，极简
    THEME     13-28   58.2s   主题（长笛）+ 弦乐垫 + 低音
    RISE      29-44   58.2s   转 ♭B 大调，加铜管长音与合唱
    PEAK      45-60   58.2s   主题再现（弦乐齐奏 + 马林巴八度加倍）
    OUTRO     61-80   72.7s   渐薄，收在 G 大调
                            ─────────
                            291.0s ≈ 4 分 51 秒

★ 如果嫌长，把 SECTIONS 里 PEAK 的结束小节从 60 改成 52（少 8 小节）
  就变成约 4 分 22 秒；把 RISE 也砍掉就约 3 分 20 秒。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import gmnames as gm
from .slicing import dedupe
from .smf import Track, cc, note, program, write_smf

BPM = 66
BEATS_PER_BAR = 4.0


# ── 和声 ──────────────────────────────────────────────────────────────
# triad: 三和弦（钢琴/弦乐用，中音区）
# high:  高八度三和弦（马林巴/竖琴/管钟用）
# bass:  低音（贝斯用）
# scale: 该小节的调式音阶（半音偏移），旋律取音用
CHORDS = {
    "Gm": {"triad": (67, 70, 74), "high": (79, 82, 86), "bass": 31,
           "scale": (0, 2, 3, 5, 7, 8, 10)},
    "Eb": {"triad": (63, 67, 70), "high": (75, 79, 82), "bass": 27,
           "scale": (0, 2, 4, 5, 7, 9, 10)},
    "Bb": {"triad": (58, 62, 65), "high": (70, 74, 77), "bass": 34,
           "scale": (0, 2, 4, 5, 7, 9, 11)},
    "F":  {"triad": (65, 69, 72), "high": (77, 81, 84), "bass": 29,
           "scale": (0, 2, 4, 5, 7, 9, 10)},
    "Cm": {"triad": (60, 63, 67), "high": (72, 75, 79), "bass": 36,
           "scale": (0, 2, 3, 5, 7, 8, 10)},
    "Dm": {"triad": (62, 65, 69), "high": (74, 77, 81), "bass": 38,
           "scale": (0, 2, 3, 5, 7, 8, 10)},
    "G":  {"triad": (67, 71, 74), "high": (79, 83, 86), "bass": 31,
           "scale": (0, 2, 4, 5, 7, 9, 11)},
}

# 每小节的进行（同一段内循环）
INTRO_PROG = ("Gm", "Eb", "Bb", "F")
THEME_PROG = ("Gm", "Eb", "Bb", "Dm")
RISE_PROG = ("Bb", "F", "Gm", "Eb")
PEAK_PROG = ("Gm", "Eb", "Bb", "F")
OUTRO_PROG = ("Gm", "Eb", "Cm", "G")

_PROG_TABLE = {
    "INTRO": INTRO_PROG, "THEME": THEME_PROG, "RISE": RISE_PROG,
    "PEAK": PEAK_PROG, "OUTRO": OUTRO_PROG,
}


def chord_of(bar: int) -> str:
    for name, b0, b1, _d in SECTIONS:
        if b0 <= bar < b1:
            prog = _PROG_TABLE[name]
            return prog[(bar - b0) % len(prog)]
    return THEME_PROG[bar % 4]


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


# ── 钢琴：流动的分解和弦 ──────────────────────────────────────────────
def piano_flow(s: Score, bar: int, dens: int = 6, vel: int = 62):
    """一小节内均匀铺 dens 个音，构成上下起伏的分解和弦。

    dens=6 时落在八分三连音的位置上 —— 这个密度是"流动"而不是
    "忙碌"的临界点，悠扬的织体基本都在这个量级。

    ★ 时值分两种：中间的音用 step×1.9（互相叠一点，听起来是连贯的
      "流动"而不是断开的点），但**最后一个音必须收在本小节内**。
      否则它会越界到下一小节开头（step×1.9 会多出 0.6 拍），
      和下一小节的长音/和弦在同一音高上撞车 —— 硬件音源上就是挂音。
      这个 bug 只在"分解和弦后面紧跟整小节长音"的段边界才暴露出来。
    """
    c = CHORDS[chord_of(bar)]
    t = c["triad"]
    b0 = bar * BEATS_PER_BAR
    step = BEATS_PER_BAR / dens
    # 低-中-高-中 的起伏轮廓，避免机械地上下行
    order = (t[0] - 12, t[1], t[2], t[1] + 12, t[2], t[1])
    for i in range(dens):
        p = order[i % len(order)]
        last = (i == dens - 1)
        dur = step * 0.95 if last else step * 1.9
        s.add(b0 + i * step, dur, gm.CH_PIANO, p,
              vel if i % 3 == 0 else vel - 12)


def piano_whole(s: Score, bar: int, vel: int = 50, dur: float = 3.95):
    """整小节长和弦 —— OUTRO 用，越简单越好。

    dur 默认 3.95（几乎铺满一小节）。但 OUTRO 的分界小节需要它短一点：
    前一小节的钢琴分解是按 step×1.9 的加长时值收尾的，会**越界**到
    下一小节开头，和这里的整小节长音重叠同一个和弦音高 → 撞车。
    """
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.chord(b0, dur, gm.CH_PIANO, c["triad"], vel)


# ── 竖琴：只在 INTRO 和 OUTRO 的亮点上 ────────────────────────────────
def harp_cascade(s: Score, bar: int, up: bool = True, vel: int = 54):
    """双手琶音：从中音区一路洒到高音区，再（或不）回来。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    notes = list(c["triad"]) + [p + 12 for p in c["high"][:3]]
    if not up:
        notes = list(reversed(notes))
    step = BEATS_PER_BAR / len(notes)
    for i, p in enumerate(notes):
        s.add(b0 + i * step, step * 2.4, gm.CH_HARP, p, vel - i % 3 * 6)


# ── 弦乐 / 合唱 / 暖垫：长音铺底 ──────────────────────────────────────
def pad_whole(s: Score, bar: int, ch: int, octave: int = 0,
              vel: int = 46, upper_only: bool = False):
    """整小节长音。

    upper_only=True 时只取三音五音（省掉根音）—— 铺底跟旋律或贝斯
    撞同一个音高时，两个音会互相抢 note-off，在硬件音源上就是挂音。
    省掉根音是最省事的规避方式。
    """
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    pitches = c["triad"][1:] if upper_only else c["triad"]
    s.chord(b0, 3.95, ch, [p + 12 * octave for p in pitches], vel)


# ── 贝斯：整小节长音，只在需要时轻轻换一次 ────────────────────────────
def bass_whole(s: Score, bar: int, vel: int = 74):
    c = CHORDS[chord_of(bar)]
    s.add(bar * BEATS_PER_BAR, 3.9, gm.CH_BASS, c["bass"], vel)


def bass_walk(s: Score, bar: int, vel: int = 78):
    """第 3 拍加一个五度 —— 让低音有一点点推动，不破坏绵长感。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    s.add(b0, 1.9, gm.CH_BASS, c["bass"], vel)
    s.add(b0 + 2.0, 1.9, gm.CH_BASS, c["bass"] + 7, vel - 8)


# ── 旋律 ──────────────────────────────────────────────────────────────
# 格式 (相对起始拍, 时值, 音级, 力度)；音级 0 = 该小节调式主音
# 16 拍（4 小节）一句，所以每 4 小节才能放一次 —— 放密了会自我重叠挂音
MEL_THEME = (
    # 第一句：从主音爬上去，停在五音
    (0.0, 1.9, 0, 74), (2.0, 0.9, 2, 72), (3.0, 0.9, 4, 78),
    (4.0, 2.9, 7, 86),
    (8.0, 1.9, 5, 80), (10.0, 0.9, 4, 76), (11.0, 0.9, 2, 72),
    # ★ 这里原来是 3.4 —— 延伸到第 15.2 拍，而第 16 拍的音级 4 在 Dm 里
    #   也落在 60，两个同音高撞车（同一乐句内部！）。收到 1.5 解决。
    (12.0, 1.5, 0, 78),
    # 第二句：往高走，情绪抬起来
    (16.0, 1.9, 4, 82), (18.0, 0.9, 5, 84), (19.0, 0.9, 7, 90),
    (20.0, 2.9, 9, 96),
    (24.0, 1.9, 7, 88), (26.0, 0.9, 5, 82), (27.0, 0.9, 4, 78),
    (28.0, 2.0, 2, 80), (30.0, 1.8, 0, 74),
)

# 再现时更开阔：长音更多、音区更高
MEL_THEME_BIG = (
    (0.0, 2.9, 7, 88), (3.0, 0.9, 9, 92),
    (4.0, 3.9, 11, 100),
    (8.0, 2.9, 9, 92), (11.0, 0.9, 7, 86),
    (12.0, 3.9, 5, 88),
    (16.0, 2.9, 9, 94), (19.0, 0.9, 11, 98),
    (20.0, 3.9, 12, 104),
    (24.0, 2.9, 11, 96), (27.0, 0.9, 9, 88),
    # 同上：收尾不能超过第 32 拍
    (28.0, 1.8, 7, 90), (30.0, 1.6, 4, 82),
)

MELODY_INTERVAL_BARS = 4


def scale_note(chord: str, degree: int, octave: int = 0) -> int:
    """把音级映射成实际音高。以该和弦所在调式的主音为 0 级。"""
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
           vel_scale: float = 1.0):
    """把 16 拍长的乐句放到 bar 开始处，逐音取当前和弦的音阶音。"""
    b0 = bar * BEATS_PER_BAR
    for off, dur, deg, vel in phrase:
        local_bar = bar + int(off // BEATS_PER_BAR)
        pitch = scale_note(chord_of(local_bar), deg, octave)
        s.add(b0 + off, dur * 0.94, ch, pitch, min(127, int(vel * vel_scale)))


def chime(s: Score, bar: int):
    """管钟：每 4 小节在最高音上点一下，给悠扬加一点"亮"。"""
    c = CHORDS[chord_of(bar)]
    s.add(bar * BEATS_PER_BAR, 2.9, gm.CH_CHIME, c["high"][2] + 12, 52)


def marimba_echo(s: Score, bar: int):
    """马林巴：把和弦音在高八度轻轻重复一遍，做色彩对比。"""
    c = CHORDS[chord_of(bar)]
    b0 = bar * BEATS_PER_BAR
    for i, p in enumerate(c["high"]):
        s.add(b0 + 2.0 + i * 0.5, 1.4, gm.CH_MARIMBA, p, 52 - i * 4)


def brass_swell(s: Score, bar: int):
    """铜管长音：只在 RISE 段，一小节一个和弦，做渐强的推力。"""
    c = CHORDS[chord_of(bar)]
    s.chord(bar * BEATS_PER_BAR, 3.9, gm.CH_BRASS,
            (c["triad"][1] + 12, c["triad"][2] + 12), 64)


# ── 曲式 ──────────────────────────────────────────────────────────────
# ★ 约 3 分钟的精简版（当前使用）：
#   INTRO 12 + THEME 16 + RISE 8 + PEAK 16 + OUTRO 12 = 64 小节
#   64 × 4 拍 × 60 / 66 BPM = 232.7 秒 ≈ 3 分 53 秒
#
# 想换回完整版（80 小节 / 4 分 51 秒）：
#   RISE 改 (28, 44)，OUTRO 改 (60, 80)，
#   并把 build_score 里 RISE / OUTRO 的 for 区间同步改掉。
SECTIONS = (
    ("INTRO", 0, 12, "钢琴 + 竖琴分解，极简"),
    ("THEME", 12, 28, "主题（长笛）+ 弦乐垫"),
    ("RISE", 28, 36, "过渡：合唱铺底 + 长笛往上走"),
    ("PEAK", 36, 52, "主题再现，弦乐齐奏 + 马林巴加倍"),
    ("OUTRO", 52, 64, "渐薄，收在 G 大调"),
)

TOTAL_BARS = SECTIONS[-1][2]
TOTAL_BEATS = TOTAL_BARS * BEATS_PER_BAR
DURATION_SEC = TOTAL_BEATS * 60.0 / BPM


def build_score() -> Score:
    s = Score()

    for bar in range(0, 12):                     # INTRO
        piano_flow(s, bar, dens=6, vel=58)
        if bar >= 4:
            harp_cascade(s, bar, up=(bar % 8 < 4), vel=52)
        if bar >= 8:
            bass_whole(s, bar, 66)

    for bar in range(12, 28):                    # THEME
        piano_flow(s, bar, dens=6, vel=64)
        pad_whole(s, bar, gm.CH_STRINGS, vel=52)
        bass_whole(s, bar, 76)
        if bar % MELODY_INTERVAL_BARS == 0:
            melody(s, bar, MEL_THEME, gm.CH_LEAD, 0, 1.0)

    for bar in range(28, 36):                    # RISE（精简版：8 小节过渡）
        piano_flow(s, bar, dens=6, vel=58)
        pad_whole(s, bar, gm.CH_PAD, vel=44, upper_only=True)
        pad_whole(s, bar, gm.CH_CHOIR, vel=42, upper_only=True)
        bass_walk(s, bar, 78)
        brass_swell(s, bar)
        # ★ 一段里只放**同一种**乐句。曾经在这里交错用 MEL_THEME 和
        #   MEL_THEME_BIG，两者收尾长度不同，交界处前一句还没结束、
        #   后一句已经开始，同音高撞车 → 挂音。
        #   8 小节 = 2 个乐句（每 4 小节一次）。
        if bar % MELODY_INTERVAL_BARS == 0:
            melody(s, bar, MEL_THEME_BIG, gm.CH_LEAD, 0, 0.95)

    for bar in range(36, 52):                    # PEAK
        piano_flow(s, bar, dens=8, vel=60)
        pad_whole(s, bar, gm.CH_STRINGS, vel=58)
        pad_whole(s, bar, gm.CH_LOW, octave=0, vel=46, upper_only=True)
        bass_whole(s, bar, 82)
        marimba_echo(s, bar)
        if bar % MELODY_INTERVAL_BARS == 0:
            melody(s, bar, MEL_THEME_BIG, gm.CH_STRINGS, 1, 0.9)
        if bar % 8 == 4:
            chime(s, bar)

    for bar in range(52, 64):                    # OUTRO（精简版：12 小节）
        # 第 58 小节是两个分支的分界：之前是流动分解，之后只留整小节长音
        # （"越简单越好"）。这里的阈值必须和下面旋律、竖琴的判断协调：
        # 钢琴两个函数如果在同一个小节都被调用，分解和弦和长音会重叠，
        # 相同的和弦音高就会撞车 → 先到的 note-off 把后一个音关掉。
        if bar < 58:
            piano_flow(s, bar, dens=6, vel=54)
            pad_whole(s, bar, gm.CH_PAD, vel=42, upper_only=True)
            bass_whole(s, bar, 70)
        else:
            # piano_flow 的收尾音已经保证落在小节内了，这里不用再缩短
            piano_whole(s, bar, 46)
        if bar < 56 and bar % MELODY_INTERVAL_BARS == 0:
            melody(s, bar, MEL_THEME, gm.CH_LEAD, 0, 0.8)
        if 56 <= bar < 60 and bar % 4 == 0:
            harp_cascade(s, bar, up=True, vel=44)

    return dedupe(s.events, Score)


# ── 声部设置 ──────────────────────────────────────────────────────────
# ★ 一个通道只能有一种乐器。ps5_chick_piece / ps5_channels 会检查重复定义。
#
# ★ 声像（pan）全部 = 64（居中）。
#   64 是 GM 的居中值，左右声道等量输出。写曲子时不要为了"立体感"
#   把单个乐器推到一边 —— 监听只接一路、或者单声道回放时，
#   被推到另一边的乐器就整轨听不见了。
# (通道, 音色号, 音量, 声像, 混响量, 名字)
SETUP = (
    (gm.CH_PIANO, gm.PIANO, 96, 64, 58, "钢琴"),
    (gm.CH_BASS, gm.FINGER_BASS, 92, 64, 26, "贝斯"),
    (gm.CH_STRINGS, gm.STRINGS, 88, 64, 86, "弦乐"),
    (gm.CH_PAD, gm.WARM_PAD, 76, 64, 78, "暖垫"),
    (gm.CH_HARP, gm.HARP, 84, 64, 74, "竖琴"),
    (gm.CH_LEAD, gm.FLUTE, 90, 64, 66, "长笛"),
    (gm.CH_MARIMBA, gm.MARIMBA, 70, 64, 64, "马林巴"),
    (gm.CH_BRASS, gm.BRASS, 64, 64, 52, "铜管"),
    (gm.CH_CHOIR, gm.CHOIR, 72, 64, 88, "合唱"),
    (gm.CH_CHIME, gm.GLOCKEN, 76, 64, 82, "管钟"),
    (gm.CH_LOW, gm.LOW_STRINGS, 78, 64, 70, "低音弦乐"),
)

# 段落的层次变化：(拍, 通道, 音量, 说明)
#
# ★ 这里只能出现**曲子范围内**的拍号。曾经有一条 (72*4, ...) 是完整版
#   80 小节留下的残留，曲子砍到 64 小节后它还在，导致：
#     · live_events 的末尾被推到 288 拍（曲子只有 256 拍）
#     · 每次播放，音乐结束后还要空转 8 小节（29 秒）才结束
#     · 写出的 SMF 末拍也跟着变成 288
#   下面用 SECTIONS 的边界来生成，就再也不会和曲式脱节。
_PEAK_BEAT = SECTIONS[3][1] * BEATS_PER_BAR      # PEAK 段起始
_OUTRO_BEAT = SECTIONS[4][1] * BEATS_PER_BAR     # OUTRO 段起始
_OUTRO_MID = _OUTRO_BEAT + 6 * BEATS_PER_BAR     # OUTRO 中段（钢琴转长音处）

SECTION_MIX = (
    (_PEAK_BEAT, gm.CH_STRINGS, 96, "PEAK：弦乐顶出来"),
    (_PEAK_BEAT, gm.CH_LEAD, 0, "PEAK：长笛停，旋律交给弦乐"),
    (_OUTRO_BEAT, gm.CH_STRINGS, 72, "OUTRO：退下来"),
    (_OUTRO_BEAT, gm.CH_LEAD, 84, "OUTRO：长笛回来"),
    (_OUTRO_MID, gm.CH_PAD, 0, "OUTRO 后半：只留钢琴"),
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
