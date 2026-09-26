#!/usr/bin/env python3
"""
mp4info.py — 纯标准库解析 MP4，读出分辨率、旋转、编码、时长。

为什么自己写：这台机器上没有 ffprobe，而判断"怎么转视频"必须先把
真实的宽高和旋转矩阵读出来 —— 手机拍的竖屏视频经常是
"分辨率 1920x1080 + 旋转矩阵 90°"，光看分辨率会判断错方向。

用法：
    python tools/mp4info.py "路径.mp4"
"""
from __future__ import annotations

import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}


def boxes(data: bytes, start: int = 0, end: int | None = None):
    """遍历 MP4 box。产出 (type, payload_start, payload_end, box_start)。

    MP4 box 结构：[4 字节 size][4 字节 type][payload]
    ★ payload 从 box_start + 8 开始 —— 少了这个 +8 就会把 size/type
      自己当成 payload 去解，整个遍历一个 box 都出不来。
    """
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i + 4])[0]
        btype = data[i + 4:i + 8]
        hdr = 8
        if size == 1:                      # 64 位 size
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:                    # 直到文件末尾
            size = end - i
        if size < hdr or i + size > end:
            return
        yield btype, i + hdr, i + size, i
        i += size


def find(data: bytes, path: list[bytes], start=0, end=None):
    """按路径找 box，例如 [b'moov', b'trak', b'tkhd']。"""
    end = len(data) if end is None else end
    cur = [(start, end)]
    for want in path:
        nxt = []
        for s, e in cur:
            for btype, ps, pe, _bs in boxes(data, s, e):
                if btype == want:
                    nxt.append((ps, pe))
                elif btype in CONTAINERS and want in CONTAINERS:
                    # 允许跳过中间容器层级
                    nxt.extend(_search_nested(data, ps, pe, want))
        cur = nxt
        if not cur:
            return None
    return cur[0] if cur else None


def _search_nested(data, s, e, want, depth=0):
    out = []
    if depth > 4:
        return out
    for btype, ps, pe, _bs in boxes(data, s, e):
        if btype == want:
            out.append((ps, pe))
        elif btype in CONTAINERS:
            out.extend(_search_nested(data, ps, pe, want, depth + 1))
    return out


def matrix_rotation(m: list[int]) -> int:
    """把 tkhd 的 3x3 矩阵换算成旋转角度（0/90/180/270）。"""
    # 矩阵以 16.16 定点存：a b u / c d v / x y w
    a, b, _u, c, d, _v, _x, _y, _w = m
    def near(v, t):
        return abs(v - t) < 0.01
    if near(a, 1) and near(b, 0) and near(c, 0) and near(d, 1):
        return 0
    if near(a, 0) and near(b, 1) and near(c, -1) and near(d, 0):
        return 90          # 顺时针 90（播放器要转 90 才正）
    if near(a, -1) and near(b, 0) and near(c, 0) and near(d, -1):
        return 180
    if near(a, 0) and near(b, -1) and near(c, 1) and near(d, 0):
        return 270
    return -1


def parse(path: str) -> dict:
    # moov 可能在文件末尾（边录边写的手机视频就是这样）。
    # ★ 注意：tail.find(b'moov') 找到的是**类型字段**的位置，
    #   box 真正的起点还要往前 4 字节（size 字段）。少了这个 -4
    #   就会把 size 当成 type 去解，全部错位。
    with open(path, "rb") as f:
        f.seek(0, 2)
        total = f.tell()
        tail_len = min(total, 4 * 1024 * 1024)
        f.seek(total - tail_len)
        tail = f.read(tail_len)
        f.seek(0)
        head = f.read(min(total, 2 * 1024 * 1024))

    info: dict = {"size": total}

    moov_off = None
    blob, base = tail, total - len(tail)

    # 先扫尾部（moov 常在末尾）
    # tail.find(b'moov') 命中的是 4 字节 type 字段本身，
    # 它的 payload 从 idx+4 开始，box 起点在 idx-4（size 字段）。
    idx = tail.find(b"moov")
    if idx >= 4:
        size = struct.unpack(">I", tail[idx - 4:idx])[0]
        if (base + idx - 4) + size == total:     # size 正好补到文件末尾
            moov_off = (idx + 4, len(tail))
            info["moov_at"] = "文件末尾"

    if moov_off is None:
        for btype, ps, pe, bs in boxes(head, 0, len(head)):
            if btype == b"moov":
                moov_off = (ps, pe)
                blob, base = head, 0
                info["moov_at"] = "文件开头"
                break
    if moov_off is None:
        # 兜底：全窗口按 box 结构遍历
        for btype, ps, pe, bs in boxes(blob, 0, len(blob)):
            if btype == b"moov":
                moov_off = (ps, pe)
                info["moov_at"] = "扫描找到"
                break
    if moov_off is None:
        raise SystemExit("找不到 moov box（文件可能不完整或损坏）")

    ms, me = moov_off
    moov = blob[ms:me]
    info["moov_size"] = len(moov)

    # mvhd：时长/时间刻度
    r = find(moov, [b"mvhd"])
    if r:
        s, e = r
        ver = moov[s]
        if ver == 0:
            ts, dur = struct.unpack(">II", moov[s + 12:s + 20])
        else:
            ts, dur = struct.unpack(">IQ", moov[s + 20:s + 28])
        info["timescale"] = ts
        info["duration"] = dur
        info["seconds"] = dur / ts if ts else 0

    # 每个 trak
    tracks = []
    for btype, ps, pe, _bs in boxes(moov, 0, len(moov)):
        if btype != b"trak":
            continue
        trak = moov[ps:pe]
        t: dict = {}
        r = find(trak, [b"tkhd"])
        if r:
            s, _e = r
            ver = trak[s]
            off = s + (4 if ver == 0 else 4)
            # tkhd: ver/flags(4) ctime(4|8) mtime(4|8) trackid(4) reserved(4) dur(4|8) ...
            p = s + 4
            p += 8 if ver == 0 else 16          # creation/modification
            t["track_id"] = struct.unpack(">I", trak[p:p + 4])[0]; p += 4
            p += 4                              # reserved
            p += 4 if ver == 0 else 8           # duration
            p += 8                              # reserved
            p += 2 + 2 + 2 + 2                  # layer, altgroup, volume, reserved
            m = struct.unpack(">9i", trak[p:p + 36]); p += 36
            t["rotation_units"] = m
            t["rotation"] = matrix_rotation([v / 65536.0 for v in m])
            t["width"] = struct.unpack(">I", trak[p:p + 4])[0] / 65536.0
            t["height"] = struct.unpack(">I", trak[p + 4:p + 8])[0] / 65536.0
        # 编码
        for tag in (b"avc1", b"hvc1", b"hev1", b"mp4v", b"av01"):
            rr = find(trak, [tag])
            if rr:
                s, _e = rr
                t["codec"] = tag.decode()
                t["coded_w"] = struct.unpack(">H", trak[s + 24:s + 26])[0]
                t["coded_h"] = struct.unpack(">H", trak[s + 26:s + 28])[0]
                break
        for tag in (b"mp4a", b"ac-3", b"ec-3", b"Opus"):
            rr = find(trak, [tag])
            if rr:
                t["audio"] = tag.decode()
                break
        if t:
            tracks.append(t)
    info["tracks"] = tracks

    # 音频时长（mdhd）
    info["audio_seconds"] = None
    for btype, ps, pe, _bs in boxes(moov, 0, len(moov)):
        if btype != b"trak":
            continue
        trak = moov[ps:pe]
        r = find(trak, [b"mdhd"])
        if r:
            s, _e = r
            ver = trak[s]
            if ver == 0:
                ts, dur = struct.unpack(">II", trak[s + 12:s + 20])
            else:
                ts, dur = struct.unpack(">IQ", trak[s + 20:s + 28])
            if ts:
                info["audio_seconds"] = dur / ts
    return info


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("用法: python tools/mp4info.py <视频文件>")
    info = parse(sys.argv[1])
    print(f"文件：{sys.argv[1]}")
    print(f"大小：{info['size'] / 1048576:.1f} MB   moov {info['moov_size']} 字节")
    if "seconds" in info:
        m, s = divmod(info["seconds"], 60)
        print(f"时长：{int(m)} 分 {s:.1f} 秒（{info['seconds']:.2f}s, "
              f"timescale {info['timescale']}）")
    for i, t in enumerate(info["tracks"]):
        print(f"\n轨 {i}  (track_id {t.get('track_id')})")
        if "codec" in t:
            print(f"  视频编码：{t['codec']}  "
                  f"编码尺寸 {t.get('coded_w')}x{t.get('coded_h')}")
        if "width" in t:
            print(f"  显示尺寸：{t['width']:.0f} x {t['height']:.0f}")
            print(f"  旋转矩阵：{t['rotation']}°  "
                  f"({t['rotation_units']})")
            w, h = t["width"], t["height"]
            rot = t["rotation"]
            dw, dh = (h, w) if rot in (90, 270) else (w, h)
            print(f"  → 实际播放方向：{dw:.0f} x {dh:.0f}  "
                  f"({'竖屏 Portrait' if dh > dw else '横屏 Landscape'})")
        if "audio" in t:
            print(f"  音频编码：{t['audio']}  "
                  f"时长 {info['audio_seconds']:.2f}s"
                  if info.get("audio_seconds") else f"  音频编码：{t['audio']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
