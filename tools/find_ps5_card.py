#!/usr/bin/env python3
"""检查 MP4 顶层 box 结构 + 电脑上各磁盘的内容（找 PS5 的卡）。"""
from __future__ import annotations

import os
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

P = (r"C:\Users\15269\.dsh\attachments\v1\files\22"
     r"\22b6833fe6cb59056fb2b03355e9c5884516b38126cbc774ce06cb805ef4c74e"
     r"\VID20260926124507.mp4")


def mp4_boxes() -> None:
    f = open(P, "rb")
    f.seek(0, 2)
    total = f.tell()
    f.seek(0)
    print("=== MP4 顶层 box ===")
    i = 0
    while i + 8 <= total:
        f.seek(i)
        hdr = f.read(16)
        if len(hdr) < 8:
            break
        size = struct.unpack(">I", hdr[:4])[0]
        btype = hdr[4:8]
        hdrlen = 8
        if size == 1:
            size = struct.unpack(">Q", hdr[8:16])[0]
            hdrlen = 16
        print(f"  off={i:>13,}  size={size:>13,}  type={btype}")
        if size < hdrlen:
            print("  (size 异常，停止)")
            break
        i += size
    print(f"  文件总大小 {total:,}")
    print(f"  moov 实际位置 1,031,616,179  → mdat 真实长度 "
          f"{1031616179 - 24:,}")
    f.close()


def scan_drives() -> None:
    print("\n=== 电脑上的磁盘 ===")
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            items = os.listdir(root)
        except Exception as e:
            print(f"  {root}  打不开（{type(e).__name__}）")
            continue
        # 判断像不像 PS5 的卡：有 SMF / MP3 / SONGx 文件夹或 .PKT 文件
        markers = [x for x in items
                   if x.upper() in ("SMF", "MP3", "FXPATCH.PKT", "SYSINFO.PKT",
                                    "PATTERN.001")
                   or x.upper().startswith("SONG")]
        kind = ""
        if markers:
            kind = f"   ★★★ 像 PS5 的卡！标志：{markers}"
        elif not items:
            kind = "   （空盘）"
        print(f"  {root}  共 {len(items)} 项{kind}")
        if markers or len(items) <= 12:
            for x in sorted(items)[:14]:
                full = os.path.join(root, x)
                tag = "[目录]" if os.path.isdir(full) else ""
                print(f"        {x} {tag}")
            if len(items) > 14:
                print(f"        … 还有 {len(items) - 14} 项")


if __name__ == "__main__":
    mp4_boxes()
    scan_drives()
