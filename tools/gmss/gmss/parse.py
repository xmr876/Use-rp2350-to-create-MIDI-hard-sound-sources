"""
gmss/parse.py — 把 SF2 的 hydra 记录解析成 Python 对象

产出 SoundFont 对象：
    .sample_data  : numpy.int16 数组（全部采样拼接）
    .samples      : [SampleHead]
    .instruments  : [Instrument]，每个含已合并的 local zones
    .presets      : [Preset]，每个含已合并的 local zones
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import riff, sf2defs as D


# ==========================================================================
# 数据结构
# ==========================================================================

@dataclass
class Generator:
    oper: int
    amount: int          # int16 解释
    raw_u16: int         # 原始 u16，用于 keyRange/velRange


@dataclass
class Zone:
    gens: Dict[int, Generator] = field(default_factory=dict)
    key_lo: int = 0
    key_hi: int = 127
    vel_lo: int = 0
    vel_hi: int = 127
    instrument_idx: Optional[int] = None   # 仅 preset zone 有

    def get(self, name: str, default: Optional[int] = None) -> Optional[int]:
        g = self.gens.get(D.GEN[name])
        if g is not None:
            return g.amount
        return D.GEN_DEFAULTS.get(D.GEN[name], default)

    def get_u16(self, name: str) -> Optional[int]:
        g = self.gens.get(D.GEN[name])
        return None if g is None else g.raw_u16

    def has(self, name: str) -> bool:
        return D.GEN[name] in self.gens


@dataclass
class SampleHead:
    name: str
    start: int
    end: int
    startloop: int
    endloop: int
    sample_rate: int
    original_key: int
    correction: int      # 音分
    link: int
    sample_type: int


@dataclass
class Instrument:
    name: str
    zones: List[Zone] = field(default_factory=list)   # 只含 local zones（已合并 global）


@dataclass
class Preset:
    name: str
    program: int
    bank: int
    zones: List[Zone] = field(default_factory=list)


@dataclass
class SoundFont:
    path: str
    info: Dict[str, str]
    sample_data: np.ndarray
    samples: List[SampleHead]
    instruments: List[Instrument]
    presets: List[Preset]


# ==========================================================================
# 底层记录解析
# ==========================================================================

def _read_records(blob: bytes, size: int, fmt: str):
    """按固定记录长度切分并逐条 unpack。最后一条通常是 EOP 终止记录，调用方丢弃。"""
    n = len(blob) // size
    tail = len(blob) % size
    if tail:
        raise ValueError(f"记录块长度 {len(blob)} 不是 {size} 的整数倍（余 {tail}）")
    for i in range(n):
        yield i, struct.unpack_from(fmt, blob, i * size)


def _cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("latin-1", errors="replace")


# ==========================================================================
# shdr
# ==========================================================================

def parse_shdr(blob: bytes) -> List[SampleHead]:
    """shdr 记录 46 字节，无保留字段：
    name[20] start:u32 end:u32 startloop:u32 endloop:u32 sampleRate:u32
    originalKey:u8 correction:i8 sampleLink:u16 sampleType:u16
    """
    out: List[SampleHead] = []
    for _, r in _read_records(blob, D.SZ_SHDR, "<20sIIIIIBbHH"):
        name, start, end, sl, el, rate, okey, corr, link, stype = r
        out.append(SampleHead(
            name=_cstr(name), start=start, end=end,
            startloop=sl, endloop=el, sample_rate=rate,
            original_key=okey, correction=corr, link=link, sample_type=stype,
        ))
    if out and out[-1].name == "EOS":
        out.pop()

    # SF3 用 Ogg Vorbis 存采样，本工具链不支持——早失败，别产出垃圾
    ogg = [s.name for s in out if s.sample_type & D.SF2_SAMPLE_OGG]
    if ogg:
        raise ValueError(
            f"这是 SF3（Ogg Vorbis 采样），不是 SF2。涉及 {len(ogg)} 个采样，"
            f"例如 {ogg[0]!r}。请换成 .sf2 文件。"
        )
    return out


# ==========================================================================
# bag + gen → Zone 列表
# ==========================================================================

def _bag_count(blob: bytes) -> int:
    """bag 条数（不含末尾终止 bag）"""
    n = len(blob) // D.SZ_PBAG
    return n - 1 if n else 0


def _parse_zones(bag_blob: bytes, gen_blob: bytes,
                 first: int, last: int) -> List[Zone]:
    """把 bag[first:last] 覆盖的生成器转成 Zone 列表，并合并 zone 0 的全局生成器。

    SF2 语义：bag[i] 覆盖 [genNdx(bag[i]), genNdx(bag[i+1])) 这段生成器。
    若 bag[first] 既不含 sampleID 也不含 instrument，则它是全局区，
    其生成器对后续所有 zone 生效（local 覆盖 global）。

    直接按区间在原块上解析，不构造临时切片——切片的 genNdx 偏移会变，容易出错。
    """
    if last <= first:
        return []
    total_bags = len(bag_blob) // D.SZ_PBAG
    if last > total_bags:
        raise ValueError(f"bag 区间 [{first},{last}) 超出实际 {total_bags} 条")

    total_gens = len(gen_blob) // D.SZ_PGEN

    # 不变式（务必保持，这里踩过坑）：
    #   bag[i] 覆盖 [genNdx(bag[i]), genNdx(bag[i+1]))。
    #   终止 bag 的 genNdx == 真实生成器条数，即**指向终端记录自身**。
    # 所以越界回退要返回 total_gens（而非 total_gens-1），
    # 且 read_gens 的钳位也用 total_gens——否则最后一条真实生成器会被
    # 当成终端记录丢掉（症状：zone 少一个生成器，且很难看出来）。
    def gen_ndx_of(i: int) -> int:
        if i < total_bags:
            return struct.unpack_from("<H", bag_blob, i * D.SZ_PBAG)[0]
        return total_gens

    def read_gens(g0: int, g1: int) -> List[Generator]:
        g0 = max(0, min(g0, total_gens))
        g1 = max(0, min(g1, total_gens))
        out: List[Generator] = []
        for k in range(g0, g1):
            oper, amt_u16 = struct.unpack_from("<HH", gen_blob, k * D.SZ_PGEN)
            out.append(Generator(oper=oper, amount=_s16(amt_u16), raw_u16=amt_u16))
        return out

    raw_zones: List[List[Generator]] = []
    for i in range(first, last):
        raw_zones.append(read_gens(gen_ndx_of(i), gen_ndx_of(i + 1)))

    def _is_global(glist: List[Generator]) -> bool:
        ops = {g.oper for g in glist}
        return D.GEN["sampleID"] not in ops and D.GEN["instrument"] not in ops

    global_gens: Dict[int, Generator] = {}
    start = 0
    if raw_zones and _is_global(raw_zones[0]):
        for g in raw_zones[0]:
            global_gens[g.oper] = g
        start = 1

    zones: List[Zone] = []
    for glist in raw_zones[start:]:
        z = Zone()
        for oper, g in global_gens.items():     # 全局先铺
            z.gens[oper] = g
        for g in glist:                          # local 覆盖
            z.gens[g.oper] = g

        kr = z.get_u16("keyRange")
        if kr is not None:
            z.key_lo, z.key_hi = D.key_range(kr)
        vr = z.get_u16("velRange")
        if vr is not None:
            z.vel_lo, z.vel_hi = D.vel_range(vr)

        inst_g = z.gens.get(D.GEN["instrument"])
        if inst_g is not None:
            z.instrument_idx = inst_g.amount
        zones.append(z)

    return zones


# ==========================================================================
# phdr / inst
# ==========================================================================

def parse_phdr(blob: bytes) -> List[Tuple[str, int, int, int]]:
    """phdr 记录 38 字节：
    name[20] preset:u16 bank:u16 bagNdx:u16 library:i32 genre:i32 morphology:i32
    (preset 用 u16 且值域 0..127，按 u16 读即可)
    """
    out = []
    for _, r in _read_records(blob, D.SZ_PHDR, "<20sHHHiII"):
        name, prog, bank, bag_ndx, _lib, _genre, _morph = r
        out.append((_cstr(name), prog, bank, bag_ndx))
    if out and out[-1][0] == "EOP":
        out.pop()
    return out


def parse_inst(blob: bytes) -> List[Tuple[str, int]]:
    """inst 记录 22 字节：name[20] bagNdx:u16 —— 格式里最短的记录"""
    out = []
    for _, r in _read_records(blob, D.SZ_INST, "<20sH"):
        name, bag_ndx = r
        out.append((_cstr(name), bag_ndx))
    if out and out[-1][0] == "EOI":
        out.pop()
    return out


# ==========================================================================
# 顶层
# ==========================================================================

def load_soundfont(path: str, verbose: bool = False) -> SoundFont:
    raw = riff.load(path)
    riff.check_pdta(raw["pdta"])

    sample_data = np.frombuffer(raw["smpl"], dtype="<i2")

    shdrs = parse_shdr(raw["pdta"][b"shdr"])
    if verbose:
        print(f"  采样头 {len(shdrs)} 条，采样总点数 {len(sample_data)}")

    # ---- instruments ----
    inst_heads = parse_inst(raw["pdta"][b"inst"])
    inst_bag = raw["pdta"][b"ibag"]
    inst_gen = raw["pdta"][b"igen"]
    instruments: List[Instrument] = []
    for i, (name, bag_ndx) in enumerate(inst_heads):
        next_bag = (inst_heads[i + 1][1] if i + 1 < len(inst_heads)
                    else _bag_count(inst_bag))
        zones = _parse_zones(inst_bag, inst_gen, bag_ndx, next_bag)
        instruments.append(Instrument(name=name, zones=zones))

    # ---- presets ----
    preset_heads = parse_phdr(raw["pdta"][b"phdr"])
    p_bag = raw["pdta"][b"pbag"]
    p_gen = raw["pdta"][b"pgen"]
    presets: List[Preset] = []
    for i, (name, prog, bank, bag_ndx) in enumerate(preset_heads):
        next_bag = (preset_heads[i + 1][3] if i + 1 < len(preset_heads)
                    else _bag_count(p_bag))
        zones = _parse_zones(p_bag, p_gen, bag_ndx, next_bag)
        presets.append(Preset(name=name, program=prog, bank=bank, zones=zones))

    if verbose:
        print(f"  乐器 {len(instruments)} 个，预设 {len(presets)} 个")

    return SoundFont(
        path=path, info=raw["INFO"], sample_data=sample_data,
        samples=shdrs, instruments=instruments, presets=presets,
    )


# --- 小工具 ---------------------------------------------------------------

def _s16(v: int) -> int:
    """u16 → int16（生成器 genAmount 需要按有符号解释）"""
    return v - 0x10000 if v & 0x8000 else v
