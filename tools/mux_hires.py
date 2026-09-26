#!/usr/bin/env python3
"""
mux_hires.py — 把「Pr 导出的纯视频」+「Pr 导出的 24bit WAV」无损封装成 B站 Hi-Res 投稿文件。

为什么需要这一步：Pr 的 H.264/H.265（MP4）导出器音频只有 AAC，采样率最高 48kHz，
而 B站 Hi-Res 的判定要求音轨必须是**无损编码**（flac / pcm / alac / ape / wavpack …）
且采样率 ≥48kHz、位深 ≥24bit —— AAC 不在白名单里。
所以只能在 Pr 之外用 ffmpeg 把 WAV 无损封进容器，视频流直接 copy，不重编码。

B站官方判定（2022 年上线 Hi-Res 无损音质时公布）：
  1. 音频编码 ∈ {alac, ape, dts, flac, mp4als, ralf, shorten, mlp, tak, wavpack,
     wmalossless, pcm}，AAC 不算
  2. 采样率 ≥48kHz（≥96kHz 平台输出 96kHz 版本，否则输出 48kHz 版本）
  3. 位深 ≥24bit

用法：
    python tools/mux_hires.py --probe <video> <wav>    # 只看参数，不封装
    python tools/mux_hires.py <video> <wav>            # → build/video/<name>_hires.mp4
    python tools/mux_hires.py <video> <wav> --mkv      # MKV + FLAC（备选容器）
    python tools/mux_hires.py <video> <wav> --pcm      # MOV + PCM（不压缩，体积约 9 倍）
    python tools/mux_hires.py <video> <wav> --no-verify
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# 本机可用的 ffmpeg（与 rotate_video.py / nvenc_tune.py 用同一套）
# 9.0.2 完整版：FLAC 封装进 MP4 不再需要 -strict -2（4.x 老版本才需要）
FFMPEG = r"D:\音源\toolchain\ffmpeg.exe"
FFPROBE = r"D:\音源\toolchain\ffprobe.exe"

OUT_DIR = r"D:\音源\build\video"

# B站认可的无损音频编码（ffprobe 的 codec_name）
LOSSLESS = {"alac", "ape", "dts", "flac", "mp4als", "ralf", "shorten", "mlp",
            "tak", "wavpack", "wmalossless", "pcm_s16le", "pcm_s24le",
            "pcm_s32le", "pcm_s16be", "pcm_s24be", "pcm_f32le", "pcm_f64le"}


def run(cmd: list[str], quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def probe(path: str) -> dict:
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", path], quiet=True)
    if r.returncode != 0:
        raise SystemExit(f"ffprobe 失败：{path}\n{r.stderr.strip()[:400]}")
    return json.loads(r.stdout or "{}")


def fmt_dur(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def describe(path: str, info: dict) -> dict:
    """打印一个文件的流信息，返回 {'v': 视频流, 'a': 音频流}（可能为 None）。"""
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    size = int(info.get("format", {}).get("size", 0) or 0)
    print(f"\n■ {os.path.basename(path)}   {size / 2**20:.1f} MB")
    if v:
        print(f"    视频: {v.get('codec_name','?')} "
              f"{v.get('width')}x{v.get('height')} "
              f"{eval_rate(v.get('avg_frame_rate') or v.get('r_frame_rate'))} "
              f"{v.get('pix_fmt','?')}  "
              f"{int(v.get('bit_rate') or 0) / 1000:.0f} kbps  "
              f"{fmt_dur(float(v.get('duration') or 0))}")
    if a:
        bits = a.get("bits_per_raw_sample") or a.get("bits_per_sample") or "?"
        print(f"    音频: {a.get('codec_name','?')} "
              f"{a.get('sample_rate')} Hz  {bits}bit  "
              f"{a.get('channels')}ch  "
              f"{int(a.get('bit_rate') or 0) / 1000:.0f} kbps  "
              f"{fmt_dur(float(a.get('duration') or 0))}")
    return {"v": v, "a": a}


def eval_rate(rate: str | None) -> str:
    if not rate or rate == "0/0":
        return "?"
    try:
        num, den = rate.split("/")
        return f"{float(num) / float(den):g}fps"
    except Exception:
        return rate


def check_audio(a: dict | None) -> tuple[bool, list[str]]:
    """按 B站 Hi-Res 三条判定音频流，返回 (是否达标, 说明行)。"""
    notes: list[str] = []
    if not a:
        return False, ["❌ 没有音频流"]
    codec = a.get("codec_name", "")
    rate = int(a.get("sample_rate") or 0)
    bits = int(a.get("bits_per_raw_sample") or a.get("bits_per_sample") or 0)

    ok_codec = codec in LOSSLESS or codec.startswith("pcm_")
    ok_rate = rate >= 48000
    ok_bits = bits >= 24

    notes.append(f"  {'✅' if ok_codec else '❌'} 编码 {codec}"
                 f"{'（无损白名单内）' if ok_codec else '（AAC/有损，不在白名单！）'}")
    notes.append(f"  {'✅' if ok_rate else '❌'} 采样率 {rate} Hz"
                 f"{'→ 平台输出 96kHz 版' if rate >= 96000 else '→ 平台输出 48kHz 版' if ok_rate else '（必须 ≥48000）'}")
    notes.append(f"  {'✅' if ok_bits else '❌'} 位深 {bits}bit"
                 f"{'（≥24bit 达标）' if ok_bits else '（必须 ≥24bit）'}")
    return (ok_codec and ok_rate and ok_bits), notes


def mux(video: str, audio: str, out: str, mode: str) -> int:
    if mode == "pcm":
        # MOV + 未压缩 PCM：也在白名单里，但 96k/24bit 立体声 = 4.6Mbps ≈ 2GB/小时
        acodec = ["-c:a", "pcm_s24le"]
        extra: list[str] = []
    else:
        # FLAC：无损且体积只有 PCM 的 1/9 左右
        # -sample_fmt s32 是 FLAC 编码器的要求（FLAC 内部用 s32 承载 24bit），
        # -bits_per_raw_sample 24 才是真正写进文件的位深
        acodec = ["-c:a", "flac", "-sample_fmt", "s32",
                  "-bits_per_raw_sample", "24"]
        extra = [] if mode == "mkv" else ["-movflags", "+faststart"]

    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "warning",
           "-i", video, "-i", audio,
           # ★ 必须写死 -map：保证成品里只有 1 条视频 + 1 条音频，
           #   B站只取第一条音频流，多一条就可能取错
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy",              # 视频不重编码，Pr 导出的画质原样保留
           *acodec,
           "-ar", str(probe(audio).get("streams", [{}])[0].get("sample_rate", 48000)),
           "-ac", "2",
           *extra, out]
    t0 = time.time()
    r = run(cmd)
    if r.stderr.strip():
        print("  " + r.stderr.strip()[:600])
    print(f"  ffmpeg exit={r.returncode}  用时 {time.time() - t0:.1f}s")
    return r.returncode


def md5_of_audio(path: str) -> str:
    """把音轨解码成原始 PCM 求 MD5，用来证明封装过程逐位无损。"""
    r = run([FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0",
             "-f", "md5", "-"], quiet=True)
    return (r.stdout or "").strip().split("=")[-1]


def faststart_ok(path: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(2 * 2**20)
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    return moov >= 0 and mdat >= 0 and moov < mdat


def loudness(path: str) -> tuple[str, str]:
    """EBU R128：返回 (integrated LUFS, true peak dBFS) 的文字形式。"""
    r = run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
             "-af", "ebur128=peak=true", "-f", "null", "-"], quiet=True)
    lufs = peak = "?"
    for line in (r.stderr or "").splitlines():
        s = line.strip()
        if s.startswith("I:"):
            lufs = s.split()[1]
        elif s.startswith("Peak:"):
            peak = s.split()[1]
    return lufs, peak


def verify(video: str, audio: str, out: str) -> bool:
    print("\n=== 校验 ===")
    oi = probe(out)
    st = describe(out, oi)
    ok, notes = check_audio(st["a"])
    for n in notes:
        print(n)

    print("\n  无损比对（把成品音轨解码回 PCM 求 MD5，与源 WAV 对比）：")
    m_src, m_out = md5_of_audio(audio), md5_of_audio(out)
    same = m_src == m_out and m_src != ""
    print(f"    源   {m_src}\n    成品 {m_out}\n    {'✅ 逐位一致，真无损' if same else '❌ 不一致！'}")

    nstreams = len(oi.get("streams", []))
    print(f"\n  流数量 {nstreams}（应为 2：1 视频 + 1 音频）"
          f"{'✅' if nstreams == 2 else '❌'}")

    if out.lower().endswith(".mp4"):
        fs = faststart_ok(out)
        print(f"  faststart {'✅ moov 在前，可边下边播' if fs else '❌ moov 在后'}")
    else:
        fs = True

    dv = float(st["v"].get("duration") or 0) if st["v"] else 0
    da = float(st["a"].get("duration") or 0) if st["a"] else 0
    sync = abs(dv - da) <= 0.1
    print(f"  音视频时长 {fmt_dur(dv)} vs {fmt_dur(da)} {'✅' if sync else '❌ 差超过 0.1s'}")

    lufs, peak = loudness(out)
    print(f"  响度 {lufs} LUFS   真峰值 {peak} dBFS"
          f"   （母版惯例：约 -14 LUFS、真峰值 ≤ -1 dBTP）")

    print(f"\n  成品：{out}   {os.path.getsize(out) / 2**20:.1f} MB")
    all_ok = ok and same and nstreams == 2 and fs and sync
    print("  " + ("✅ 可以上传（B站 Hi-Res 三条判定均满足）" if all_ok else "❌ 有问题，别传"))
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pr 纯视频 + 24bit WAV → B站 Hi-Res 投稿文件（无损封装）")
    ap.add_argument("video", help="Pr 导出的纯视频（H.264/HEVC MP4）")
    ap.add_argument("audio", help="Pr 导出的 WAV（24bit / 48k 或 96k）")
    ap.add_argument("-o", "--out", default=None, help="输出路径")
    ap.add_argument("--mkv", action="store_true", help="用 MKV 容器（备选）")
    ap.add_argument("--pcm", action="store_true", help="用 MOV + PCM（不压缩）")
    ap.add_argument("--probe", action="store_true", help="只看参数，不封装")
    ap.add_argument("--no-verify", action="store_true", help="跳过封装后校验")
    a = ap.parse_args()

    for p in (a.video, a.audio):
        if not os.path.exists(p):
            raise SystemExit(f"文件不存在：{p}")

    print("=== 源文件 ===")
    describe(a.video, probe(a.video))
    audio_info = probe(a.audio)
    describe(a.audio, audio_info)

    a_stream = next((s for s in audio_info.get("streams", [])
                     if s.get("codec_type") == "audio"), None)
    eligible, notes = check_audio(a_stream)
    print("\n=== B站 Hi-Res 判定（源音频）===")
    for n in notes:
        print(n)
    if not eligible:
        print("\n❌ 源音频不达标，先回 Pr 重导 WAV（波形音频 / 24bit / ≥48kHz）")
        return 1
    if a.probe:
        return 0

    ext = ".mov" if a.pcm else ".mkv" if a.mkv else ".mp4"
    out = a.out or os.path.join(
        OUT_DIR, os.path.splitext(os.path.basename(a.video))[0] + "_hires" + ext)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    print(f"\n=== 封装（{'MOV+PCM' if a.pcm else 'MKV+FLAC' if a.mkv else 'MP4+FLAC'}）→ {out} ===")
    if mux(a.video, a.audio, out,
           "pcm" if a.pcm else "mkv" if a.mkv else "mp4") != 0:
        return 1

    if a.no_verify:
        return 0
    return 0 if verify(a.video, a.audio, out) else 1


if __name__ == "__main__":
    sys.exit(main())
