#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加速钢琴测试 —— 速度阶梯式递增，用来找出"多快开始卡"。

思路：
    卡顿和"同时发声的声部数"正相关，而声部数由**两个因素**决定：
        · 音符间隔（弹得多快）
        · 音符长度（每个音留多久 —— 钢琴衰减好几秒，所以留得久就叠得多）
    这个脚本固定"留音时长"，只让**间隔**阶梯式缩短，
    于是声部数就随速度单调上升。

    每换一档会**醒目地打印档位和参数** —— 卡的时候记下是哪一档，
    那个数字就是这台机器的实际承载上限。

用法：
    python tools/midi_ramp_test.py                # 从慢到快自动走完
    python tools/midi_ramp_test.py --hold 3000    # 留音更久 -> 更早卡
    python tools/midi_ramp_test.py --start 500    # 从更慢开始
"""

import argparse
import ctypes
import sys
import time

# 速度阶梯：每档 (间隔毫秒, 这一档弹几个音)
# 间隔越小 -> 弹得越快 -> 同时发声的声部越多
STAGES = [
    (500, 16),   # 极慢，几乎是单音
    (400, 16),
    (300, 16),
    (250, 16),
    (200, 16),
    (160, 16),
    (130, 16),
    (110, 16),
    (90,  16),
    (75,  16),
    (60,  16),
    (50,  16),
    (42,  16),
    (35,  16),
    (30,  24),
    (25,  24),
    (20,  32),
    (16,  32),
    (12,  32),
    (10,  32),
]

# 一段"像钢琴曲"的音型：C 大调音阶上下行 + 一个琶音上冲
# （比单纯音阶更像曲子，而且音高跨度大，能听出不同音区的差异）
PHRASE = [
    60, 62, 64, 65, 67, 69, 71, 72,     # 上行音阶
    71, 69, 67, 65, 64, 62, 60,         # 下行音阶
    60, 64, 67, 72, 67, 64,             # 琶音
    62, 65, 69, 74, 69, 65,             # 换个和弦再冲一次
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', type=int, default=1)
    ap.add_argument('--hold', type=int, default=2000,
                    help='每个音留多久才发 note_off（毫秒，默认 2000）')
    ap.add_argument('--start', type=int, default=None,
                    help='从这个间隔开始（毫秒），默认从 500 开始')
    ap.add_argument('--loop', action='store_true',
                    help='走完所有档位后从头再来一遍')
    args = ap.parse_args()

    winmm = ctypes.WinDLL('winmm')
    handle = ctypes.c_void_p()

    n = winmm.midiOutGetNumDevs()
    print('=== MIDI 输出设备 ===')
    for i in range(n):
        caps = ctypes.create_string_buffer(128)
        winmm.midiOutGetDevCapsA(i, caps, 128)
        name = caps.raw[8:8 + 32].split(b'\x00')[0].decode('gbk', 'replace')
        mark = '' if i == 0 else ''
        print(f'  [{i}] {name}{mark}')

    rc = winmm.midiOutOpen(ctypes.byref(handle), args.device, 0, 0, 0)
    if rc != 0:
        print(f'\n打不开设备 {args.device}（错误码 {rc}）')
        return 1
    print(f'\n已打开设备 [{args.device}]')

    def send(status, d1, d2):
        winmm.midiOutShortMsg(handle, ctypes.c_uint32(status | (d1 << 8) | (d2 << 16)))

    # 选钢琴
    send(0xC0, 0, 0)

    stages = STAGES
    if args.start is not None:
        stages = [s for s in stages if s[0] <= args.start]

    print(f'留音 {args.hold}ms —— 音留得越久，同时发声的声部越多\n')

    try:
        while True:
            for si, (delay, count) in enumerate(stages):
                # 估一下稳态声部数：留音时长 / 间隔（粗略）
                est = max(1, args.hold // delay)
                print()
                print('=' * 52)
                print(f'  第 {si + 1}/{len(stages)} 档   间隔 {delay} ms'
                      f'   约 {1000 / delay:.1f} 音/秒')
                print(f'  预计同时发声声部数 ≈ {est}')
                print('=' * 52)

                idx = 0
                pending = []      # (发note_on的序号, 音符)
                for k in range(count):
                    note = PHRASE[idx % len(PHRASE)]
                    idx += 1

                    send(0x90, note, 100)          # note_on

                    # ★ 按"实际发出的音符序列"关音，而不是按模式索引猜。
                    #   原来用 PHRASE[off_idx % len] 猜，会出现
                    #   "发了 note_on 但从来没发对应 note_off"
                    #   的情况 -> 声部永远不释放 -> 测出来的
                    #   "卡顿" 可能是**脚本自己造的**，而不是固件的问题。
                    #
                    #   pending 里存 (发 note_on 时的序号 k, 那个音符)，
                    #   到点了就按**当时真正发的那个音**去关。
                    due = k - (args.hold // delay)
                    while due >= 0 and pending and pending[0][0] <= due:
                        _, off_note = pending.pop(0)
                        send(0x80, off_note, 0)
                    pending.append((k, note))

                    if k % 8 == 0:
                        print(f'    [{k + 1:3d}/{count}] note {note:3d}'
                              f'  已发 {k + 1} 音', flush=True)

                    time.sleep(delay / 1000.0)

                # 这一档结束，收掉所有音，避免跨档累积影响下一档判断
                for nn in PHRASE:
                    send(0x80, nn, 0)
                time.sleep(0.35)

            if not args.loop:
                print('\n所有档位走完。')
                print('★ 记下"从哪一档开始卡" —— 那个"预计声部数"就是实际承载上限。')
                break

    except KeyboardInterrupt:
        print('\n停止…')
    finally:
        for nn in PHRASE:
            send(0x80, nn, 0)
        winmm.midiOutClose(handle)
    return 0


if __name__ == '__main__':
    sys.exit(main())
