"""
gmss/plan.py — 从 SF2 解析结果生成"可播放的 zone 计划"

职责：
  1. 把 preset（bank/program）映射到 GM 音色表
  2. 展开 preset zone → instrument zone → sample 的三层引用
  3. 抽取采样数据（含 loop/offset 修正）
  4. **统一重采样到板载 48kHz**（关键：让固件的音高计算只剩 2^((key-root)/12)）
  5. 合并包络/衰减/声像等参数

不在本模块做的：编码（encode.py）、跨片布局（layout.py）。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import sf2defs as D
from .parse import Instrument, Preset, SoundFont, Zone

# 板载采样率，必须与 firmware/include/gmss_format.h 的 GMSS_SAMPLE_RATE 一致
BOARD_RATE = 48000

# 无循环
LOOP_NONE = 0xFFFFFFFF


@dataclass
class Sample:
    """一段已抽取、已重采样到 48kHz 的音频"""
    name: str
    data: np.ndarray            # int16, 单声道, 48kHz
    loop_start: int             # LOOP_NONE = 不循环
    loop_end: int
    root_key: int               # 原始音高
    tune_cents: int             # 微调音分
    src_rate: int               # 原始采样率（记录用）
    content_hash: str = ""      # 内容指纹，供 layout 去重（在抽取时算一次）

    @property
    def looped(self) -> bool:
        return self.loop_start != LOOP_NONE and self.loop_end > self.loop_start

    def key(self) -> str:
        """去重键：内容指纹 + 循环点 + 音高参数。

        ★ 必须用**内容**指纹，不能用 id()——
        每个 zone 抽取时都会 .copy() 出新对象，id 永远不同，去重会失效，
        结果同一个采样被编码并存了 N 遍，Flash 直接爆掉。
        指纹在抽取时算一次（见 _make_sample），这里只做字符串拼接。
        """
        return "{}|{}|{}|{}|{}".format(
            self.content_hash, self.loop_start, self.loop_end,
            self.root_key, self.tune_cents)


@dataclass
class PlannedZone:
    """一个可直接编码上板的分区"""
    key_lo: int
    key_hi: int
    vel_lo: int
    vel_hi: int
    sample: Sample
    # 包络（毫秒 / Q15）
    attack_ms: int
    decay_ms: int
    sustain_q15: int
    release_ms: int
    gain_db100: int             # 0.01dB 单位
    percussive: bool            # 打击乐：note-off 即进 release
    program: int = -1           # 溯源用
    bank: int = 0
    part: str = ""              # 溯源用（preset 名）


# ==========================================================================
# 重采样
# ==========================================================================

def resample_to_board(x: np.ndarray, src_rate: int,
                      loop: Tuple[int, int] | None) -> Tuple[np.ndarray, Tuple[int, int] | None]:
    """把采样重采样到 BOARD_RATE，并**等比修正循环点**。

    为什么在 PC 端做而不是固件做：
      固件要为 48 个复音同时重采样，CPU 根本不够（见可行性文档 §3.2）。
      在 PC 端一次性重采样，固件的音高计算就只剩
          ratio = 2^((key - root_key + tune/100) / 12)
      省掉了采样率补偿这一项，也省掉了每个复音的插值器重采样误差。

    用线性插值就够了——源采样和 48kHz 都是 44.1k/48k 量级，
    比值接近 1，线性插值的误差远低于 16bit 量化噪声。
    """
    if src_rate == BOARD_RATE:
        return x, loop

    n_src = len(x)
    n_dst = int(round(n_src * BOARD_RATE / src_rate))
    if n_dst < 1:
        return np.zeros(1, dtype="<i2"), loop

    # 用浮点做线性插值（PC 端不在乎这点开销）
    idx = np.arange(n_dst, dtype=np.float64) * (n_src / n_dst)
    i0 = np.floor(idx).astype(np.int64)
    frac = idx - i0
    i1 = np.minimum(i0 + 1, n_src - 1)

    xf = x.astype(np.float64)
    out = xf[i0] * (1.0 - frac) + xf[i1] * frac
    out = np.clip(np.rint(out), -32768, 32767).astype("<i2")

    loop_dst = None
    if loop is not None:
        ls, le = loop
        scale = n_dst / n_src
        ls2 = int(round(ls * scale))
        le2 = int(round(le * scale))
        # 夹紧到合法范围
        ls2 = max(0, min(ls2, n_dst - 2))
        le2 = max(ls2 + 1, min(le2, n_dst))
        loop_dst = (ls2, le2)

    return out, loop_dst


# ==========================================================================
# 采样抽取
# ==========================================================================

def _make_sample(sf: SoundFont, izone: Zone, use_loop: bool) -> Optional[Sample]:
    """从 instrument zone 抽出一段采样"""
    sid = izone.gens.get(D.GEN["sampleID"])
    if sid is None:
        return None
    si = sid.amount
    if not (0 <= si < len(sf.samples)):
        return None
    sh = sf.samples[si]

    # --- 起始/结束（含 coarse 偏移，单位 32768）---
    start = (sh.start
             + izone.get("startAddrsOffset", 0)
             + izone.get("startAddrsCoarseOffset", 0) * 32768)
    end = (sh.end
           + izone.get("endAddrsOffset", 0)
           + izone.get("endAddrsCoarseOffset", 0) * 32768)

    # --- 循环点 ---
    mode = izone.get("sampleModes", 0) or 0
    looped = use_loop and (mode & 0x3) in (1, 3)     # 1=连续, 3=连续+包络释放后继续
    ls = (sh.startloop
          + izone.get("startloopAddrsOffset", 0)
          + izone.get("startloopAddrsCoarseOffset", 0) * 32768)
    le = (sh.endloop
          + izone.get("endloopAddrsOffset", 0)
          + izone.get("endloopAddrsCoarseOffset", 0) * 32768)

    # 夹紧到采样池边界
    n_pool = len(sf.sample_data)
    start = max(0, min(start, n_pool))
    end = max(start, min(end, n_pool))
    if end - start < 8:
        return None

    data = sf.sample_data[start:end].copy()

    # ★ 裁剪循环点之后的尾巴：采样里 loop_end 之后的那些点是"淡出尾巴"，
    #   上板后循环播放用不到，留着纯属浪费 Flash。
    #   但不循环的采样（如打击乐）必须保留完整尾巴。
    if looped:
        ls_rel = ls - start
        le_rel = le - start
        # 合法性检查；SF2 里经常有越界的循环点，必须夹紧而不是报错
        ls_rel = max(0, min(ls_rel, len(data) - 2))
        le_rel = max(ls_rel + 1, min(le_rel, len(data)))
        if le_rel - ls_rel < 4:
            looped = False

    if looped:
        # 保留 loop_end 之后一小段（用于循环交叉淡化/Hermite 插值的前瞻）
        # 48kHz 下 256 点 ≈ 5.3ms，足够
        keep_tail = 256
        cut = min(len(data), le_rel + keep_tail)
        data = data[:cut]
        loop_pair = (ls_rel, le_rel)
    else:
        loop_pair = None

    # --- 统一重采样到 48kHz ---
    src_rate = sh.sample_rate if sh.sample_rate > 0 else 44100
    data, loop_pair = resample_to_board(data, src_rate, loop_pair)

    # --- root key ---
    rk = izone.get("overridingRootKey", -1)
    if rk is None or rk < 0:
        rk = sh.original_key if sh.original_key > 0 else 60
    rk = max(0, min(127, rk))

    # --- 音分修正：采样自带 correction + fineTune + coarseTune*100 ---
    cents = int(sh.correction or 0)
    cents += int(izone.get("fineTune", 0) or 0)
    cents += int(izone.get("coarseTune", 0) or 0) * 100
    # 折叠到 -100..100（超过一个半音的部分用 root_key 表达更省事）
    while cents > 100:
        cents -= 100
        rk = min(127, rk + 1)
    while cents < -100:
        cents += 100
        rk = max(0, rk - 1)

    return Sample(
        name=sh.name, data=data,
        loop_start=loop_pair[0] if loop_pair else LOOP_NONE,
        loop_end=loop_pair[1] if loop_pair else 0,
        root_key=rk, tune_cents=cents, src_rate=src_rate,
        content_hash=hashlib.sha1(data.tobytes()).hexdigest(),
    )


# ==========================================================================
# 包络与音量的合并（preset zone × instrument zone）
# ==========================================================================

def _merge_envelope(pz: Zone, iz: Zone, key: int) -> Tuple[int, int, int, int, int]:
    """返回 (attack_ms, decay_ms, sustain_q15, release_ms, gain_db100)

    SF2 语义：时间类生成器（timecents）在 preset 与 instrument
    两层是**相加**关系，不是覆盖。音量衰减（initialAttenuation）也是相加。

    ★★ 但"相加"只对**这一层真的写了**的 generator 成立 ——
       某层没写就不参与相加，两层都没写才用规范默认值。

       ★★★ 这里曾经用 `z.get(name)` 来判断，而 `Zone.get()` 在
       generator 缺失时会**返回 SF2 默认值**，于是"缺失"被当成了
       "显式写了默认值"，跟着一起加进去，结果翻倍。

       后果非常严重，实例：TimGM6mb 的钢琴
         · instrument 层：decayVolEnv = 4955 tc（= 17.5 秒）
         · preset  层：没写这个 generator
         · 正确结果：4955 tc → 17.5 秒
         · 错误结果：-12000 + 4955 = -7045 tc → **17.1 毫秒**
       衰减快了约 1000 倍 —— 钢琴按下 18 毫秒后就完全没声音了。

       而且它对**所有**时间类 generator 都成立（attack / hold / decay /
       release），只是 attack 和 release 恰好被钳制到 1ms / 5ms 下限，
       听感上不明显，真正致命的是 decay。

       是 tools/verify_sampler_host.py 的引擎测试抓出来的：
       它用真实音色库跑"音高 / 循环 / 踏板"这些检查，全部报静音。

       → 判定"这一层写没写"必须用 `Zone.has()`，不能用 `get()`。
    """
    def tc(p, i, name):
        num = D.GEN[name]
        hp, hi = p.has(name), i.has(name)
        if not hp and not hi:
            return D.GEN_DEFAULTS.get(num, -12000)
        return (p.gens[num].amount if hp else 0) + (i.gens[num].amount if hi else 0)

    def cb(p, i, name):
        # 同上：只有真的写了的那一层才参与相加（厘贝类默认值是 0，但要显式表达）
        num = D.GEN[name]
        hp, hi = p.has(name), i.has(name)
        return (p.gens[num].amount if hp else 0) + (i.gens[num].amount if hi else 0)

    # --- 音量包络 ---
    att_ms = D.tc2ms(tc(pz, iz, "attackVolEnv"))
    dec_ms = D.tc2ms(tc(pz, iz, "decayVolEnv"))
    rel_ms = D.tc2ms(tc(pz, iz, "releaseVolEnv"))
    sus_cb = cb(pz, iz, "sustainVolEnv")

    # --- 按键对包络的缩放（keynumToVolEnvDecay，单位 timecents/key）---
    # 规范化：音符越高衰减越快（钢琴就是这样）。这里只做温和处理。
    # 同上：用 has() 而不是 get()（get 会把缺失当成默认值 0 —— 这里恰好无害，
    # 但保持和 tc/cb 一致的写法，免得以后改成非零默认值时踩坑）
    _kd = D.GEN["keynumToVolEnvDecay"]
    kdec = ((pz.gens[_kd].amount if pz.has("keynumToVolEnvDecay") else 0)
            + (iz.gens[_kd].amount if iz.has("keynumToVolEnvDecay") else 0))
    if kdec:
        # 相对中央 C 的偏移量
        dec_ms *= 2.0 ** ((key - 60) * kdec / 1200.0)

    # --- 钳制到合理范围 ---
    # 下限：太短的 attack 会有咔哒，用 1ms；release 用 5ms
    att_ms = max(1.0, min(att_ms, 10000.0))
    dec_ms = max(1.0, min(dec_ms, 20000.0))
    rel_ms = max(5.0, min(rel_ms, 20000.0))

    # sustain：SF2 是"衰减多少 centibels"，转成 Q15 的**保留**电平
    sus_q15 = D.cb2q15(max(0.0, sus_cb))
    # 若 sustain 电平接近 0，说明这个音色是"打击型"，衰减到静音
    sustain_is_zero = sus_cb >= 1000

    # --- 增益 ---
    atten_cb = cb(pz, iz, "initialAttenuation")
    gain_db100 = -int(round(atten_cb * 10))   # 1 centibel = 0.1 dB

    return int(round(att_ms)), int(round(dec_ms)), sus_q15, int(round(rel_ms)), gain_db100


def _pan(pz: Zone, iz: Zone) -> int:
    """声像 -500..500（-500=全左）"""
    _pn = D.GEN["pan"]
    p = ((pz.gens[_pn].amount if pz.has("pan") else 0)
         + (iz.gens[_pn].amount if iz.has("pan") else 0))
    return max(-500, min(500, p))


# ==========================================================================
# 顶层：preset → PlannedZone 列表
# ==========================================================================

def _iter_preset_samples(sf: SoundFont, preset: Preset, use_loop: bool,
                         max_zones: int):
    """展开一个 preset 的所有 (preset zone, instrument zone) 组合"""
    out = []
    for pz in preset.zones:
        if pz.instrument_idx is None:
            continue
        if not (0 <= pz.instrument_idx < len(sf.instruments)):
            continue
        inst = sf.instruments[pz.instrument_idx]
        for iz in inst.zones:
            if iz.gens.get(D.GEN["sampleID"]) is None:
                continue

            # --- 范围取交集 ---
            klo = max(pz.key_lo, iz.key_lo)
            khi = min(pz.key_hi, iz.key_hi)
            vlo = max(pz.vel_lo, iz.vel_lo)
            vhi = min(pz.vel_hi, iz.vel_hi)
            if klo > khi or vlo > vhi:
                continue

            # keynum/velocity 固定值时收窄为单点
            kn = iz.get("keynum", -1)
            if kn is not None and kn >= 0:
                klo = khi = kn
            vn = iz.get("velocity", -1)
            if vn is not None and vn >= 0:
                vlo = vhi = vn

            smp = _make_sample(sf, iz, use_loop)
            if smp is None:
                continue

            a, d, s, r, g = _merge_envelope(pz, iz, (klo + khi) // 2)
            mode = iz.get("sampleModes", 0) or 0
            # 打击乐判定：sustain 衰减到静音，或采样本身不循环
            percussive = (s == 0) or (not smp.looped)

            out.append(PlannedZone(
                key_lo=klo, key_hi=khi, vel_lo=vlo, vel_hi=vhi,
                sample=smp, attack_ms=a, decay_ms=d, sustain_q15=s,
                release_ms=r, gain_db100=g, percussive=percussive,
                program=preset.program, bank=preset.bank, part=preset.name,
            ))
            if len(out) >= max_zones:
                return out
    return out


# GM 打击乐通道固定是 channel 10，对应 SF2 的 bank 128
DRUM_BANK = 128


def plan(sf: SoundFont, use_loop: bool = True,
         melodic_programs: Optional[set] = None,
         drum_programs: Optional[set] = None,
         max_zones_per_preset: int = 32) -> Tuple[List[PlannedZone], List[PlannedZone]]:
    """返回 (melodic, drum) 两组 PlannedZone。

    melodic: bank 0，program 0..127
    drum:    bank 128，通常只有 program 0 是标准鼓组
    """
    melodic: List[PlannedZone] = []
    drum: List[PlannedZone] = []

    for preset in sf.presets:
        is_drum = (preset.bank == DRUM_BANK) or (preset.bank == 128)
        if is_drum:
            if drum_programs is not None and preset.program not in drum_programs:
                continue
        else:
            if preset.bank != 0:
                continue
            if melodic_programs is not None and preset.program not in melodic_programs:
                continue

        zones = _iter_preset_samples(sf, preset, use_loop, max_zones_per_preset)
        (drum if is_drum else melodic).extend(zones)

    return melodic, drum


def summarize(zones: List[PlannedZone]) -> Dict[str, int]:
    """统计信息，用于命令行输出"""
    total_frames = sum(len(z.sample.data) for z in zones)
    return {
        "zones": len(zones),
        "unique_samples": len({id(z.sample.data) for z in zones}),
        "total_frames": total_frames,
        "pcm_bytes": total_frames * 2,
        "looped": sum(1 for z in zones if z.sample.looped),
        "programs": len({(z.bank, z.program) for z in zones}),
    }
