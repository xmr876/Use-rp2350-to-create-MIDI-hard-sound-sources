#!/usr/bin/env python3
"""
smf.py — 标准 MIDI 文件（SMF）写出器，纯标准库。

为什么自己写：PS5 只吃卡上 SMF 文件夹里的 .MID 文件（8.3 文件名），
而不想为这件事引入 mido / music21 这种依赖。

格式：SMF Format 1（一个 tempo 轨 + 每通道一个轨），
      分辨率 480 ticks/四分音符。

事件用「拍」表示（1.0 = 一个四分音符），内部换算成 tick。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

TPQ = 480                    # ticks per quarter note
DEFAULT_TEMPO_US = 600_000   # 100 BPM


def _vlq(n: int) -> bytes:
    """MIDI 可变长度量。"""
    if n < 0:
        raise ValueError("delta 不能为负")
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


@dataclass
class Event:
    """一个 MIDI 事件。beat 是绝对拍位置。"""
    beat: float
    kind: str                    # "on" | "off" | "cc" | "prog" | "sysex" | "meta"
    ch: int = 0
    d1: int = 0
    d2: int = 0
    data: bytes = b""
    order: int = 1               # 同一拍内的排序权重（0=先，2=后）

    def sort_key(self):
        return (round(self.beat * TPQ), self.order)


@dataclass
class Track:
    name: str
    events: list[Event] = field(default_factory=list)

    def add(self, ev: Event) -> None:
        self.events.append(ev)


# ── 便捷构造 ──────────────────────────────────────────────────────────
def note(track: Track, beat: float, dur: float, ch: int, pitch: int, vel: int = 90):
    """加一个音。note-off 排在同时刻其它事件之后，避免同音被立刻关掉。"""
    vel = max(1, min(127, vel))
    track.add(Event(beat, "on", ch, pitch, vel, order=2))
    track.add(Event(beat + dur, "off", ch, pitch, 0, order=0))


def chord(track: Track, beat: float, dur: float, ch: int, pitches, vel: int = 90):
    for p in pitches:
        note(track, beat, dur, ch, p, vel)


def cc(track: Track, beat: float, ch: int, num: int, val: int):
    track.add(Event(beat, "cc", ch, num, max(0, min(127, val)), order=0))


def program(track: Track, beat: float, ch: int, prog: int):
    track.add(Event(beat, "prog", ch, prog & 0x7F, 0, order=0))


# ── 写出 ──────────────────────────────────────────────────────────────
def _encode_track(name: str, events: list[Event]) -> bytes:
    body = bytearray()
    # 轨名 meta 事件
    nm = name.encode("latin-1", "replace")[:127]
    body += _vlq(0) + b"\xFF\x03" + _vlq(len(nm)) + nm

    last_tick = 0
    for ev in sorted(events, key=Event.sort_key):
        tick = round(ev.beat * TPQ)
        delta = max(0, tick - last_tick)
        last_tick = tick

        if ev.kind == "on":
            msg = bytes([0x90 | (ev.ch & 0x0F), ev.d1 & 0x7F, ev.d2 & 0x7F])
        elif ev.kind == "off":
            # 用 note-on + velocity 0，兼容性最好（PS5 的 chart 里 Note OFF 是 x）
            msg = bytes([0x90 | (ev.ch & 0x0F), ev.d1 & 0x7F, 0])
        elif ev.kind == "cc":
            msg = bytes([0xB0 | (ev.ch & 0x0F), ev.d1 & 0x7F, ev.d2 & 0x7F])
        elif ev.kind == "prog":
            msg = bytes([0xC0 | (ev.ch & 0x0F), ev.d1 & 0x7F])
        elif ev.kind == "sysex":
            msg = bytes([0xF0]) + _vlq(len(ev.data)) + ev.data + bytes([0xF7])
        elif ev.kind == "meta":
            msg = b"\xFF" + bytes([ev.d1]) + _vlq(len(ev.data)) + ev.data
        else:
            raise ValueError(f"未知事件类型 {ev.kind}")

        body += _vlq(delta) + msg

    # 轨尾
    body += _vlq(0) + b"\xFF\x2F\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def write_smf(path: str, tracks: list[Track], tempo_us: int = DEFAULT_TEMPO_US,
              time_sig=(4, 4), gm_reset: bool = True) -> None:
    """写出 Format 1 的 SMF。第一轨是 tempo 轨。

    gm_reset=True 会在最前面插一个 GM System On（F0 7E 7F 09 01 F7）。
    这是 GM 文件的标准做法，能让音源（包括 PS5）切回标准 GM 状态再放，
    避免上一次留下的移调/音色映射影响这一首。
    """
    tempo_track = Track("Tempo")
    if gm_reset:
        # data 不含 F0/F7，_encode_track 会自动补
        tempo_track.add(Event(0.0, "sysex", data=bytes([0x7E, 0x7F, 0x09, 0x01])))
    tempo_track.add(Event(0.0, "meta", d1=0x51, data=struct.pack(">I", tempo_us)[1:]))
    num, den = time_sig
    tempo_track.add(Event(0.0, "meta", d1=0x58,
                          data=bytes([num, max(0, den.bit_length() - 1), 24, 8])))

    chunks = [_encode_track("Tempo", tempo_track.events)]
    for t in tracks:
        if t.events:
            chunks.append(_encode_track(t.name, t.events))

    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), TPQ)
    with open(path, "wb") as f:
        f.write(header)
        for c in chunks:
            f.write(c)


# ── 读回来自检 ────────────────────────────────────────────────────────
def read_smf(path: str) -> dict:
    """极简 SMF 解析，用来验证写出的文件是合法的。"""
    d = open(path, "rb").read()
    if d[:4] != b"MThd":
        raise ValueError("没有 MThd")
    hlen, fmt, ntrk, div = struct.unpack(">IHHH", d[4:14])
    pos = 8 + hlen
    tracks, total = [], 0

    for _ in range(ntrk):
        if d[pos:pos + 4] != b"MTrk":
            raise ValueError(f"位置 {pos} 不是 MTrk")
        tlen = struct.unpack(">I", d[pos + 4:pos + 8])[0]
        body, pos = d[pos + 8:pos + 8 + tlen], pos + 8 + tlen
        i = 0
        tick = 0
        evs = []

        def rd_vlq():
            nonlocal i
            v = 0
            while True:
                b = body[i]; i += 1
                v = (v << 7) | (b & 0x7F)
                if not b & 0x80:
                    return v

        while i < len(body):
            tick += rd_vlq()
            st = body[i]
            if st == 0xFF:
                i += 1
                mt = body[i]; i += 1
                ln = rd_vlq()
                evs.append((tick, "meta", mt, body[i:i + ln]))
                i += ln
                if mt == 0x2F:
                    break
            elif st in (0xF0, 0xF7):
                i += 1
                ln = rd_vlq(); i += ln
                evs.append((tick, "sysex", None, b""))
            else:
                hi = st & 0xF0
                ch = st & 0x0F
                i += 1
                if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    evs.append((tick, hi, ch, (body[i], body[i + 1]))); i += 2
                elif hi in (0xC0, 0xD0):
                    evs.append((tick, hi, ch, (body[i],))); i += 1
                else:
                    raise ValueError(f"坏状态字节 {st:#x} @ {i}")
        tracks.append(evs)
        total += len(evs)
    return {"format": fmt, "ntracks": ntrk, "division": div,
            "tracks": tracks, "events": total,
            "end_beat": max((t[-1][0] for t in tracks if t), default=0) / div}
