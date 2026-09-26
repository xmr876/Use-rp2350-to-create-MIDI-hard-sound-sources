"""
gmss/player.py — 板载镜像的**参考播放器**（PC 端）

用途
----
这是固件里 sampler.c 的**参考实现**。它读真实产物（**单个 library.bin**），
按固件的算法渲染音频，用来：

  1. 验证 PC 工具链产出的镜像**确实能播**（闭环验证）
  2. 给 C 版一个逐位可对拍的标准 —— C 版必须输出完全相同的采样序列
  3. 在没有硬件时就能听效果（导出 WAV）

固件对应文件：firmware/src/sampler.c / voice.c
两边必须逐位一致，否则循环点会有咔哒、音高会偏。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import adpcm

# 与 gmss_format.h 一致
GMSS_MAGIC = 0x53534D47
CHIP_MAGIC = 0x43534D47
HEADER_SIZE = 64
CHIP_HDR_SIZE = 16
ZONE_SIZE = 56
INSTR_SIZE = 32
POOL_OFFSET = 0x1000      # 旧多片方案的池偏移，单片方案不再使用（保留仅为兼容引用）

# ★ 库在整片 flash 里的起始地址（与 gmss_format.h 的 GMSS_LIB_BASE 一致）。
#   zone 表里的 sample_off 是**整片 flash 的绝对地址**，而 library.bin
#   从 LIB_BASE 开始 —— 读文件前必须减掉它（见 read_samples）。
#   ★ 这个常量曾经只出现在注释里而没定义，导致 read_samples() 直接 NameError。
LIB_BASE = 0x40000

CODEC_PCM16 = 0
CODEC_PCM8 = 1
CODEC_ADPCM4 = 2

ZF_LOOPED = 0x0001
ZF_PERCUSSIVE = 0x0002

LOOP_NONE = 0xFFFFFFFF


# ==================================================================
# 结构体
# ==================================================================
@dataclass
class Zone:
    key_lo: int
    key_hi: int
    vel_lo: int
    vel_hi: int
    chip_id: int
    codec: int
    flags: int
    sample_off: int
    sample_len: int
    loop_start: int
    loop_end: int
    attack_samples: int
    keyframe_off: int
    keyframe_count: int
    root_key: int
    tune_cents: int
    gain_db100: int
    env_attack_ms: int
    env_decay_ms: int
    env_sustain: int
    env_release_ms: int

    @property
    def looped(self) -> bool:
        return bool(self.flags & ZF_LOOPED)

    @property
    def percussive(self) -> bool:
        return bool(self.flags & ZF_PERCUSSIVE)


@dataclass
class Instrument:
    zone_first: int
    zone_count: int
    bank: int
    program: int
    fixed_note: int
    name: str


class GmssImage:
    """加载板载镜像，提供"按 MIDI 音符取采样点"的接口"""

    def __init__(self, image_dir: Path):
        self.dir = Path(image_dir)
        # ★ v2：整库一个文件，文件偏移 0 == flash 地址 LIB_BASE (0x40000)
        lib = (self.dir / "library.bin").read_bytes()

        (magic, self.version_major, self.version_minor, self.header_size,
         self.sample_rate, self.total_zones, self.instrument_count,
         self.drumkit_count, self.zone_table_off, self.instr_table_off,
         self.pool_off, self.chip_count, self.flags, _r0,
         self.total_bytes, self.checksum, _r1) = struct.unpack_from(
            "<IBBHIIHHIIIBBHIII", lib, 0)

        if magic != GMSS_MAGIC:
            raise ValueError(f"library.bin 魔数错: 0x{magic:08X}")
        if self.sample_rate != 48000:
            raise ValueError(f"采样率应为 48000，实际 {self.sample_rate}")

        if self.total_zones * ZONE_SIZE > self.instr_table_off - self.zone_table_off:
            raise ValueError("zone 表长度超出头部声明")

        # 校验和自检：覆盖 zone 表 + 128 槽音色表 + 鼓表
        end = (self.zone_table_off + self.total_zones * ZONE_SIZE
               + 128 * INSTR_SIZE + self.drumkit_count * INSTR_SIZE)
        if end > len(lib):
            raise ValueError("音色表越出 library.bin")
        actual = fnv1a(lib[self.zone_table_off:end])
        if actual != self.checksum:
            raise ValueError(
                f"校验和不符：头部 {self.checksum:08X}，实算 {actual:08X}")

        # zone 表
        self.zones: List[Zone] = []
        for i in range(self.total_zones):
            off = self.zone_table_off + i * ZONE_SIZE
            self.zones.append(parse_zone(lib, off))

        # 验证每条的 sample_off 都落在池内（绝对地址必须是 LIB_BASE + pool_off 之后）
        # ★ zone 里的 sample_off 是**整片 flash 的绝对地址**，而本文件从
        #   LIB_BASE 开始 —— 读之前必须减去 LIB_BASE。搞错就会静默读到垃圾。
        for i, z in enumerate(self.zones):
            if z.sample_off < LIB_BASE + self.pool_off:
                raise ValueError(
                    f"zone{i} 的 sample_off=0x{z.sample_off:X} 落在表区内"
                    f"（池从 0x{LIB_BASE + self.pool_off:X} 开始）")

        # 音色表（旋律 128 槽 + 鼓组）
        self.instruments: Dict[Tuple[int, int], Instrument] = {}
        for slot in range(128):
            off = self.instr_table_off + slot * INSTR_SIZE
            inst = parse_instrument(lib, off)
            self.instruments[(0, inst.program)] = inst
        for i in range(self.drumkit_count):
            off = self.instr_table_off + (128 + i) * INSTR_SIZE
            inst = parse_instrument(lib, off)
            self.instruments[(128, inst.program)] = inst

        self.lib = lib

    # ---- 库访问 ----
    def chip(self, chip_id: int = 0) -> bytes:
        """保留旧接口名（测试和固件对拍脚本都在用）。单片方案恒返回整库。"""
        return self.lib

    def find_zone(self, program: int, key: int, vel: int,
                  bank: int = 0) -> Optional[Zone]:
        """选 zone：在匹配的 zone 中挑力度范围最窄的（SF2 的"最具体者优先"）"""
        inst = self.instruments.get((bank, program))
        if inst is None or inst.zone_count == 0:
            return None

        best = None
        best_span = 0x7FFFFFFF
        for i in range(inst.zone_first, inst.zone_first + inst.zone_count):
            if i >= len(self.zones):
                break
            z = self.zones[i]
            if not (z.key_lo <= key <= z.key_hi):
                continue
            if not (z.vel_lo <= vel <= z.vel_hi):
                continue
            span = z.vel_hi - z.vel_lo
            if span < best_span:
                best_span, best = span, z
        return best

    # ---- 采样点读取（与固件 sampler.c 必须逐位一致）----
    def read_samples(self, z: Zone, start: int, count: int) -> np.ndarray:
        """读 zone 里第 start 帧起的 count 帧（**不做循环回绕**）"""
        blob = self.lib
        # ★ zone.sample_off 是整片 flash 的绝对地址；本文件从 LIB_BASE 开始。
        base = z.sample_off - LIB_BASE
        pcm_bytes = z.attack_samples * 2

        out = np.empty(count, dtype="<i2")

        # 前段：PCM16 无损（若 start 落在这一段内）
        if start < z.attack_samples:
            n_pcm = min(count, z.attack_samples - start)
            out[:n_pcm] = np.frombuffer(
                blob, dtype="<i2", count=n_pcm,
                offset=base + start * 2)
            done = n_pcm
        else:
            done = 0

        if done >= count:
            return out

        if z.codec == CODEC_ADPCM4:
            # ★ ADPCM：必须先从关键帧恢复状态再顺序解码
            #   （与 adpcm.decode_at 同一套逻辑，固件也必须这么做）
            kf = self._load_keyframes(z, blob, base)
            got = adpcm.decode_at(
                blob, start + done, count - done, kf,
                byte_offset=base + pcm_bytes)
            out[done:] = got
        elif z.codec == CODEC_PCM16:
            # 整段 PCM16
            out[done:] = np.frombuffer(
                blob, dtype="<i2", count=count - done,
                offset=base + (start + done) * 2)
        else:
            raise NotImplementedError(f"codec {z.codec} 未实现")

        return out

    def _load_keyframes(self, z: Zone, blob: bytes,
                        base: int) -> List[adpcm.AdpcmState]:
        """从 zone 的关键帧表恢复状态列表"""
        if z.keyframe_off == LOOP_NONE or z.keyframe_count == 0:
            # 没有关键帧表 → 只能从头解码。
            # 这不该发生（PC 工具总会写表），但兜底避免崩溃。
            return [adpcm.AdpcmState()]
        kf = []
        off = base + z.keyframe_off
        for i in range(z.keyframe_count):
            kf.append(adpcm.AdpcmState.from_bytes(blob[off + i * 4: off + i * 4 + 4]))
        return kf


# ==================================================================
# 解析辅助
# ==================================================================
def parse_zone(buf: bytes, off: int) -> Zone:
    """解析一条 zone —— 格式串必须与 layout.pack_zone() 逐字段对应。

    ★ 这里也踩过 58 vs 56 那个坑：格式串里原来多了一个 h（int16），
      比 C 结构体长 2 字节。两个 Python 模块用同样错的常量互相自洽，
      所以 38 个单测全过 —— 而固件那边读的是**真的 56 字节**布局。
      现在由 tools/check_struct_sizes.py 用 ARM 编译器实测 sizeof 兜底。
    """
    (kl, kh, vl, vh, chip, codec, flags, soff, slen, ls, le,
     attack, kf_off, kf_n, root, tune, gain,
     ea, ed, es, er, _fx, _pad2, _rsv) = struct.unpack_from(
        "<BBBBBBHIIIIIIIBbhHHhHhHI", buf, off)
    return Zone(
        key_lo=kl, key_hi=kh, vel_lo=vl, vel_hi=vh,
        chip_id=chip, codec=codec, flags=flags,
        sample_off=soff, sample_len=slen, loop_start=ls, loop_end=le,
        attack_samples=attack, keyframe_off=kf_off, keyframe_count=kf_n,
        root_key=root, tune_cents=tune, gain_db100=gain,
        env_attack_ms=ea, env_decay_ms=ed, env_sustain=es, env_release_ms=er,
    )


def parse_instrument(buf: bytes, off: int) -> Instrument:
    zf, zc, bank, prog, fixed, _pad = struct.unpack_from("<HHBBbb", buf, off)
    name = buf[off + 8:off + 24].split(b"\x00", 1)[0].decode(
        "latin-1", errors="replace")
    return Instrument(zone_first=zf, zone_count=zc, bank=bank,
                      program=prog, fixed_note=fixed, name=name)


def fnv1a(data: bytes) -> int:
    """与 tools/gmss/gmss/layout.py 及固件必须逐位一致"""
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h
