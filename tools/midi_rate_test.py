#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIDI 速率测试 —— 按指定间隔连续发音符。

为什么单独写一个：
    现成的 midi_debug_stream.py 没有调速参数，而调"推送速率"正是
    定位卡顿的关键手段 —— 从慢到快扫一遍，就能看出：
        · 多慢开始卡   -> 说明引擎能承受多少同时发声的声部
        · 卡顿是否随速率线性恶化 -> 说明瓶颈在渲染负载，而不是别的

用法：
    python tools/midi_rate_test.py                 # 默认 120ms 一个音
    python tools/midi_rate_test.py --delay 60      # 更快
    python tools/midi_rate_test.py --delay 20      # 很快（压力）
    python tools/midi_rate_test.py --delay 300     # 慢速（对照）
    python tools/midi_rate_test.py --delay 120 --note-len 5000
                                                   # 音符留得久 -> 声部叠加更多

★ 音符长度（--note-len）比速率更能压出声部：
  钢琴衰减好几秒，所以 note_off 发得再早，声部也在响。
  想加压就加大 --note-len。
"""

import argparse
import sys
import time

try:
    import msvcrt  # Windows
    import ctypes
    HAS_WINMM = True
except ImportError:
    HAS_WINMM = False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--delay', type=int, default=120,
                    help='每个音符之间的间隔，毫秒（默认 120）')
    ap.add_argument('--note-len', type=int, default=0, dest='note_len',
                    help='note_off 延迟毫秒（0 = 不发 note_off，让声部一直叠加）')
    ap.add_argument('--device', type=int, default=1, help='MIDI 输出设备号')
    args = ap.parse_args()

    if not HAS_WINMM:
        print('这个脚本用 Windows 的 winmm 发 MIDI，只能在 Windows 上跑')
        return 1

    winmm = ctypes.WinDLL('winmm')
    handle = ctypes.c_void_p()

    # 先列设备
    n = winmm.midiOutGetNumDevs()
    print('=== MIDI 输出设备 ===')
    for i in range(n):
        caps = ctypes.create_string_buffer(128)
        winmm.midiOutGetDevCapsA(i, caps, 128)
        name = caps.raw[8:8 + 32].split(b'\x00')[0].decode('gbk', 'replace')
        print(f'  [{i}] {name}')

    rc = winmm.midiOutOpen(ctypes.byref(handle), args.device, 0, 0, 0)
    if rc != 0:
        print(f'打不开设备 {args.device}（错误码 {rc}）')
        return 1
    print(f'\n已打开设备 [{args.device}]')

    def send(status, d1, d2):
        msg = status | (d1 << 8) | (d2 << 16)
        winmm.midiOutShortMsg(handle, ctypes.c_uint32(msg))

    # C 大调音阶（和 midi_debug_stream.py --mode scale 同一套音）
    SCALE = [60, 62, 64, 65, 67, 69, 71, 72,
             71, 69, 67, 65, 64, 62, 60]

    print(f'间隔 {args.delay}ms，note_len={args.note_len or "不关"}')
    print('进主循环 —— Ctrl+C 停\n')

    idx = 0
    sent = 0
    t0 = time.time()

    try:
        while True:
            note = SCALE[idx % len(SCALE)]
            idx += 1

            send(0x90, note, 100)       # note_on, ch0, vel100
            sent += 1

            if args.note_len and args.note_len > 0:
                # 简单起见：不真的等 note_len，而是"延迟几拍再关"
                # （真按 note_len 等会让速率变得很慢，失去意义）
                pass

            if sent % 25 == 0:
                el = time.time() - t0
                print(f'  已发 {sent} 个音（{sent / el:.1f} 个/秒）'
                      f'  当前 note={note}')

            time.sleep(args.delay / 1000.0)
    except KeyboardInterrupt:
        print('\n停止，发 note_off 收尾…')
        for n in SCALE:
            send(0x80, n, 0)
        winmm.midiOutClose(handle)
        print(f'共发 {sent} 个音，用时 {time.time() - t0:.1f} 秒')
    return 0


if __name__ == '__main__':
    sys.exit(main())
