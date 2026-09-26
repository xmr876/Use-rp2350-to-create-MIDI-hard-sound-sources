"""
gmss/layout.py — 单片 Flash 布局与镜像输出

★ v3：最终方案是 **Pico 2 模块，板载 Flash 魔改成一片 16MB（W25Q128JVSIQ）**，
     音色库和固件同住这一片，**没有第二片芯片**。

     演进过三版，每版放弃的原因都值得记：
       v1  裸片 RP2350A + 4 片 QSPI Flash —— CS0/CS1 硬件片选 + CS2/CS3
           软件片选 + 采样交错分布；更麻烦的是 QMI Direct 模式要求
           "期间不能执行 flash 里的代码"，所有相关函数得塞 RAM。
       v2  Pico 2 + 1 片 32MB Flash 挂 SPI —— 但 Pico 2 的 QSPI 引脚
           **没有引到排针**（RP2350 的 QSPI 是专用引脚），载板碰不到 QMI；
           而 32MB 又超出 RP2350 的 CS0 默认窗口，用不上 XIP。
       v3  魔改 16MB（本版）—— 音色库直接内存映射，读采样点就是读指针。

     为什么上限恰好是 16MB（这是硬约束，不是省成本）：
       RP2350 的 OTP 字段 FLASH_DEVINFO.CS0_SIZE 决定 XIP 窗口大小，
       而没烧过 OTP 时 **默认值就是 12（16 MiB）**。
       16MB 正好等于这个默认值 → 不用烧 OTP、UF2 也能写满全片。
       32MB 就得烧 OTP（该枚举只列到 12），风险大得多。

产出 1 个二进制文件：

    library.bin   库镜像，**文件偏移 0 对应 flash 地址 GMSS_LIB_BASE** (0x40000)

    +0x000000 (库内)  gmss_header_t        (64 B)
    +0x000040         gmss_zone_t[]        (56 B × N)
    +...              gmss_instrument_t[]  (32 B × 128)
    +...              鼓组音色表            (32 B × D)
    +...              填充到 4KB 对齐
    +pool_off         采样池                每条采样 4KB 对齐

另外产出 library.uf2（可直接拖进 RP2350 盘，目标地址 0x10040000）
和 library_map.txt（池地址/校验和/大小，烧录时核对用）。

★ 三套坐标系，别搞混（这是这个模块最容易出错的地方）：
    1. zone.sample_off   —— **flash 绝对偏移**（含 LIB_BASE）
    2. zone.keyframe_off —— **相对 sample_off** 的偏移
    3. header.pool_off   —— **库内相对**偏移（相对 LIB_BASE）
  而 place(capacity=...) / write_images(capacity=...) 的 capacity
  也是**库内相对**量（从 LIB_BASE 起算）。
  player.py 里的 GmssImage 会把 sample_off 减掉 LIB_BASE 再索引文件。
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import numpy as np

from . import adpcm, sf2defs as D
from .plan import LOOP_NONE, PlannedZone

# 与 gmss_format.h 保持一致
GMSS_MAGIC = 0x53534D47          # "GMSS"
GMSS_CHIP_MAGIC = 0x43534D47     # "GMSC"（旧多片方案的片头魔数，单片不再用）
CHIP_HDR_SIZE = 16
POOL_OFFSET = 0x1000
POOL_ALIGN = 4096
ZONE_SIZE = 56       # ★ 由 tools/check_struct_sizes.py 用 ARM 编译器实测 sizeof 核对
INSTR_SIZE = 32
HEADER_SIZE = 64

CODEC_PCM16 = 0
CODEC_PCM8 = 1
CODEC_ADPCM4 = 2

ZF_LOOPED = 0x0001
ZF_PERCUSSIVE = 0x0002

# ★ 库在整片 flash 里的起始地址（与 gmss_format.h 的 GMSS_LIB_BASE 一致）
#   固件区预留 512KB，所以库从 0x40000 开始足够宽松（当前固件只有 14KB）。
LIB_BASE = 0x40000

# ★★ 整片容量：**16MB**（Pico 2 魔改成 W25Q128JVSIQ）
#
#   为什么是 16MB 而不是 32MB —— 这是硬上限，不是省成本：
#     RP2350 的 OTP 字段 FLASH_DEVINFO.CS0_SIZE 决定 XIP 窗口大小，
#     而 SDK 文档写明：
#       "When BOOT_FLAGS0_FLASH_DEVINFO_ENABLE is not set,
#        a default of 12 (16 MiB) is used."
#     即没烧过 OTP 的 RP2350，bootrom 默认就认为 CS0 上有 16MiB。
#     魔改成 16MB **正好等于这个默认值**，不用烧 OTP、UF2 也能写满全片。
#     32MB 会超出窗口（CS0_SIZE 的枚举值只到 12=16MB），得烧 OTP 才行。
CHIP_CAPACITY = 16 * 1024 * 1024
LIB_CAPACITY = CHIP_CAPACITY - LIB_BASE      # 库可用空间 ≈ 15.75MB

# 固件区预留（生成镜像时的自检用：库不能压到固件头上）
FW_RESERVE = 0x80000

# XIP 基址：flash 地址 0 → 0x10000000
XIP_BASE = 0x10000000

# 与 gmss_format.h 的 GMSS_MAX_CHIPS 一致（单片方案 = 1）
GMSS_MAX_CHIPS = 1

# UF2 的 family id。
# ★ 这张表是从 pico-sdk 的 src/common/boot_uf2_headers/include/boot/uf2.h
#   抄来的，不是猜的。曾经把 RP2350_ARM_S 写成 0xE48BFF57 ——
#   那其实是 **ABSOLUTE_FAMILY_ID**，用它烧进去 bootrom 会拒绝该 UF2
#   （表现为拖进去没反应、盘符重新挂载但固件没变）。
UF2_FAMILY_CYW43       = 0xE48BFF55
UF2_FAMILY_RP2040      = 0xE48BFF56
UF2_FAMILY_ABSOLUTE    = 0xE48BFF57
UF2_FAMILY_DATA        = 0xE48BFF58
UF2_FAMILY_RP2350_ARM_S = 0xE48BFF59   # ★ Pico 2 默认（Arm 安全模式）
UF2_FAMILY_RP2350_RISCV = 0xE48BFF5A
UF2_FAMILY_RP2350_ARM_NS = 0xE48BFF5B
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000

# XIP_BASE 已在上面跟着容量一起定义，这里不重复


@dataclass
class PlacedZone:
    """已确定物理位置的 zone

    ★ sample_off 是**整片 flash 的绝对字节地址**（含 LIB_BASE），
      不再是"片内偏移 + chip_id"。固件读采样时直接把这个值丢给
      flash_direct_read()，不需要任何换算。
    """
    z: PlannedZone
    chip_id: int             # 恒为 0（保留字段，与 C 结构体对齐）
    sample_off: int          # ★ flash 绝对地址
    blob: bytes              # **只含采样数据**（PCM 段 ++ ADPCM 段），不含关键帧表
    sample_len: int          # 解码后帧数
    loop_start: int
    loop_end: int
    codec: int
    attack_samples: int
    keyframes: List[adpcm.AdpcmState]
    alloc_bytes: int = 0     # 本条实际占用的字节（含对齐填充）

    @property
    def data_bytes(self) -> int:
        """采样数据部分的字节数（= len(blob)）

        ★ blob 里**不含**关键帧表 —— 关键帧表在 alloc 区内紧跟其后，
          由 write_images() 拼接。这个区分很关键：
          keyframe_off 是相对 sample_off 的偏移，必须等于 data_bytes。
          曾经把关键帧表塞进 blob 再用 len(blob) 当偏移，
          结果偏移多了整个表的长度，固件会读到错误位置。
        """
        return len(self.blob)

    @property
    def keyframe_bytes(self) -> int:
        return len(self.keyframes) * 4

    @property
    def total_bytes(self) -> int:
        """数据 + 关键帧表的总字节数（不含对齐填充）"""
        return self.data_bytes + self.keyframe_bytes



# ==========================================================================
# 编码策略
# ==========================================================================

def choose_attack_samples(sample_len: int, loop_start: int) -> int:
    """决定前多少个采样点用 PCM16 无损编码。

    目标：覆盖起音瞬态（对音色辨识最关键的部分），但不超过循环点
    （循环段必须全程 ADPCM 才能省空间，而且循环段本来就不含瞬态）。
    """
    if sample_len <= 0:
        return 0
    # 10ms @48k = 480 点
    target = 480
    if loop_start != LOOP_NONE and loop_start > 0:
        # 留出 1/4 循环段给 ADPCM，别把整个循环段都做成 PCM
        target = min(target, max(0, loop_start))
    return max(0, min(target, sample_len))


def encode_zone(z: PlannedZone, force_pcm16: bool = False
                ) -> Tuple[bytes, int, int, int, int, int, List]:
    """把 PlannedZone 编码成 blob。

    返回 (blob, sample_len, loop_start, loop_end, codec, attack_samples, keyframes)

    force_pcm16=True 时整段用无损 PCM16（体积约 2 倍，仅供调试对比音质用）。
    """
    data = z.sample.data
    n = len(data)
    ls = z.sample.loop_start
    le = z.sample.loop_end

    if force_pcm16:
        blob = data.astype("<i2").tobytes()
        return blob, n, ls, le, CODEC_PCM16, n, []

    attack = choose_attack_samples(n, ls if ls != LOOP_NONE else -1)

    if attack >= n:
        # 整段 PCM16（很短的采样，比如打击乐）
        blob = data.astype("<i2").tobytes()
        return blob, n, ls, le, CODEC_PCM16, n, []

    blob, kf, got_attack = adpcm.encode_hybrid(data, attack)
    return blob, n, ls, le, CODEC_ADPCM4, got_attack, kf


# ==========================================================================
# 布局
# ==========================================================================

def _sample_key(z: PlannedZone) -> str:
    """采样去重键，直接转给 Sample.key()（内容指纹 + 循环点 + 音高参数）"""
    return z.sample.key()


class LayoutError(RuntimeError):
    pass


def compute_pool_off(zones: Sequence[PlannedZone]) -> int:
    """算出采样池的**绝对 flash 地址**（4KB 对齐）。

    表区大小只取决于 zone 条数和鼓组数，与采样放哪里无关，
    所以可以先算池地址、再放采样 —— 这样 sample_off 一次就能定成绝对值，
    不需要"先放再平移"的两遍逻辑。

    ★ place() 和 write_images() 都调用本函数。两处算出不同的值会立刻
      在这里被断言抓到，而不是变成固件读到垃圾数据。
    """
    groups = {(z.bank, z.program) for z in zones}
    drumkits = sum(1 for (b, p) in groups if b == 128)
    melodic = sum(1 for (b, p) in groups if b != 128)
    if melodic > 128:
        raise LayoutError(
            f"旋律音色数 {melodic} > 128 个 GM 槽位，无法索引")
    # 头 + zone 表 + 旋律表(补齐到 128) + 鼓组表
    table = HEADER_SIZE + ZONE_SIZE * len(zones) + INSTR_SIZE * (128 + drumkits)
    return LIB_BASE + _align_up(table, POOL_ALIGN)


def place(zones: Sequence[PlannedZone],
          capacity: int = LIB_CAPACITY,
          pool_base: int | None = None,
          verbose: bool = True,
          force_pcm16: bool = False) -> List[PlacedZone]:
    """编码所有 zone 并顺序排进单片 flash 的采样池。

    ★ 排列策略：**按访问局部性紧凑排列**，不再交错。
      同一乐器的多个力度层相邻（换力度层时地址跳变小），
      每条 4KB 对齐（QSPI 突发读不跨页）。
      去重：内容完全相同的采样只编码一次，多个 zone 共享同一块数据。

    ★★ `capacity` 的语义（这里改过名字，原来的名字是个陷阱）：
       它是**库内可用空间**，单位是"从 LIB_BASE 起算的字节数"，
       池的硬上限 = `LIB_BASE + capacity`。

       原名 `chip_capacity`，听起来像"整片 flash 的容量"。
       于是最自然的写法 `place(z, chip_capacity=CHIP_CAPACITY)`
       会把上限抬到 `0x40000 + 0x1000000 = 0x1040000` ——
       **超出 16MB 芯片末尾 256KB**，而且不会报错，
       直到烧录时才发现镜像尾部越界。这是纯 API 命名造成的静默越界。
       改名成 `capacity` 之后与 `write_images(capacity=...)` 一致，
       并且下面加了硬上界断言，传错值会立刻炸而不是悄悄放过。
    """
    # ★ 硬上界：不管调用方传什么，都不许越过整片 flash 的末尾。
    if capacity > LIB_CAPACITY:
        raise LayoutError(
            f"capacity={capacity:,} 字节超出库可用空间 {LIB_CAPACITY:,} 字节。\n"
            f"  注意 capacity 是**从 LIB_BASE(0x{LIB_BASE:X}) 起算的库内空间**，"
            f"不是整片容量。\n"
            f"  整片是 {CHIP_CAPACITY:,} 字节，库从 0x{LIB_BASE:X} 开始，"
            f"所以库最多只能用 {LIB_CAPACITY:,} 字节。\n"
            f"  如果你传的是 CHIP_CAPACITY，改成 LIB_CAPACITY（或直接省略用默认值）。")
    if capacity < 0:
        raise LayoutError(f"capacity 不能为负：{capacity}")

    if pool_base is None:
        pool_base = compute_pool_off(zones)

    cursor = pool_base
    limit = LIB_BASE + capacity
    placed: List[PlacedZone] = []
    dedup: Dict[str, Tuple[bytes, int, int, int, int, int, List]] = {}

    # 预编码 + 去重（编码是 CPU 大头，只做一次）
    encoded: List[Tuple[bytes, int, int, int, int, int, List]] = []
    for z in zones:
        key = _sample_key(z)
        if key in dedup:
            encoded.append(dedup[key])
            continue
        blob, n, ls, le, codec, attack, kf = encode_zone(z, force_pcm16=force_pcm16)
        # ★ blob 只放采样数据；关键帧表单独保存，写入镜像时才拼接。
        #   这样 len(blob) 就是 keyframe_off，语义清晰不会错位。
        rec = (blob, n, ls, le, codec, attack, kf)
        dedup[key] = rec
        encoded.append(rec)

    if verbose:
        total_encoded = sum(len(b) + len(k) * 4 for (b, _, _, _, _, _, k) in dedup.values())
        print(f"  去重后唯一采样 {len(dedup)} 个，编码后共 {total_encoded / 1e6:.2f} MB")

    # 逐条分配；同一采样共享同一地址
    key_off: Dict[str, int] = {}

    for i, z in enumerate(zones):
        key = _sample_key(z)
        blob, n, ls, le, codec, attack, kf = encoded[i]
        # ★ 总占用 = 采样数据 + 关键帧表。两者一起参与分配和对齐，
        #   因为关键帧表紧跟采样数据（keyframe_off 相对 sample_off 算）。
        total = len(blob) + len(kf) * 4

        if key in key_off:
            off = key_off[key]
        else:
            if cursor + total > limit:
                # ★ 错误信息里**统一用库内相对量**（MB），并附上绝对地址。
                #   原来混着写"绝对 0x… 上限"和"相对 MB 已用"，
                #   两者单位不同，看的人得自己减 LIB_BASE 才知道离满还有多远。
                used_rel = cursor - LIB_BASE
                cap_rel = capacity
                raise LayoutError(
                    f"采样池装不下这一条：需要 {total:,} 字节，"
                    f"当前已用 {used_rel / 1e6:.2f} MB / {cap_rel / 1e6:.2f} MB，"
                    f"还差 {(cursor + total - limit) / 1e6:.3f} MB。\n"
                    f"  （库内偏移 0x{used_rel:X}，池上限 0x{limit - LIB_BASE:X}，"
                    f"flash 绝对地址 0x{cursor:X} / 0x{limit:X}）\n"
                    f"  请减少音色库内容：删音色、减力度层、或把采样率降到 32kHz。")
            off = cursor
            # 4KB 对齐：满足 QSPI 突发读不跨页，也有余量容纳关键帧表
            cursor += _align_up(total, POOL_ALIGN)
            key_off[key] = off

        placed.append(PlacedZone(
            z=z, chip_id=0, sample_off=off, blob=blob,
            sample_len=n, loop_start=ls, loop_end=le,
            codec=codec, attack_samples=attack, keyframes=kf,
            alloc_bytes=_align_up(total, POOL_ALIGN),
        ))

    if verbose:
        # ★ 这里的 used 要**从 LIB_BASE 起算**（= cursor - LIB_BASE），
        #   才能和 capacity 同口径。原来写的是 cursor - pool_base，
        #   那只算了池内用量，没算表区，于是"已用"和"容量"不同基准，
        #   百分比会偏小 —— 打印出来的余量比实际乐观。
        used = cursor - LIB_BASE
        print(f"    采样池区: {used / 1e6:6.2f} MB / {capacity / 1e6:.2f} MB "
              f"({100.0 * used / capacity:5.1f}%)  "
              f"结束偏移 0x{cursor - LIB_BASE:X}（flash 绝对 0x{cursor:X}）")

    return placed


def _align_up(v: int, a: int) -> int:
    return (v + a - 1) // a * a



# ==========================================================================
# 二进制打包
# ==========================================================================

def pack_zone(pz: PlacedZone) -> bytes:
    """按 gmss_format.h 的 gmss_zone_t 逐字段打包，共 56 字节。

    字段顺序必须与 C 结构体**完全一致**，漏一个字段整个表就错位，
    而且是静默错位（音频全乱但不报错）。所以末尾有长度断言兜底。

    offset  字段                                     C 类型              累计
      0     key_lo / key_hi / vel_lo / vel_hi        uint8 ×4              4
      4     chip_id / codec / flags                  uint8,uint8,uint16    8
      8     sample_off                               uint32               12
     12     sample_len                               uint32               16
     16     loop_start                               uint32               20
     20     loop_end                                 uint32               24
     24     attack_samples                           uint32               28
     28     keyframe_off                             uint32 ← 曾漏掉这个   32
     32     keyframe_count                           uint32               36
     36     root_key / tune_cents / gain_db100       uint8,int8,int16     40
     40     env_attack/decay/sustain/release         uint16×3,int16       48
     48     loop_xfade / pad0                        int16,uint16         52
     52     reserved0                                uint32               56

    ★★ 这张表以前写"共 58 字节"，而 C 结构体实际是 **56**。
       差的 2 字节是 <Bbhh> 里多写的一个 int16 填充，
       而 C 那边 root_key+tune_cents+gain_db100 只有 4 字节（1+1+2）。

       后果：**zone 表每一条都错位 2 字节**，固件读到的
       key_lo/vel_hi/sample_off 全是垃圾 —— 不崩溃不报错，
       只是音频完全乱掉。而它躲过了全部 38 个单测，
       因为那些测试都是 Python↔Python 自洽检查，两边用同一个错常量，
       自然对得上。**没有任何一处问过 C 编译器 sizeof 是多少。**

       现在有两重保险，改坏了会立刻发现：
         · firmware/include/gmss_format.h 末尾的 _Static_assert
         · tools/check_struct_sizes.py（用 ARM 编译器实测 sizeof 对拍）

    ★ 所以：**不要手算这张表**。改结构体之后跑 check_struct_sizes.py，
      让它告诉你真实布局，再回来改这里的格式串。
    """
    z = pz.z
    flags = 0
    if z.sample.looped:
        flags |= ZF_LOOPED
    if z.percussive:
        flags |= ZF_PERCUSSIVE

    loop_start = z.sample.loop_start if z.sample.looped else LOOP_NONE
    loop_end = z.sample.loop_end if z.sample.looped else 0

    # 关键帧表位置：相对 sample_off 的偏移。
    # ★ 必须等于"采样数据部分的长度"（不含关键帧表自身）。
    #   曾经把表塞进 blob 再用 len(blob) 当偏移，导致偏移多了整张表的长度，
    #   固件会去错误位置读关键帧 → 循环点定位全错 → 每次回跳都咔哒。
    kf_off = pz.data_bytes if pz.keyframes else 0xFFFFFFFF
    kf_count = len(pz.keyframes)

    out = bytearray()
    out += struct.pack("<BBBB", z.key_lo, z.key_hi, z.vel_lo, z.vel_hi)   # 0
    out += struct.pack("<BBH", pz.chip_id, pz.codec, flags)               # 4
    out += struct.pack("<I", pz.sample_off)                               # 8
    out += struct.pack("<I", pz.sample_len)                               # 12
    out += struct.pack("<I", loop_start)                                  # 16
    out += struct.pack("<I", loop_end)                                    # 20
    out += struct.pack("<I", pz.attack_samples)                           # 24
    out += struct.pack("<I", kf_off)                                      # 28
    out += struct.pack("<I", kf_count)                                    # 32
    out += struct.pack("<Bbh", z.sample.root_key,                        # 36
                       z.sample.tune_cents,
                       max(-32768, min(32767, z.gain_db100)))
    out += struct.pack("<HHhH", z.attack_ms, z.decay_ms,                  # 40
                       max(0, min(32767, z.sustain_q15)), z.release_ms)
    out += struct.pack("<hH", 0, 0)                                       # 48
    out += struct.pack("<I", 0)                                           # 52

    assert len(out) == ZONE_SIZE, \
        f"zone 打包长度 {len(out)} != {ZONE_SIZE}，字段顺序或类型与 gmss_format.h 不符"
    return bytes(out)


def pack_instrument(program: int, bank: int, first: int, count: int, name: str) -> bytes:
    nb = name.encode("latin-1", errors="replace")[:15]
    nb += b"\x00" * (16 - len(nb))
    out = bytearray()
    out += struct.pack("<HH", first, count)
    out += struct.pack("<BBbb", bank, program, -1, 0)
    out += nb
    out += struct.pack("<II", 0, 0)
    assert len(out) == INSTR_SIZE, f"instrument 打包长度 {len(out)} != {INSTR_SIZE}"
    return bytes(out)


def pack_header(total_zones: int, instrument_count: int, drumkit_count: int,
                zone_off: int, instr_off: int, pool_off: int,
                chip_count: int, total_bytes: int, checksum: int,
                build_name: str) -> bytes:
    """按 gmss_format.h 的 gmss_header_t 打包，共 64 字节。

    偏移: 0 magic / 4 ver / 6 hdr_size / 8 rate / 12 zones / 16 counts
          20 zone_off / 24 instr_off / 28 pool_off / 32 chip+flags+rsv
          36 total_bytes / 40 checksum / 44 reserved1 / 48 build_name[16]

    曾经多写了一个 4 字节保留字段变成 68 字节 —— 末尾的长度断言就是为了抓这个。
    """
    nb = build_name.encode("ascii", errors="replace")[:15]
    nb += b"\x00" * (16 - len(nb))
    out = bytearray()
    out += struct.pack("<I", GMSS_MAGIC)                     # 0
    out += struct.pack("<BBH", 1, 0, HEADER_SIZE)            # 4
    out += struct.pack("<II", 48000, total_zones)            # 8
    out += struct.pack("<HH", instrument_count, drumkit_count)  # 16
    out += struct.pack("<III", zone_off, instr_off, pool_off)   # 20
    out += struct.pack("<BBH", chip_count, 0x01, 0)          # 32
    out += struct.pack("<II", total_bytes, checksum)         # 36
    out += struct.pack("<I", 0)                              # 44 reserved1
    out += nb                                                # 48
    assert len(out) == HEADER_SIZE, \
        f"header 打包长度 {len(out)} != {HEADER_SIZE}，字段与 gmss_format.h 不符"
    return bytes(out)


def pack_chip_header(chip_id: int, zone_count: int, pool_bytes: int) -> bytes:
    out = struct.pack("<IBBHI", GMSS_CHIP_MAGIC, chip_id, 1, zone_count, 0)
    out += struct.pack("<I", pool_bytes)
    assert len(out) == CHIP_HDR_SIZE
    return bytes(out)


def fnv1a(data: bytes) -> int:
    """FNV-1a 32bit 校验和。

    ★ 为什么没有"快速版"——这里连续踩了三次坑，结论值得记下来：

    FNV-1a 的迭代是 `h = (h ^ b) * P`，**每一步都截断到 32 位**。
    截断破坏了乘法的分配律，所以下面这些"向量化"思路**全部不成立**：

        (h ^ b) * P  与  (h*P) ^ (b*P)     ← 在完整整数域相等，
                                            但各自截断到 32 位后**不等**
        多项式展开 Σ b_i * P^(n-i)          ← 混了加法与 XOR，错
        XOR 分解（各字节独立项）             ← 后续字节项仍含前序状态，错

    实测反例（h0=0x811C9DC5, b0=0x8b）：
        (h0^b0)*P   = 0x8e0ba1ca   ← 正确
        (h0*P)^(b0*P) = 0x8e0c87ce ← 错

    **FNV-1a 本质上是串行的，无法向量化。**

    那为什么不需要快？校验和只覆盖 zone 表 + 音色表（约 100KB 量级），
    **不覆盖 48MB 采样池**。100KB 逐字节 Python 循环只要 ~35ms。
    固件端用 C 实现同样的逐字节循环，100KB 不到 1ms。
    """
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


# 保留别名，避免调用方改动。它就是对拍用的同一个实现。
fnv1a_fast = fnv1a


# ==========================================================================
# 镜像输出
# ==========================================================================

def write_images(placed: Sequence[PlacedZone], out_dir: Path,
                 build_name: str = "GMSS",
                 capacity: int = LIB_CAPACITY,
                 verbose: bool = True) -> Dict[str, object]:
    """生成 library.bin（文件偏移 0 == flash 地址 LIB_BASE）。

    同目录另写一份 library_map.txt，记录池地址/校验和/大小，
    方便烧录时核对（烧到错误地址的库不会报错，只会静音 —— 必须有据可查）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 音色表：按 (bank, program) 分组 ----------
    groups: Dict[Tuple[int, int], List[int]] = {}
    for idx, pz in enumerate(placed):
        groups.setdefault((pz.z.bank, pz.z.program), []).append(idx)

    melodic = sorted((k, v) for k, v in groups.items() if k[0] != 128)
    drums = sorted((k, v) for k, v in groups.items() if k[0] == 128)

    # zone 表按"音色分组连续"重排，让固件能顺序读
    order: List[int] = []
    for _, idxs in melodic + drums:
        order.extend(idxs)
    # （旧的 remap 变量没人用，删掉；重排后下标就是新顺序）

    zone_table = b"".join(pack_zone(placed[old]) for old in order)

    # ---------- 音色表条目 ----------
    # 旋律槽固定 128 个（program 0..127 直接索引），缺失的补空条目。
    instr_entries: List[bytes] = []
    cursor = 0
    for (bank, prog), idxs in melodic:
        name = placed[idxs[0]].z.part
        instr_entries.append(pack_instrument(prog, 0, cursor, len(idxs), name))
        cursor += len(idxs)
    melodic_count = len(instr_entries)
    # ★ 补齐到 128 个槽。不能写成"遍历 128 个 program 再看列表里有没有"——
    #   那样每轮都会看到自己刚追加的填充项，条件恒真，会追加 128 次，
    #   音色表膨胀成 250 条（曾经就是这样）。
    #   正确的补数是 128 - 已有条数。
    for prog in range(melodic_count, 128):
        instr_entries.append(pack_instrument(prog, 0, 0, 0, f"p{prog:03d}"))
    assert len(instr_entries) == 128, \
        f"旋律音色表应为 128 槽，实际 {len(instr_entries)}"
    instr_table = b"".join(instr_entries)

    drum_entries: List[bytes] = []
    for (bank, prog), idxs in drums:
        name = placed[idxs[0]].z.part
        drum_entries.append(pack_instrument(prog, 128, cursor, len(idxs), name))
        cursor += len(idxs)
    drum_table = b"".join(drum_entries)

    # ---------- 池地址 ----------
    # ★ 必须与 place() 算出的完全一致，否则 zone 里的 sample_off 全部错位。
    #   这里用同一份 zones 重算一遍并断言，属"便宜的自检换半夜不睡"。
    pool_off = compute_pool_off([pz.z for pz in placed])
    if placed:
        expect = min(pz.sample_off for pz in placed)
        if expect != pool_off:
            raise LayoutError(
                f"池地址不一致：place() 用了 0x{expect:X}，"
                f"write_images() 算出 0x{pool_off:X}。"
                f"通常是 zone 集合被改动过（增删 zone 会改变表区大小）。")

    # ---------- 头部 ----------
    zone_off = HEADER_SIZE
    instr_off = zone_off + len(zone_table)

    all_zones_bytes = zone_table + instr_table + drum_table
    checksum = fnv1a_fast(all_zones_bytes)

    total_bytes = (pool_off - LIB_BASE)
    for pz in placed:
        total_bytes = max(total_bytes, pz.sample_off - LIB_BASE + pz.total_bytes)

    header = pack_header(
        total_zones=len(placed),
        instrument_count=melodic_count,
        drumkit_count=len(drums),
        zone_off=zone_off, instr_off=instr_off,
        pool_off=pool_off - LIB_BASE,     # 头部里存的是**库内相对偏移**
        chip_count=1, total_bytes=total_bytes,
        checksum=checksum, build_name=build_name,
    )

    # ---------- 组装镜像 ----------
    img = bytearray()
    img += header
    img += zone_table
    img += instr_table
    img += drum_table
    if len(img) > pool_off - LIB_BASE:
        raise LayoutError(
            f"表区越界：表占 {len(img)} 字节，池从 {pool_off - LIB_BASE} 开始")
    img += b"\xFF" * (pool_off - LIB_BASE - len(img))

    # 采样池：按地址排序顺序写入（place() 本来就是顺序分配的，这里再排一次
    # 是为了防止将来改成非顺序分配时静默写错位置）
    written: Dict[int, bytes] = {}
    for pz in placed:
        if pz.sample_off in written:
            continue
        # 布局: [采样数据][关键帧表]；keyframe_off 相对 sample_off 算
        payload = pz.blob
        if pz.keyframes:
            payload += b"".join(s.to_bytes() for s in pz.keyframes)
        if len(payload) > pz.alloc_bytes:
            raise LayoutError(
                f"zone 数据超出分配区：off=0x{pz.sample_off:X} "
                f"需要 {len(payload)} 字节但只分配了 {pz.alloc_bytes}")
        if pz.sample_off < pool_off:
            raise LayoutError(
                f"zone 地址 0x{pz.sample_off:X} 落在表区内（池从 0x{pool_off:X} 开始）")
        written[pz.sample_off] = payload

    for off in sorted(written):
        rel = off - LIB_BASE
        if rel < len(img):
            raise LayoutError(f"偏移 0x{off:X} 与已写数据重叠")
        img += b"\xFF" * (rel - len(img))     # 擦除态填充
        img += written[off]

    if len(img) > capacity:
        raise LayoutError(f"库镜像 {len(img)} 字节超出容量 {capacity}")
    # 补齐到 256 字节边界（flash 页大小），方便按页烧录
    if len(img) % 256:
        img += b"\xFF" * (256 - len(img) % 256)

    (out_dir / "library.bin").write_bytes(bytes(img))

    # ---------- 地图/自检文件 ----------
    last_end = (max((off + len(b) for off, b in written.items()), default=pool_off))
    map_txt = "\n".join([
        f"# GMSS library map  (v2 single-chip)",
        f"build_name      = {build_name}",
        f"lib_base        = 0x{LIB_BASE:08X}   (flash byte offset)",
        f"xip_base        = 0x{XIP_BASE + LIB_BASE:08X}   (memory-mapped view)",
        f"uf2_target_addr = 0x{XIP_BASE + LIB_BASE:08X}",
        f"pool_off        = 0x{pool_off - LIB_BASE:08X}   (库内相对)",
        f"pool_addr       = 0x{pool_off:08X}   (flash 绝对)",
        f"pool_end        = 0x{last_end:08X}",
        f"total_zones     = {len(placed)}",
        f"melodic_progs   = {melodic_count}",
        f"drumkits        = {len(drums)}",
        f"unique_samples  = {len(written)}",
        f"image_bytes     = {len(img)}",
        f"image_end_addr  = 0x{LIB_BASE + len(img):08X}",
        f"checksum_fnv1a  = 0x{checksum:08X}",
        "",
    ])
    (out_dir / "library_map.txt").write_text(map_txt, encoding="utf-8")

    results = {"library.bin": len(img)}
    if verbose:
        for k, v in results.items():
            print(f"    {k:14s} {v / 1e6:7.2f} MB  → flash 0x{LIB_BASE:X} "
                  f"(XIP 0x{XIP_BASE + LIB_BASE:X})")

    return {
        "files": results,
        "lib_base": LIB_BASE,
        "pool_off": pool_off - LIB_BASE,
        "pool_addr": pool_off,
        "pool_end": last_end,
        "total_zones": len(placed),
        "melodic_programs": melodic_count,
        "drumkits": len(drums),
        "unique_samples": len(written),
        "checksum": checksum,
        "header": header,
        "map_text": map_txt,
        "table_bytes": all_zones_bytes,   # 校验和覆盖的原始字节，供自检用
    }


# ==========================================================================
# UF2 打包
# ==========================================================================

def make_uf2(data: bytes, target_addr: int,
             family: int = UF2_FAMILY_RP2350_ARM_S,
             payload: int = 256) -> bytes:
    """把一段二进制包成 UF2。

    UF2 块固定 512 字节：32 字节头 + 最多 476 字节数据 + 魔数尾巴。
    这里用 256 字节/块 —— 与 flash 页大小一致，也是 picotool 的默认，
    擦写对齐最省心（每块正好一页）。

    头字段布局（全部小端）：
        0  magicStart0   0x0A324655
        4  magicStart1   0x9E5D5157
        8  flags         0x00002000 = 带 familyID
       12  targetAddr    ★ 目标**地址**（不是偏移），XIP 视角
       16  payloadSize
       20  blockNo
       24  numBlocks
       28  fileSize / familyID
       32  data[476]
      508 magicEnd      0x0AB16F30
    """
    assert payload <= 476, "UF2 单块数据不能超过 476 字节"
    assert target_addr % payload == 0 or target_addr % 256 == 0, \
        "target_addr 应 256 字节对齐，否则每块都要跨页"
    nb = (len(data) + payload - 1) // payload
    out = bytearray()
    for i in range(nb):
        chunk = data[i * payload:(i + 1) * payload]
        chunk = chunk + b"\xFF" * (payload - len(chunk))
        blk = bytearray(512)
        struct.pack_into("<IIIIIIII", blk, 0,
                         UF2_MAGIC_START0, UF2_MAGIC_START1,
                         UF2_FLAG_FAMILY_ID, target_addr + i * payload,
                         payload, i, nb, family)
        blk[32:32 + payload] = chunk
        struct.pack_into("<I", blk, 508, UF2_MAGIC_END)
        out += blk
    return bytes(out)


def write_uf2(data: bytes, target_addr: int, path: Path,
              family: int = UF2_FAMILY_RP2350_ARM_S,
              payload: int = 256) -> int:
    uf = make_uf2(data, target_addr, family=family, payload=payload)
    path.write_bytes(uf)
    return len(uf)

