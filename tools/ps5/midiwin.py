#!/usr/bin/env python3
"""
midiwin.py — Windows winmm 的 MIDI 输出薄封装（纯 ctypes，无需装库）。

和 tools/midi_debug_stream.py 里那个 MidiOut 的区别：
  · 这个支持 **SysEx**（GM System On 等），需要 midiOutPrepareHeader/
    midiOutLongMsg/midiOutUnprepareHeader 三件套
  · 这个的 all_notes_off 只发真正需要的通道/CC，不吞掉别的消息

PS5 的 MIDI Implementation Chart 里 System Exclusive 是 o（识别），
所以曲子上来先甩一个 GM System On 是安全的，能把它切回标准 GM 状态。

★ 端口占用问题（真踩过）：
  上一次播放的进程如果还没完全退出，winmm 句柄没释放，
  新进程会拿到**错误码 7**。表现是"两个端口只打开一个"，
  结果是只有一路出声 —— 很容易误判成线材或接线故障。
  所以这里提供 open_with_retry()，失败会自动重试。
"""
from __future__ import annotations

import time

import ctypes
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")


class MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
        ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


class MIDIHDR(ctypes.Structure):
    pass


MIDIHDR._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.POINTER(ctypes.c_ulong)),
    ("dwFlags", wintypes.DWORD),
    ("dwOffset", wintypes.DWORD),
    ("dwReserved", ctypes.POINTER(ctypes.c_ulong) * 8),
    ("lpNext", ctypes.POINTER(MIDIHDR)),
    ("reserved", ctypes.POINTER(ctypes.c_ulong)),
]

MIDIERR_STILLPLAYING = 65


def list_devices() -> list[tuple[int, str]]:
    out = []
    for i in range(winmm.midiOutGetNumDevs()):
        caps = MIDIOUTCAPS()
        if winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname))
    return out


def usb_devices() -> list[tuple[int, str]]:
    """排除微软自带合成器后的输出设备。"""
    return [(i, n) for i, n in list_devices()
            if "Microsoft" not in n and "GS Wavetable" not in n]


def pick_device(want: int | None = None) -> tuple[int, str]:
    devs = list_devices()
    if want is not None:
        for i, n in devs:
            if i == want:
                return i, n
        raise SystemExit(f"没有编号 {want} 的 MIDI 输出设备")
    usb = usb_devices()
    if not usb:
        raise SystemExit("只看到微软自带合成器，没找到 USB 转 MIDI 设备")
    return usb[0]


def open_with_retry(dev_id: int, tries: int = 4, delay: float = 0.8):
    """打开一个 MIDI 输出端口，失败就重试；彻底失败返回 None。

    为什么要重试：上一次播放的进程还没完全退出时，winmm 句柄没释放，
    新进程会拿到错误码 7。这个窗口通常不到一秒，但足够让"两个端口
    只打开一个"——结果只有一路出声，容易被当成线材故障。
    """
    last = None
    for k in range(tries):
        try:
            return MidiOut(dev_id)
        except SystemExit as e:
            last = e
            if k < tries - 1:
                time.sleep(delay)
    print(f"  ⚠ 端口 {dev_id} 重试 {tries} 次仍打不开：{last}")
    return None


def open_devices(all_usb: bool, want: int | None = None):
    """按 --all-devices / --device 的语义打开端口，带重试。

    返回 (targets, name)。一个都打不开时抛 SystemExit ——
    那种情况下继续跑只会静默没声音，不如直接报错。
    """
    if all_usb:
        targets = []
        for i, n in usb_devices():
            m = open_with_retry(i)
            if m is not None:
                targets.append(m)
                print(f"已打开 [{i}] {n}（第 {len(targets)} 路）")
        if not targets:
            raise SystemExit(
                "⚠ 所有 USB-MIDI 端口都打不开。\n"
                "   最常见原因：上一次播放的进程还没退出，占着 winmm 句柄。\n"
                "   处理：等一两秒重跑，或者用任务管理器结束残留的 python.exe")
        n_usb = len(usb_devices())
        if len(targets) < n_usb:
            print(f"  ⚠ 只打开了 {len(targets)}/{n_usb} 路 —— "
                  f"可能有端口被占用，这次会少一路出声")
        return targets, "全部设备"

    dev, name = pick_device(want)
    m = open_with_retry(dev)
    if m is None:
        raise SystemExit(f"⚠ 端口 [{dev}] {name} 打不开（可能被别的程序占用）")
    print(f"使用设备 [{dev}] {name}")
    return [m], name


class MidiOut:
    """一个 MIDI 输出端口。"""
    def __init__(self, dev_id: int):
        self.dev_id = dev_id
        self.h = wintypes.HANDLE()
        r = winmm.midiOutOpen(ctypes.byref(self.h), dev_id, 0, 0, 0)
        if r != 0:
            raise SystemExit(f"打不开 MIDI 设备 {dev_id}（winmm 错误码 {r}）")
        self._hdr = None
        self._buf = None

    # ── 短消息 ──
    def send(self, status: int, d1: int = 0, d2: int = 0) -> None:
        msg = ((d2 & 0xFF) << 16) | ((d1 & 0xFF) << 8) | (status & 0xFF)
        winmm.midiOutShortMsg(self.h, ctypes.c_ulong(msg))

    def note_on(self, ch, note, vel=100):
        self.send(0x90 | (ch & 0x0F), note & 127, vel & 127)

    def note_off(self, ch, note, vel=0):
        self.send(0x80 | (ch & 0x0F), note & 127, vel & 127)

    def program(self, ch, prog):
        self.send(0xC0 | (ch & 0x0F), prog & 127, 0)

    def cc(self, ch, num, val):
        self.send(0xB0 | (ch & 0x0F), num & 127, val & 127)

    def pitch_bend(self, ch, value):
        """value: -8192..8191"""
        v = max(-8192, min(8191, value)) + 8192
        self.send(0xE0 | (ch & 0x0F), v & 0x7F, (v >> 7) & 0x7F)

    # ── 长消息（SysEx）──
    def sysex(self, data) -> None:
        """data: 完整字节序列，含开头 F0 和结尾 F7。"""
        raw = bytes(data)
        if not raw.startswith(b"\xF0"):
            raw = b"\xF0" + raw
        if not raw.endswith(b"\xF7"):
            raw = raw + b"\xF7"

        self._buf = ctypes.create_string_buffer(raw, len(raw))
        hdr = MIDIHDR()
        hdr.lpData = ctypes.cast(self._buf, ctypes.c_char_p)
        hdr.dwBufferLength = len(raw)
        hdr.dwBytesRecorded = len(raw)
        hdr.dwFlags = 0
        hdr.dwOffset = 0
        if winmm.midiOutPrepareHeader(self.h, ctypes.byref(hdr),
                                      ctypes.sizeof(hdr)) != 0:
            return
        winmm.midiOutLongMsg(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        # 等它发完再 unprepare，否则 STILLPLAYING
        for _ in range(200):
            if hdr.dwFlags & 0x01:          # MHDR_DONE
                break
            ctypes.windll.kernel32.Sleep(1)
        winmm.midiOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        self._hdr = hdr

    def all_notes_off(self) -> None:
        """所有通道的 CC123（All Notes Off）+ CC120（All Sound Off）。"""
        for c in range(16):
            self.send(0xB0 | c, 123, 0)
            self.send(0xB0 | c, 120, 0)

    def reset(self) -> None:
        """GM System On 复位音源。"""
        self.sysex((0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7))

    def close(self) -> None:
        try:
            self.all_notes_off()
        except Exception:
            pass
        try:
            winmm.midiOutClose(self.h)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
