#!/usr/bin/env python3
"""
nvenc_tune.py — 在 10 秒片段上试几组 NVENC 参数，找出能跑且速度合适的。

为什么需要：ffmpeg 9.x 里 NVENC 的参数校验变严了，
"-rc vbr -cq 19 -b:v 0" 这种老写法会直接 "Error while opening encoder"。
与其猜，不如拿 10 秒片段把几种组合都跑一遍。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

FFMPEG = r"D:\音源\toolchain\ffmpeg.exe"
# 2023 年的旧构建：编译时对接的 NVENC 版本较老，可能匹配用户当前的驱动
FFMPEG_OLD = (r"D:\WeGameApps\rail_apps\无畏契约(2001715)\ACLOS\Cross"
              r"\recorder-release\ffmpeg.exe")
FFMPEG_SW = r"C:\Users\15269\AppData\Roaming\bilibili\ffmpeg\ffmpeg.exe"
SRC = (r"C:\Users\15269\.dsh\attachments\v1\files\22"
       r"\22b6833fe6cb59056fb2b03355e9c5884516b38126cbc774ce06cb805ef4c74e"
       r"\VID20260926124507.mp4")
OUT_DIR = r"D:\音源\build\video"

# (显示名, ffmpeg 路径, 视频编码参数, preset)
TRIALS = [
    ("旧版NVENC constqp19", FFMPEG_OLD,
     ["-c:v", "hevc_nvenc", "-rc", "constqp", "-qp", "19"], "p5"),
    ("旧版NVENC 默认cq", FFMPEG_OLD, ["-c:v", "hevc_nvenc", "-cq", "19"], "p5"),
    ("软件 x265 crf20 medium", FFMPEG, ["-c:v", "libx265", "-crf", "20"], "medium"),
    ("软件 x265 crf20 fast", FFMPEG, ["-c:v", "libx265", "-crf", "20"], "fast"),
    ("软件 x264 crf18 fast", FFMPEG, ["-c:v", "libx264", "-crf", "18"], "fast"),
]


def run_trial(name: str, ff: str, venc: list[str], preset: str) -> None:
    out = os.path.join(OUT_DIR, "trial.mp4")
    if os.path.exists(out):
        os.remove(out)
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-noautorotate", "-ss", "00:00:20", "-t", "10", "-i", SRC,
           "-vf", "transpose=2",
           *venc, "-preset", preset,
           "-pix_fmt", "yuv420p", "-an", out]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dt = max(0.001, time.time() - t0)
    if r.returncode != 0 or not os.path.exists(out):
        err = (r.stderr or "").strip().splitlines()
        print(f"  ❌ {name}")
        for l in err[:3]:
            print(f"       {l[:170]}")
        return
    sz = os.path.getsize(out)
    p = subprocess.run([r"D:\音源\toolchain\ffprobe.exe", "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,avg_frame_rate",
                        "-of", "csv=p=0", out],
                       capture_output=True, text=True)
    print(f"  ✅ {name:26} {sz / 2**20:6.1f} MB/10s  {dt:6.1f}s  "
          f"{10 / dt:4.2f}x 实时  [{p.stdout.strip()}]")
    os.remove(out)


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"源：{os.path.basename(SRC)}")
    print(f"用 10 秒片段（00:00:20 起）试 {len(TRIALS)} 组，"
          f"整片 147 秒的预计用时 = 147 / 实时倍数\n")
    for name, ff, venc, preset in TRIALS:
        run_trial(name, ff, venc, preset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
