#!/usr/bin/env python3
"""
rotate_video.py — 把竖屏（4K 横向编码 + 旋转标记）的视频真正转成横向 4K。

背景：手机竖着拍的 4K 视频，实际是 3840x2160 的帧 + displaymatrix 旋转标记。
播放器会照着标记把它竖过来显示。要"真正"变成横向，必须重编码像素，
不是删掉标记就行的。

用 NVENC 硬件编码（Quadro RTX 4000），4K60 转码几分钟就能完成。

用法：
    python tools/rotate_video.py --probe              # 只抽帧确认旋转方向
    python tools/rotate_video.py                      # 完整转码
    python tools/rotate_video.py --software           # 用 libx264 软件编码
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

SRC = (r"C:\Users\15269\.dsh\attachments\v1\files\22"
       r"\22b6833fe6cb59056fb2b03355e9c5884516b38126cbc774ce06cb805ef4c74e"
       r"\VID20260926124507.mp4")

# 本机可用的 ffmpeg（不需要另装）
# 9.0.2 完整版：抽帧 + 软件编码都能干，但它要求 NVENC API 13.1，
#             本机驱动只到 13.0，所以它做不了硬件编码
FFMPEG = r"D:\音源\toolchain\ffmpeg.exe"
FFPROBE = r"D:\音源\toolchain\ffprobe.exe"
# Valorant 录制器自带的 2023 年构建：NVENC 对接的是老 API，能用！
FFMPEG_OLD = (r"D:\WeGameApps\rail_apps\无畏契约(2001715)\ACLOS\Cross"
              r"\recorder-release\ffmpeg.exe")
FFMPEG_NVENC = FFMPEG_OLD          # 别名
# B站自带的 3.0.1（很老，留作最后备选）
FFMPEG_SW = r"C:\Users\15269\AppData\Roaming\bilibili\ffmpeg\ffmpeg.exe"

OUT_DIR = r"D:\音源\build\video"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def probe() -> None:
    """抽一帧出来，确认哪个 transpose 方向是对的。

    ★ 抽帧必须用带 PNG 编码器的全功能构建（bilibili 那个）；
      Valorant 录制器自带的那个是精简版，只有 NVENC 编码器，
      连 png 都写不出来（"Default encoder for format image2 is probably
      disabled"）。两个 ffmpeg 分工：这个抽帧，那个编码。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    for label, filt in (("cw", "transpose=1"), ("ccw", "transpose=2")):
        out = os.path.join(OUT_DIR, f"probe_{label}.png")
        r = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                 "-noautorotate", "-ss", "00:00:30", "-i", SRC,
                 "-frames:v", "1", "-vf", filt, out])
        ok = os.path.exists(out)
        print(f"  {label} ({filt}): {'OK' if ok else '失败'} {out}")
        if not ok and r.stderr:
            print("   ", r.stderr.strip()[:300])


def encode(software: bool, crf_or_cq: int, preset: str, out: str) -> int:
    src_kb = os.path.getsize(SRC) / 1024
    print(f"源：{SRC}")
    print(f"     {src_kb / 1024:.1f} MB")
    print(f"输出：{out}\n")

    if software:
        ff = FFMPEG
        venc = ["-c:v", "libx265", "-crf", str(crf_or_cq)]
        print(f"编码器：libx265  crf {crf_or_cq}  preset {preset}（软件，慢）")
    else:
        # 必须用 2023 年那个旧构建：ffmpeg 9.0.2 要求 NVENC API 13.1，
        # 而本机驱动（596.36）只提供 13.0，新版会直接拒绝打开编码器
        ff = FFMPEG_OLD
        venc = ["-c:v", "hevc_nvenc", "-rc", "constqp",
                "-qp", str(crf_or_cq)]
        print(f"编码器：hevc_nvenc  qp {crf_or_cq}  preset {preset}（硬件）")

    # ★ 不要加 -noautorotate！
    #   旧版 ffmpeg 不认这个选项，反而会因此把"不自动旋转"当成默认，
    #   结果 transpose=2 作用在已经旋转过的帧上，输出又变回竖屏。
    #   正确的做法就是老老实实写 transpose=2，让 ffmpeg 自己处理旋转标记。
    cmd = [ff, "-y", "-hide_banner", "-stats",
           "-i", SRC,
           "-vf", "transpose=2",
           *venc, "-preset", preset,
           "-pix_fmt", "yuv420p",
           "-c:a", "copy",            # 音频直接复制，不重编码
           "-movflags", "+faststart",
           out]

    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    last = 0.0
    for line in p.stdout:              # type: ignore[union-attr]
        line = line.rstrip()
        if "frame=" in line or "time=" in line:
            now = time.time()
            if now - last >= 3:
                last = now
                # ffmpeg 的进度是 \r 刷新的，这里直接打关键片段
                seg = line.strip()
                print(f"    {seg[:140]}", flush=True)
        elif "error" in line.lower() or "Error" in line:
            print(f"    ! {line[:200]}")
    p.wait()
    dt = time.time() - t0

    if p.returncode != 0:
        print(f"\n❌ 转码失败，退出码 {p.returncode}")
        return p.returncode
    sz = os.path.getsize(out)
    print(f"\n✅ 完成：{out}")
    print(f"   {sz / 2**20:.1f} MB（源 {src_kb / 1024:.1f} MB），"
          f"用时 {dt:.0f}s，速度 {147 / max(1, dt):.2f}x 实时")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="4K 竖屏 → 横向 真正旋转")
    ap.add_argument("--probe", action="store_true", help="只抽帧确认方向")
    ap.add_argument("--software", action="store_true", help="用 libx264 软件编码")
    ap.add_argument("--quality", type=int, default=None,
                    help="NVENC 用 cq / 软件用 crf，默认 19")
    ap.add_argument("--preset", default=None, help="编码 preset")
    ap.add_argument("-o", "--out", default=None, help="输出路径")
    a = ap.parse_args()

    if not os.path.exists(SRC):
        raise SystemExit(f"源文件不存在：{SRC}")

    if a.probe:
        probe()
        return 0

    q = a.quality if a.quality is not None else 19
    preset = a.preset or ("fast" if a.software else "p5")
    out = a.out or os.path.join(
        OUT_DIR, "VID20260926124507_landscape_4k.mp4")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return encode(a.software, q, preset, out)


if __name__ == "__main__":
    sys.exit(main())
