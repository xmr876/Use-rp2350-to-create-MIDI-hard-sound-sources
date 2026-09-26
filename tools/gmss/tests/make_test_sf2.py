"""
tests/make_test_sf2.py — 手工构造一个最小但合法的 .sf2 文件

没有真实音色库时用它验证解析器与转换链。
结构（严格遵循 SF2 2.04 规范）：

  1 个 preset:  bank=0 program=0 "TestPiano"   → 2 个 zone（力度分层 0-63 / 64-127）
       zone 1 → instrument 0, keyRange 0-127, velRange 0-63
       zone 2 → instrument 1, keyRange 0-127, velRange 64-127
  各 instrument: 1 个 global zone + 1 个 local zone（含 sampleID）
  2 个 sample:  都是 44100Hz、root key 60、带循环

生成的正弦波带淡入淡出，并做整数周期对齐，便于检查循环点。
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np

# 复用 sf2defs 里的生成器编号，避免手抄错
# 目录结构：tools/gmss/{gmss/,tests/}，包在 tests 的上一级里
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gmss import sf2defs as D


class Chunk:
    """收集 pdta 记录，自动生成记录块"""

    def __init__(self):
        self.records: list[bytes] = []

    def add(self, fmt: str, *args) -> int:
        self.records.append(struct.pack(fmt, *args))
        return len(self.records) - 1

    def blob(self) -> bytes:
        return b"".join(self.records)


def cstr20(s: str) -> bytes:
    b = s.encode("latin-1")[:19]
    return b + b"\x00" * (20 - len(b))


def make_sample(freq: float, sr: int, dur_s: float,
                fade_in_ms: float = 5.0, fade_out_ms: float = 40.0,
                loop_cycles: int = 8, amp: float = 0.8) -> tuple[np.ndarray, int, int]:
    """生成一个带循环的正弦采样。

    返回 (int16 数组, loop_start, loop_end)。

    两条硬性要求（否则循环点必然咔哒）：
      1. 循环段长度必须是**整数个周期** → 用 round(period) 取整，而不是 round(period×N)
      2. 淡出**必须全部落在循环段之后** → 否则循环段内部音量在变，接不回起点
    """
    n = int(sr * dur_s)
    p = sr / freq
    loop_len = max(1, int(round(p)) * loop_cycles)     # 整数个周期

    fo = int(sr * fade_out_ms / 1000.0)
    fi = int(sr * fade_in_ms / 1000.0)
    tail = fo + int(sr * 0.001)                        # 尾部余量 1ms

    loop_end = n - tail
    loop_start = loop_end - loop_len
    if loop_start <= fi:
        raise ValueError(
            f"采样太短：需要开头 {fi} 点 + 循环 {loop_len} 点 + 尾部 {tail} 点 "
            f"= {fi + loop_len + tail}，但只有 {n} 点"
        )

    t = np.arange(n, dtype=np.float64) / sr
    x = np.sin(2.0 * math.pi * freq * t)

    # 淡出只作用于 [loop_end, n)，绝不碰循环段
    if fo > 0:
        x[loop_end:loop_end + fo] *= np.linspace(1.0, 0.0, fo)
    x[loop_end + fo:] = 0.0
    # 开头淡入
    if fi > 0:
        x[:fi] *= np.linspace(0.0, 1.0, fi)

    x = np.clip(x * amp, -1.0, 1.0)
    return (x * 32767.0).astype("<i2"), loop_start, loop_end


def gen_record(oper: int, amount_i16: int, subrange: tuple[int, int] | None = None) -> bytes:
    """构造一条 pgen/igen。

    subrange 用于 keyRange(43)/velRange(44)：按 SF2 规范 p.19 的 rangesType
    打包为 `(hi << 8) | lo`（低字节 = 低值，高字节 = 高值）。
    """
    if subrange is not None:
        lo, hi = subrange
        amt = ((hi & 0xFF) << 8) | (lo & 0xFF)
    else:
        amt = amount_i16 & 0xFFFF
    return struct.pack("<HH", oper, amt)


def build_sf2(out_path: str, sr: int = 44100) -> dict:
    """生成测试 sf2，返回一些自检用的元信息"""
    # ---------------- 采样 ----------------
    # 用 262.5Hz：44100/262.5 = 168.0 恰好整数，循环段就是精确的整数个周期，
    # 便于测试断言（真实音色库当然不会这么巧，容差要宽一些）。
    TEST_FREQ = 262.5
    s1, ls1, le1 = make_sample(TEST_FREQ, sr, 1.2, loop_cycles=8, amp=0.5)
    # 强力度层：**内容必须与弱层不同**，否则 layout 的内容去重会把两层合成一份，
    # 测试就覆盖不到"同一乐器多采样"的路径。
    s2, ls2, le2 = make_sample(TEST_FREQ * 2, sr, 1.2, loop_cycles=16, amp=0.9)

    smpl = np.concatenate([s1, s2])
    n1 = len(s1)
    off1, off2 = 0, n1
    end1, end2 = n1, n1 + len(s2)

    # ---------------- shdr ----------------
    # 记录 46 字节，无保留字段: name[20] start/end/loopStart/loopEnd/rate:u32×5
    #                          originalKey:u8 correction:i8 link:u16 type:u16
    SHDR_FMT = "<20sIIIIIBbHH"
    assert struct.calcsize(SHDR_FMT) == D.SZ_SHDR, \
        f"shdr 格式长度 {struct.calcsize(SHDR_FMT)} != {D.SZ_SHDR}"

    shdr = Chunk()
    shdr.add(SHDR_FMT, cstr20("TestSine_soft"), off1, end1,
             ls1, le1, sr, 60, 0, 0, 1)
    shdr.add(SHDR_FMT, cstr20("TestSine_hard"), off2, end2,
             off2 + ls2, off2 + le2, sr, 60, 0, 0, 1)
    shdr.add(SHDR_FMT, cstr20("EOS"), 0, 0, 0, 0, 0, 0, 0, 0, 0)

    # ---------------- igen / ibag / inst ----------------
    # 索引必须精确记账：每个 instrument 有自己的全局区 + N 个 local zone，
    # bagNdx 指向**该 instrument 的全局区**，下一个 instrument 的 bagNdx 就是它的终点。
    igen = Chunk()
    ibag = Chunk()
    inst = Chunk()

    def add_local_soft():
        ibag.add("<HH", 2, 0)                                  # local zone 起点
        igen.records.append(gen_record(D.GEN["sampleID"], 0))
        igen.records.append(gen_record(D.GEN["sampleModes"], 1))     # 1 = 连续循环
        igen.records.append(gen_record(D.GEN["initialAttenuation"], 60))   # -3dB
        igen.records.append(gen_record(D.GEN["attackVolEnv"], -6000))      # ~31ms
        igen.records.append(gen_record(D.GEN["decayVolEnv"], -3000))
        igen.records.append(gen_record(D.GEN["sustainVolEnv"], 100))       # -1dB
        igen.records.append(gen_record(D.GEN["releaseVolEnv"], -2000))
        # 终止 bag 的 genNdx 必须指向**终端生成器**的位置（= 目前 igen 条数）
        ibag.add("<HH", len(igen.records), 0)

    def add_local_hard():
        ibag.add("<HH", 9, 0)
        igen.records.append(gen_record(D.GEN["sampleID"], 1))
        igen.records.append(gen_record(D.GEN["sampleModes"], 1))
        igen.records.append(gen_record(D.GEN["attackVolEnv"], -12000))     # 1ms
        igen.records.append(gen_record(D.GEN["decayVolEnv"], -3000))
        igen.records.append(gen_record(D.GEN["sustainVolEnv"], 0))
        igen.records.append(gen_record(D.GEN["releaseVolEnv"], -2000))
        ibag.add("<HH", len(igen.records), 0)

    # --- instrument 0: 弱力度层 ---
    ibag.add("<HH", 0, 0)          # 全局区 [0,2)
    igen.records.append(gen_record(D.GEN["initialFilterFc"], 13500))
    igen.records.append(gen_record(D.GEN["pan"], 0))
    add_local_soft()               # local [2,9)
    inst.add("<20sH", cstr20("InstSoft"), 0)

    # --- instrument 1: 强力度层（全局区从 bag 索引 2 开始）---
    ibag.add("<HH", 9, 0)          # 全局区 [9,10)
    igen.records.append(gen_record(D.GEN["initialFilterFc"], 13500))
    add_local_hard()               # local [10,16)
    inst.add("<20sH", cstr20("InstHard"), 2)
    inst.add("<20sH", cstr20("EOI"), 0)

    # 自检：ibag 的 genNdx 必须单调不减，这是最容易写错的地方
    _b = [struct.unpack_from("<H", ibag.blob(), i * 4)[0]
          for i in range(len(ibag.blob()) // 4)]
    assert _b == sorted(_b), f"ibag 的 genNdx 不是单调不减的: {_b}"
    assert _b[-1] == 16, f"ibag 终端 bag 的 genNdx 应为 16（终端生成器位置），实际 {_b[-1]}"
    assert len(igen.blob()) // 4 == 16, \
        f"igen 条数应为 16（弱层 2+7，强层 1+6），实际 {len(igen.blob()) // 4}"

    # ---------------- pgen / pbag / phdr ----------------
    # 顺序很关键：preset 的第一个 zone 必须是**空**全局区，后面才是真正的 zone。
    # 若把 keyRange 塞进全局区，它就不再被识别为 global（SF2 语义如此）。
    pgen = Chunk()
    pbag = Chunk()
    phdr = Chunk()

    pbag.add("<HH", 0, 0)                                  # 全局区（无生成器）
    pbag.add("<HH", 0, 0)                                  # zone1 起点
    pgen.records.append(gen_record(D.GEN["keyRange"], 0, subrange=(0, 127)))
    pgen.records.append(gen_record(D.GEN["velRange"], 0, subrange=(0, 63)))
    pgen.records.append(gen_record(D.GEN["instrument"], 0))
    pbag.add("<HH", 3, 0)                                  # zone2 起点
    pgen.records.append(gen_record(D.GEN["keyRange"], 0, subrange=(0, 127)))
    pgen.records.append(gen_record(D.GEN["velRange"], 0, subrange=(64, 127)))
    pgen.records.append(gen_record(D.GEN["instrument"], 1))
    pbag.add("<HH", 6, 0)                                  # 终止 bag → 指向终端生成器
    phdr.add("<20sHHHiII", cstr20("TestPiano"), 0, 0, 0, 0, 0, 0)
    phdr.add("<20sHHHiII", cstr20("EOP"), 0, 0, 0, 0, 0, 0)

    # ---------------- 组装 ----------------
    def chunk(cid: bytes, body: bytes) -> bytes:
        pad = b"\x00" if (len(body) & 1) else b""
        return cid + struct.pack("<I", len(body)) + body + pad

    def list_chunk(ltype: bytes, body: bytes) -> bytes:
        inner = ltype + body
        pad = b"\x00" if (len(inner) & 1) else b""
        return b"LIST" + struct.pack("<I", len(inner)) + inner + pad

    info_body = (chunk(b"ifil", struct.pack("<HH", 2, 4)) +
                 chunk(b"isng", b"EMU8000\x00") +
                 chunk(b"INAM", b"GMSS Test Font\x00"))

    smpl_body = chunk(b"smpl", smpl.astype("<i2").tobytes())
    pdta_body = b"".join(
        chunk(name, c.blob()) for name, c in [
            (b"phdr", phdr), (b"pbag", pbag), (b"pmod", Chunk()),
            (b"pgen", pgen), (b"inst", inst), (b"ibag", ibag),
            (b"imod", Chunk()), (b"igen", igen), (b"shdr", shdr),
        ]
    )

    sfbk = list_chunk(b"INFO", info_body) + list_chunk(b"sdta", smpl_body) + \
        list_chunk(b"pdta", pdta_body)
    out = b"RIFF" + struct.pack("<I", len(sfbk) + 4) + b"sfbk" + sfbk

    Path(out_path).write_bytes(out)

    return {
        "path": out_path,
        "bytes": len(out),
        "samples": 2,
        "sample_points": len(smpl),
        "loop1": (ls1, le1),
        "loop2": (off2 + ls2, off2 + le2),
        "sr": sr,
    }


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "out" / "test.sf2"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = build_sf2(str(out))
    print(f"已生成 {meta['path']}  ({meta['bytes']} 字节, "
          f"{meta['sample_points']} 采样点)")
    print(f"  采样1 循环点 {meta['loop1']}, 采样2 循环点 {meta['loop2']}")
