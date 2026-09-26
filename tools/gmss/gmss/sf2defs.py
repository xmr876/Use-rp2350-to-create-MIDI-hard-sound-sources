"""
gmss/sf2defs.py — SoundFont 2.04 记录结构与生成器枚举定义

记录长度（SF2 规范 2.04，第 7 章）：
    phdr / inst / shdr : 38 字节
    pbag / ibag        :  4 字节
    pgen / igen        :  4 字节（本工具按 int16 读，见 parse.py 说明）
    pmod / imod        : 10 字节

注意 pgen/igen 的规范定义是 (sfGenOper:u16, genAmount:u16)，但 genAmount
在实际文件中是「两个 int8 组成的 subrange」或「一个 int16」。按 u16 读会把
负数（如 coarseTune = -1 → 0xFFFF）误读成 65535。本工具按 int16 解释 genAmount，
这对 keyRange/velRange 的字节对表示是等价的（小端下 0x7F00 = 高字节 0x7F、
低字节 0x00，正是 keyHigh/keyLow 的顺序反过来，所以单独处理）。
"""
from __future__ import annotations

import math
import struct

# --------------------------------------------------------------------------
# 记录大小（已对照 SF2 2.04 规范与第三方格式解剖资料核实）
# --------------------------------------------------------------------------
#   phdr : name[20] + preset:u16 bank:u16 bagNdx:u16 + library/genre/morphology:i32×3 = 38
#   inst : name[20] + bagNdx:u16                                                    = 22
#   shdr : name[20] + start/end/loopStart/loopEnd/rate:u32×5 + pitch:u8 corr:i8
#          + link:u16 type:u16                                                      = 46
SZ_PHDR = 38
SZ_PBAG = 4
SZ_PGEN = 4
SZ_PMOD = 10
SZ_INST = 22
SZ_IBAG = 4
SZ_IGEN = 4
SZ_IMOD = 10
SZ_SHDR = 46

# 采样类型位（shdr 的 sampleType 字段）
SF2_SAMPLE_MONO        = 0x0001
SF2_SAMPLE_RIGHT       = 0x0002
SF2_SAMPLE_LEFT        = 0x0004
SF2_SAMPLE_LINKED      = 0x0008
SF2_SAMPLE_ROM         = 0x0010
SF2_SAMPLE_OGG         = 0x0010   # SF3：位 0x10 表示 Ogg Vorbis 采样

# SF2 规定每个采样之后至少留 46 个零采样点作为余量
SF2_SAMPLE_TAIL_POINTS = 46

# --------------------------------------------------------------------------
# 生成器枚举（SF2 规范 8.1.2）
# --------------------------------------------------------------------------
GEN = {
    "startAddrsOffset":        0,
    "endAddrsOffset":          1,
    "startloopAddrsOffset":    2,
    "endloopAddrsOffset":      3,
    "startAddrsCoarseOffset":  4,
    "modLfoToPitch":           5,
    "vibLfoToPitch":           6,
    "modEnvToPitch":           7,
    "initialFilterFc":         8,
    "initialFilterQ":          9,
    "modLfoToFilterFc":       10,
    "modEnvToFilterFc":       11,
    "endAddrsCoarseOffset":   12,
    "modLfoToVolume":         13,
    "chorusEffectsSend":      15,
    "reverbEffectsSend":      16,
    "pan":                    17,
    "delayModLFO":            21,
    "freqModLFO":             22,
    "delayVibLFO":            23,
    "freqVibLFO":             24,
    "delayModEnv":            25,
    "attackModEnv":           26,
    "holdModEnv":             27,
    "decayModEnv":            28,
    "sustainModEnv":          29,
    "releaseModEnv":          30,
    "keynumToModEnvHold":     31,
    "keynumToModEnvDecay":    32,
    "delayVolEnv":            33,
    "attackVolEnv":           34,
    "holdVolEnv":             35,
    "decayVolEnv":            36,
    "sustainVolEnv":          37,
    "releaseVolEnv":          38,
    "keynumToVolEnvHold":     39,
    "keynumToVolEnvDecay":    40,
    "instrument":             41,
    "reserved1":              42,
    "keyRange":               43,
    "velRange":               44,
    "startloopAddrsCoarseOffset": 45,
    "keynum":                 46,
    "velocity":               47,
    "initialAttenuation":     48,
    "reserved2":              49,
    "endloopAddrsCoarseOffset": 50,
    "coarseTune":             51,
    "fineTune":               52,
    "sampleID":               53,
    "sampleModes":            54,
    "reserved3":              55,
    "scaleTuning":            56,
    "exclusiveClass":         57,
    "overridingRootKey":      58,
}
GEN_NAME = {v: k for k, v in GEN.items()}

# 这些生成器是「全局」的，只能出现在 zone 的 0 号位置
GLOBAL_ONLY = {
    GEN["startAddrsOffset"], GEN["endAddrsOffset"],
    GEN["startloopAddrsOffset"], GEN["endloopAddrsOffset"],
    GEN["startAddrsCoarseOffset"], GEN["endAddrsCoarseOffset"],
    GEN["startloopAddrsCoarseOffset"], GEN["endloopAddrsCoarseOffset"],
    GEN["keynum"], GEN["velocity"], GEN["sampleModes"], GEN["exclusiveClass"],
    GEN["overridingRootKey"],
}

# --------------------------------------------------------------------------
# 单位换算
# --------------------------------------------------------------------------

def tc2ms(timecents: int) -> float:
    """timecents → 毫秒。SF2 规范：秒 = 2^(tc/1200)，tc = -32768 表示 0。

    验证：tc=0 → 1.0s(1000ms)；tc=1200 → 2.0s；tc=-1200 → 0.5s
    """
    if timecents <= -32768:
        return 0.0
    return (2.0 ** (timecents / 1200.0)) * 1000.0


def tc2s(timecents: int) -> float:
    return tc2ms(timecents) / 1000.0


def cb2gain(centibels: float) -> float:
    """centibels 衰减 → 线性增益。0 cb = 1.0，100 cb = 0.1"""
    return 10.0 ** (-centibels / 200.0)


def cb2q15(centibels: float) -> int:
    """centibels 衰减 → Q15 线性增益（0..32767）"""
    g = cb2gain(centibels)
    return max(0, min(32767, int(round(g * 32767.0))))


def cents2ratio(cents: float) -> float:
    return 2.0 ** (cents / 1200.0)


def semitones2ratio(semitones: float) -> float:
    return 2.0 ** (semitones / 12.0)


# --------------------------------------------------------------------------
# 生成器默认值（SF2 规范 8.1.3，按 instrument 层）
# --------------------------------------------------------------------------
GEN_DEFAULTS = {
    GEN["initialFilterFc"]:      13500,
    GEN["initialFilterQ"]:           0,
    GEN["pan"]:                      0,
    GEN["delayVolEnv"]:         -12000,
    GEN["attackVolEnv"]:        -12000,
    GEN["holdVolEnv"]:          -12000,
    GEN["decayVolEnv"]:         -12000,
    GEN["sustainVolEnv"]:            0,
    GEN["releaseVolEnv"]:       -12000,
    GEN["keynumToVolEnvHold"]:       0,
    GEN["keynumToVolEnvDecay"]:      0,
    GEN["coarseTune"]:               0,
    GEN["fineTune"]:                 0,
    GEN["scaleTuning"]:            100,
    GEN["initialAttenuation"]:       0,
    GEN["sampleModes"]:              0,
    GEN["overridingRootKey"]:       -1,
    GEN["keynum"]:                  -1,
    GEN["velocity"]:                -1,
    GEN["exclusiveClass"]:           0,
}


def key_range(amount_u16: int) -> tuple[int, int]:
    """keyRange 的 genAmount → (keyLow, keyHigh)。

    SF2 规范 p.19 的 rangesType 是 `{uint8 byLo; uint8 byHi;}`，
    即 **低字节 = 低值，高字节 = 高值**（与 genAmountType 联合体共用 16 位）。

    佐证：NAudio（.NET 主流 SF2 解析库）的 SampleMap 正是这么取的——
        KeyLowRange  = (byte)(KeyRange & 0xFF)
        KeyHighRange = (byte)((KeyRange & 0xFF00) >> 8)

    所以 keyRange 0-127 编码为 0x007F，0-60 为 0x003C。
    注意：网上有资料声称"低字节=高音、高字节=低音"，那是错的
    （那个说法与它自己举的 15360 例子自相矛盾）。
    """
    return amount_u16 & 0xFF, (amount_u16 >> 8) & 0xFF


def vel_range(amount_u16: int) -> tuple[int, int]:
    """velRange 同上：低字节 = velLow，高字节 = velHigh。
    velRange 0-63 编码为 0x3F00 → 读回 (0x00, 0x3F) = (0, 63)"""
    return amount_u16 & 0xFF, (amount_u16 >> 8) & 0xFF


def pack_range(lo: int, hi: int) -> int:
    """(lo, hi) → genAmount。与 key_range 互逆，供写出 SF2 时使用。"""
    return ((hi & 0xFF) << 8) | (lo & 0xFF)
