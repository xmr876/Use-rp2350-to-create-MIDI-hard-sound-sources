#!/usr/bin/env python3
"""
ps5_probe_smf.py — 生成一个极小、极简的 SMF 探针文件。

用途：把"文件本身有问题"和"文件没拷到正确位置"这两种情况分开。

  · 只有 1 条轨、几十个事件、不到 200 字节
  · 格式 0（最老最通用），慢速 C 大调音阶，循环 8 遍 ≈ 30 秒
  · 通道 1 + 钢琴音色 —— 万一 TgMode 还在 Pattern 模式（只收通道 1）也能响

如果这个小文件能加载能响 → 说明卡的路径和加载流程没问题，
之前那两首不响就是文件或格式的问题。
如果这个小文件也不响 → 问题在"文件放的位置"或"加载操作"上。
"""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, r"D:\音源")
sys.path.insert(0, r"D:\音源\tools")

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

TPQ = 480
BPM = 80


def vlq(n: int) -> bytes:
    out = bytearray([n & 0x7F])
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def main() -> int:
    events: list[tuple[int, bytes]] = []      # (tick, message)

    events.append((0, bytes([0xF0, 0x05, 0x7E, 0x7F, 0x09, 0x01, 0xF7])))
    events.append((0, bytes([0xC0, 0x00])))                 # ch1 = 钢琴
    events.append((0, bytes([0xB0, 0x07, 0x64])))           # 音量 100
    events.append((0, bytes([0xB0, 0x0A, 0x40])))           # 声像 居中

    scale = [60, 62, 64, 65, 67, 69, 71, 72, 71, 69, 67, 65, 64, 62, 60]
    beat = 0
    for rnd in range(4):                                     # 4 遍音阶
        if rnd:                                              # 从第二遍起先空 4 拍
            beat += 4
        for pitch in scale:                                  # 每遍 15 个音
            t = int(beat * TPQ)
            events.append((t, bytes([0x90, pitch, 100])))
            events.append((t + TPQ // 2, bytes([0x80, pitch, 0])))
            beat += 1
    # beat = 15 + 3×19 = 72 拍，最后一个音在第 72 拍上，没有多余空拍
    duration_beats = beat

    events.sort(key=lambda x: x[0])

    body = bytearray()
    body += vlq(0) + b"\xFF\x03" + vlq(8) + b"PS5PROBE"
    # 速度
    us = int(60_000_000 / BPM)
    body += vlq(0) + b"\xFF\x51\x03" + struct.pack(">I", us)[1:]
    # 拍号 4/4
    body += vlq(0) + b"\xFF\x58\x04" + bytes([4, 2, 24, 8])
    last = 0
    for tick, msg in events:
        body += vlq(tick - last) + msg
        last = tick
    body += vlq(TPQ * 2) + b"\xFF\x2F\x00"                   # 轨尾

    track = b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    # 格式 0：单轨，最通用
    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, TPQ)
    data = header + track

    out = r"D:\音源\build\SMF\PROBE.MID"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)

    secs = duration_beats * 60.0 / BPM
    # 时长别手算 —— 手算已经在"最后一个音落在第几拍"上错过两次。
    # 直接用 SMF 解析器把刚写的文件读回来，以文件里的真实数据为准。
    from ps5 import smf as _smf
    _info = _smf.read_smf(out)
    real_secs = _info["end_beat"] * 60.0 / BPM

    print(f"✅ 已生成 {out}")
    print(f"   {len(data)} 字节   格式 0   1 条轨   {len(events)} 个事件")
    print(f"   速度 {BPM} BPM，C 大调音阶 ×4")
    print(f"   解析回来：末拍 {_info['end_beat']:.1f} ≈ {real_secs:.1f} 秒"
          f"（手算 {duration_beats} 拍 ≈ {secs:.0f} 秒，以解析值为准）")
    print(f"   文件名 PROBE.MID（8.3 合规）")
    print()
    print("   ★ 特征：只用 MIDI 通道 1 + 钢琴音色。")
    print("     即使 PS5 的 TgMode 还停在 Pattern 模式（只收通道 1），")
    print("     这个文件也应该能响 —— 所以它能同时验证两件事。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
