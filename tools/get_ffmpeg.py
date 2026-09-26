#!/usr/bin/env python3
"""get_ffmpeg.py — 从多个源试下载 ffmpeg 静态构建，谁快用谁。"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

DEST = r"D:\音源\toolchain"
TMP = r"D:\音源\build\tmp"


def candidates() -> list[tuple[str, str]]:
    """返回 [(名字, url)]。"""
    out = []
    # GitHub 上的 BtbN 构建（有国内可达的 CDN）
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest",
            headers={"User-Agent": "Mozilla/5.0"})
        d = json.load(urllib.request.urlopen(req, timeout=60, context=CTX))
        for a in d.get("assets", []):
            n = a["name"]
            if ("win64" in n and n.endswith(".zip")
                    and "shared" not in n and "gpl" in n):
                out.append((n, a["browser_download_url"]))
    except Exception as e:
        print(f"  列 GitHub 资产失败: {type(e).__name__} {e}")

    out.append(("gyan-essentials",
                "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"))
    return out


def speed_test(url: str, seconds: float = 6.0, cap: int = 12 << 20) -> float:
    """下载几秒，返回 KB/s。0 表示失败。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        t0 = time.time()
        got = 0
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            while time.time() - t0 < seconds and got < cap:
                b = r.read(256 << 10)
                if not b:
                    break
                got += len(b)
        dt = max(0.001, time.time() - t0)
        return got / 1024 / dt
    except Exception as e:
        print(f"    测速失败: {type(e).__name__} {e}")
        return 0.0


def download(url: str, dst: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r, \
                open(dst, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if got - last >= (10 << 20):
                    last = got
                    pct = f"{got / 2**20:.0f}/{total / 2**20:.0f} MB" if total else f"{got / 2**20:.0f} MB"
                    sp = got / 1024 / max(0.001, time.time() - t0)
                    print(f"      {pct}  ({sp:.0f} KB/s)", flush=True)
        print(f"    完成 {os.path.getsize(dst) / 2**20:.1f} MB，"
              f"用时 {time.time() - t0:.0f}s")
        return True
    except Exception as e:
        print(f"    下载失败: {type(e).__name__} {e}")
        return False


def main() -> int:
    os.makedirs(DEST, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    cands = candidates()
    print(f"候选源 {len(cands)} 个，先测速（各 6 秒）：\n")

    scored = []
    for name, url in cands:
        print(f"  {name}")
        print(f"    {url[:100]}")
        kb = speed_test(url)
        print(f"    → {kb:.0f} KB/s")
        scored.append((kb, name, url))
    scored.sort(reverse=True)

    print()
    for kb, name, url in scored:
        if kb <= 0:
            continue
        print(f"用最快的源：{name}（{kb:.0f} KB/s）")
        zip_path = os.path.join(TMP, "ffmpeg_dl.zip")
        if not download(url, zip_path):
            continue
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = [n for n in z.namelist() if n.endswith("ffmpeg.exe")]
                if not names:
                    print("    压缩包里没有 ffmpeg.exe")
                    continue
                print(f"    解压 {names[0]} …")
                with z.open(names[0]) as src, \
                        open(os.path.join(DEST, "ffmpeg.exe"), "wb") as out:
                    out.write(src.read())
            # 顺带把 ffprobe 也取出来
            with zipfile.ZipFile(zip_path) as z:
                for n in z.namelist():
                    if n.endswith("ffprobe.exe"):
                        with z.open(n) as src, \
                                open(os.path.join(DEST, "ffprobe.exe"), "wb") as out:
                            out.write(src.read())
                        break
            sz = os.path.getsize(os.path.join(DEST, "ffmpeg.exe"))
            print(f"✅ 已就位 {DEST}\\ffmpeg.exe（{sz / 2**20:.1f} MB）")
            return 0
        except Exception as e:
            print(f"    解压失败: {type(e).__name__} {e}")
    print("❌ 所有源都失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
