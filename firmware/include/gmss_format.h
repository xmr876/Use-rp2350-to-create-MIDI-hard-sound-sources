/*
 * gmss_format.h — GMSS (General MIDI Sample Set) 板载音色库二进制格式定义
 *
 * 这是 PC 端转换工具（tools/gmss 下的 Python 包）与 RP2350 固件共享的唯一契约。
 * 任何改动必须两边同步。
 *
 * 设计要点
 * --------
 * 1. 小端、无对齐填充（打包布局），所有多字节字段显式按字节读取。
 * 2. 全部为"扁平表 + 大块采样池"结构，固件只需顺序读表，随机读采样。
 * 3. 地址用 chip_id(2bit) + offset(24bit) 编码，天然覆盖 3 片 × 16MB。
 * 4. 采样按 zone 交错分布到各数据 Flash，让多复音能并行读取（见 §交错策略）。
 * 5. 支持混合编码：attack 段为 16bit PCM 保证起音瞬态，循环段用 IMA-ADPCM 省空间。
 *
 * 字节序：小端 (LE)
 * 打包：无 padding（用 __attribute__((packed)) 或逐字节解析）
 */
#ifndef GMSS_FORMAT_H
#define GMSS_FORMAT_H

#include <stdint.h>
#include <stddef.h>   /* offsetof —— 下面的 _Static_assert 要用 */

/* ------------------------------------------------------------------ */
/* 版本与规模上限                                                       */
/* ------------------------------------------------------------------ */

#define GMSS_MAGIC          0x53534D47u  /* "GMSS" 小端读作 u32 */
#define GMSS_VERSION_MAJOR  1
#define GMSS_VERSION_MINOR  0

/* ------------------------------------------------------------------ */
/* ★ 单片 Flash 布局（v3 定稿）                                         */
/* ------------------------------------------------------------------ */
/*
 * 硬件：**Pico 2 模块，板载 Flash 魔改成一片 16MB（W25Q128JVSIQ）**，
 *       音色库和固件同住这一片，全部走 QMI（CS0），**不需要第二片芯片**。
 *
 * 为什么最终是这个方案（演进过三版，每版的原因都值得记）：
 *
 *   v1  裸片 RP2350A + 4 片 QSPI Flash
 *       → 要 CS0/CS1 硬件片选 + CS2/CS3 软件片选，还要把采样交错分布；
 *         更麻烦的是 QMI Direct 模式有"期间不能执行 flash 里的代码"
 *         这类致命约束，代码必须全塞 RAM。
 *
 *   v2  Pico 2 模块 + 1 片 32MB Flash 挂 SPI
 *       → Pico 2 的 QSPI 引脚**没有引到排针**（RP2350 的 QSPI 是专用引脚，
 *         不像 RP2040 与 GPIO0~5 复用），载板根本碰不到 QMI 总线；
 *         而 32MB 又超出 RP2350 的 CS0 默认窗口，用不上 XIP，
 *         只能自己写 SPI 驱动 + 把采样 DMA 进 RAM。
 *
 *   v3  **Pico 2 模块 + 板载 Flash 换成 16MB**（本版）
 *       → 音色库直接**内存映射**：读采样点就是读指针，XIP 缓存自动处理。
 *         不需要 SPI/QMI 驱动、不需要 DMA 搬运、不需要 __not_in_flash_func、
 *         不受勘误 E14 影响。而且 UF2 能烧满整片，固件和音色库都拖拽即可。
 *
 * ★ 为什么 16MB 是"免费"的上限：
 *   RP2350 的 OTP 字段 FLASH_DEVINFO.CS0_SIZE 决定 XIP 窗口大小。
 *   SDK 的定义里写明：
 *     "When BOOT_FLAGS0_FLASH_DEVINFO_ENABLE is not set,
 *      a default of 12 (16 MiB) is used."
 *   也就是**没烧过 OTP 的 RP2350，bootrom 默认就认为 CS0 上有 16MiB**。
 *   改成 16MB 正好等于这个默认值 —— 不用烧 OTP，UF2 也能写满全片。
 *   32MB 就超出窗口了（该枚举只到 12=16MB），必须烧 OTP，风险大得多。
 *
 * 全片布局（16MB = 0x1000000）：
 *
 *   0x0000000 ┌──────────────────────────┐
 *             │ boot2 + 向量表 + 固件      │  ← UF2 目标 0x10000000
 *             │ （当前只有 14KB）          │
 *   0x0080000 ├──────────────────────────┤  ← 固件区预留上限 GMSS_FW_RESERVE
 *   0x0040000 ├──────────────────────────┤  ← GMSS_LIB_BASE（音色库起点）
 *             │ gmss_header_t    (64B)    │  ← UF2 目标 0x10040000
 *             │ zone 表   (58B × N)       │
 *             │ 音色表    (32B × 136)     │
 *             ├──────────────────────────┤
 *             │ 采样池                    │
 *   0x1000000 └──────────────────────────┘
 *
 * ★ 为什么库基址取 0x40000：
 *     1. 给固件留 256KB —— 当前固件 14KB，哪怕加满引擎也够
 *     2. 64KB 对齐，擦除方便
 *     3. 和固件区不重叠，所以两段可以**合并成一个 UF2 一次烧完**
 *
 * ★ 寻址：zone 里的 sample_off 是**全片绝对字节偏移**。
 *   固件访问时加上 XIP_BASE 就是可直接解引用的指针：
 *       const uint8_t *p = (const uint8_t *)(0x10000000u + z->sample_off);
 */

#define GMSS_MAX_CHIPS      1      /* 单片方案 */
#define GMSS_DATA_CHIPS     1
#define GMSS_CHIP_SIZE      (16u * 1024u * 1024u)   /* ★ 16MB */

/* 库在整片 Flash 里的起始偏移（见上面的布局图） */
#define GMSS_LIB_BASE       (0x40000u)
/* 固件区预留（升级固件时不要越界踩到库） */
#define GMSS_FW_RESERVE     (0x80000u)
/* XIP 基址：flash 偏移 + 这个 = 可直接解引用的地址 */
#define GMSS_XIP_BASE       (0x10000000u)
#define GMSS_LIB_XIP_ADDR   (GMSS_XIP_BASE + GMSS_LIB_BASE)

/* 采样池在库内的对齐（QSPI 突发读不跨页） */
#define GMSS_POOL_ALIGN     4096u

#define GMSS_MAX_INSTRUMENTS 128   /* GM 旋律音色数 */
#define GMSS_MAX_DRUMKITS    8     /* 鼓组数（通常只用 1~2） */
#define GMSS_MAX_ZONES_PER_INSTRUMENT 64
#define GMSS_MAX_ZONES_TOTAL 2048
#define GMSS_NAME_LEN        16    /* 音色名，ASCII，无终止符时补 0 */

/* 采样率：全库统一，固件不做重采样率转换（除音高移位） */
#define GMSS_SAMPLE_RATE     48000

/* ------------------------------------------------------------------ */
/* 编码类型                                                             */
/* ------------------------------------------------------------------ */

typedef enum {
    GMSS_CODEC_PCM16   = 0,  /* 16bit signed 单声道，小端 */
    GMSS_CODEC_PCM8    = 1,  /*  8bit signed 单声道（预留，未启用） */
    GMSS_CODEC_ADPCM4  = 2,  /* IMA/DVI ADPCM 4bit，块内连续 */
} gmss_codec_t;

/*
 * 混合编码：一个 zone 的数据布局是
 *
 *     [ PCM16 × attack_samples ] ++ [ ADPCM 全长度流 ]
 *
 * ★ 关键设计：**ADPCM 流覆盖整个采样缓冲区**，不是只覆盖尾部。
 *   前 attack_samples 个点的编码是冗余的（解码时被 PCM 替代），
 *   换来三个好处：
 *     1. 关键帧索引 == 采样点索引，无需偏移换算，解码器不会算错
 *     2. ADPCM 解码器在第 attack 点处处于正确状态（非从 0 重启），
 *        PCM→ADPCM 交界无跳变
 *     3. 循环点定位只有一套逻辑：查关键帧 → 顺序解码
 *   冗余代价 = attack_samples/2 字节/zone（典型 240B，全库约 0.5MB），可接受。
 *
 * 解码器取第 p 个采样点：
 *     p <  attack_samples  → 读 PCM：blob[p*2] 起的 int16
 *     p >= attack_samples  → 读 ADPCM：blob[pcm_bytes + p/2]，
 *                            高 nibble 还是低 nibble 由 p&1 决定
 *
 * 若 attack_samples == 0，则 blob 全是 ADPCM，退化为纯压缩编码。
 */

/* ------------------------------------------------------------------ */
/* 头部 (64 字节)                                                       */
/* ------------------------------------------------------------------ */

typedef struct __attribute__((packed)) {
    uint32_t magic;            /* 0x00 "GMSS"                       */
    uint8_t  version_major;    /* 0x04                              */
    uint8_t  version_minor;    /* 0x05                              */
    uint16_t header_size;      /* 0x06 本头长度，用于向后兼容 = 64   */

    uint32_t sample_rate;      /* 0x08 固定 48000                   */
    uint32_t total_zones;      /* 0x0C zone 总数                    */
    uint16_t instrument_count; /* 0x10 通常 128                     */
    uint16_t drumkit_count;    /* 0x12 通常 1~2                     */

    uint32_t zone_table_off;   /* 0x14 zone 表在库内的字节偏移       */
    uint32_t instr_table_off;  /* 0x18 音色表偏移（128+8 项）        */
    uint32_t pool_off;         /* 0x1C 采样池起始偏移（仅作校验用）  */

    uint8_t  chip_count;       /* 0x20 实际使用的数据片数 1~4        */
    uint8_t  flags;            /* 0x21 bit0: 已交错分布              */
    uint16_t reserved0;        /* 0x22                              */

    uint32_t total_bytes;      /* 0x24 整个库的字节数（不含每片头）  */
    uint32_t checksum;         /* 0x28 简单校验（FNV-1a over zone+pool）*/
    uint32_t reserved1;        /* 0x2C                              */

    uint8_t  build_name[16];   /* 0x30 工具写入的库名，ASCII        */
} gmss_header_t;               /* 共 64 字节 */

/* ------------------------------------------------------------------ */
/* 库在 Flash 内的物理布局（单片 32MB）                                  */
/* ------------------------------------------------------------------ */
/*
 *   0x0000000  ┌────────────────────────────┐
 *              │ 固件（.uf2 烧写区）          │  256KB
 *   0x0040000  ├────────────────────────────┤ ← GMSS_LIB_BASE
 *              │ gmss_header_t      64 B     │
 *              │ zone 表        56 B × N     │
 *              │ 音色表  32 B × (128+D)      │
 *              ├────────────────────────────┤ ← header.pool_off（4KB 对齐）
 *              │ 采样池                      │
 *   0x2000000  └────────────────────────────┘
 *
 * 池起点 4KB 对齐（GMSS_POOL_ALIGN），使 QSPI 突发读的地址低 12 位为 0，
 * 4KB 以内的突发不会跨页边界，固件缓冲逻辑可以简化。
 *
 * ★ 旧的多片方案里每条数据片自带一个 gmss_chip_header_t（魔数 "GMSC"）。
 *   单片后这个头失去意义（只有一片，没有"探测哪片已烧录"的需求），
 *   但结构体定义保留，PC 工具也不再生成它。
 */
#define GMSS_CHIP_HDR_SIZE   16
#define GMSS_POOL_OFFSET     0x1000u

typedef struct __attribute__((packed)) {
    uint32_t magic;         /* "GMSC" 魔数（单片方案不再使用，保留定义）*/
    uint8_t  chip_id;       /* 0 */
    uint8_t  format_major;
    uint16_t zone_count;
    uint32_t pool_bytes;
    uint32_t reserved;
} gmss_chip_header_t;       /* 共 16 字节 */

#define GMSS_CHIP_MAGIC  0x43534D47u  /* "GMSC" */

/* ------------------------------------------------------------------ */
/* 音色表条目 (32 字节) — 每个 GM 音色一条                            */
/* ------------------------------------------------------------------ */

typedef struct __attribute__((packed)) {
    uint16_t zone_first;   /* 该音色第一个 zone 在 zone 表中的下标   */
    uint16_t zone_count;
    uint8_t  bank;         /* 0 = 旋律, 128 = 鼓组                  */
    uint8_t  program;      /* 0..127                                */
    int8_t   fixed_note;   /* 鼓组：强制音高；-1 = 使用按键音高     */
    int8_t   pad;
    uint8_t  name[16];
    uint32_t reserved0;    /* 凑满 32 字节                          */
    uint32_t reserved1;
} gmss_instrument_t;

/* ------------------------------------------------------------------ */
/* Zone 条目 (56 字节) — 一个采样分区                                   */
/* ------------------------------------------------------------------ */
/*
 * ★ 字段偏移（packed，无填充），已逐字段累加核对：
 *      0  key_lo/key_hi/vel_lo/vel_hi        4
 *      4  chip_id/codec/flags                4
 *      8  sample_off                         4
 *     12  sample_len                         4
 *     16  loop_start                         4
 *     20  loop_end                           4
 *     24  attack_samples                     4
 *     28  keyframe_off                       4
 *     32  keyframe_count                     4
 *     36  root_key/tune_cents/gain_db100     4
 *     40  env_attack/decay/sustain/release   8
 *     48  loop_xfade/pad0                    4
 *     52  reserved0                          4
 *                                            --
 *                                            56
 *
 * PC 端 tools/gmss/gmss/layout.py 的 pack_zone() 按同一顺序打包，
 * 且带长度断言。两边长度不一致会立刻报错，不会静默错位。
 *
 * ★★ 这里曾经错过一次，所以下面加了 _Static_assert：
 *   最初文档和 PC 端都写"58 字节"，而 C 结构体实际是 **56**。
 *   差的 2 字节来自 PC 端格式串 <Bbhh> 多写了一个 int16 填充，
 *   而 C 这边 root_key/tune_cents/gain_db100 只有 4 字节（1+1+2）。
 *
 *   后果有多严重：zone 表**每一条都错位 2 字节**，固件读到的
 *   key_lo / vel_hi / sample_off 全是垃圾 —— 不是报错崩溃，
 *   而是"音频完全乱掉但程序照跑"，属于最难定位的一类缺陷。
 *   而且它躲过了所有单测：那些测试只做 Python↔Python 的自洽检查，
 *   没有任何一处真正问过 C 编译器 sizeof 是多少。
 *
 *   现在两重保险：
 *     · 本文件末尾的 _Static_assert（编译期就拦住）
 *     · tools/check_struct_sizes.py（用 ARM 编译器实测 sizeof 与
 *       Python 端常量对拍，进 CI / 进测试套件）
 */

typedef struct __attribute__((packed)) {
    /* --- 分区匹配条件 --- */
    uint8_t  key_lo;        /* 按键范围（含）                        */
    uint8_t  key_hi;
    uint8_t  vel_lo;        /* 力度范围（含），1..127                */
    uint8_t  vel_hi;

    /* --- 采样寻址 --- */
    uint8_t  chip_id;       /* 单片方案恒为 0（保留字段，占位不改尺寸）*/
    uint8_t  codec;         /* gmss_codec_t                          */
    uint16_t flags;         /* 见 GMSS_ZF_*                          */

    uint32_t sample_off;    /* ★ 全片**绝对**字节偏移（含库基址 0x40000）*/
    uint32_t sample_len;    /* 采样总帧数（解码后）                  */
    uint32_t loop_start;    /* 循环起点（帧，解码后）0xFFFFFFFF = 不循环 */
    uint32_t loop_end;      /* 循环终点（帧，不含）                  */

    /* --- 混合编码分界 --- */
    uint32_t attack_samples;/* 前 N 帧为 PCM16，其余用 codec。0 = 全用 codec */

    /* --- 编码辅助表（仅 ADPCM 需要）---
     * 每 GMSS_ADPCM_KEYFRAME_INTERVAL 帧存一份解码器状态 (predictor, step_index)，
     * 各 2 字节小端，共 (sample_len / INTERVAL + 1) 条。
     * 固件寻址循环点/O(1) 定位时先跳到此表恢复状态，再顺序解码。
     * 没有这张表就必须从 zone 开头解码——对循环采样不可接受。
     */
    uint32_t keyframe_off;  /* 相对 sample_off 的偏移；0xFFFFFFFF = 无 */
    uint32_t keyframe_count;

    /* --- 音高 --- */
    uint8_t  root_key;      /* 采样原始音高（MIDI note）             */
    int8_t   tune_cents;    /* 微调，-100..+100 音分                 */
    int16_t  gain_db100;    /* 增益，单位 0.01dB，用于层间音量平衡    */

    /* --- 包络（时间单位：毫秒；音量单位：0..32767 的 Q15） --- */
    uint16_t env_attack_ms;
    uint16_t env_decay_ms;
    int16_t  env_sustain;   /* Q15，0..32767。32767 = 无衰减        */
    uint16_t env_release_ms;

    /* --- 采样循环微调（用于消除循环点咔哒声） --- */
    int16_t  loop_xfade;    /* 循环交叉淡化帧数，0 = 不做           */
    uint16_t pad0;

    uint32_t reserved0;     /* 凑满 56 字节                          */
} gmss_zone_t;              /* 共 56 字节 */

/* ADPCM 关键帧间隔（帧）。128 帧 ≈ 2.7ms @48kHz，开销 4/128 = 3.1% */
#define GMSS_ADPCM_KEYFRAME_INTERVAL 128u

/* zone flags */
#define GMSS_ZF_LOOPED      0x0001  /* 采样含循环段 */
#define GMSS_ZF_PERCUSSIVE  0x0002  /* 打击乐：收到 note-off 就进 release */
#define GMSS_ZF_LOOP_REVERSE 0x0004 /* 预留：反向循环 */

/* 特殊值 */
#define GMSS_LOOP_NONE      0xFFFFFFFFu

/* ------------------------------------------------------------------ */
/* 采样池排列（取代旧的多片交错策略）                                    */
/* ------------------------------------------------------------------ */
/*
 * 旧方案把采样 round-robin 分到 4 片，指望多个复音的 QSPI 读能"并行"。
 * 单片之后这个前提不成立了：**所有采样在同一条 QSPI 总线上**，
 * 物理上不可能并行，交错只会让地址更碎、突发更短，反而更慢。
 *
 * 新策略：**按访问局部性紧凑排列**
 *   1. 同一采样的内容去重（内容指纹相同 → 只存一份）
 *   2. 同一乐器的多个力度层相邻存放（换力度层时地址跳变小）
 *   3. 每条采样 4KB 对齐（突发读不跨页）
 *   4. 鼓组放最后（它们的读取最随机）
 *
 * 实测带宽（见 docs/可行性评估 §3.4）：QMI direct CLKDIV=4 → SCK 18.75MHz
 * → 约 2.34 MB/s，48 复音满负载需 1.43 MB/s，余量 1.63×。
 */

/* ------------------------------------------------------------------ */
/* 固件侧 zone 匹配逻辑（参考实现，避免两端口径不一致）                  */
/* ------------------------------------------------------------------ */

static inline int gmss_zone_matches(const gmss_zone_t *z, uint8_t key, uint8_t vel)
{
    return (key >= z->key_lo && key <= z->key_hi &&
            vel >= z->vel_lo && vel <= z->vel_hi);
}

/*
 * 选择 zone：在匹配的 zone 中挑力度范围最窄的（最专门化的）那个。
 * SF2 的语义就是"最具体者优先"，按 vel 跨度排序即可。
 */
static inline const gmss_zone_t *gmss_select_zone(const gmss_zone_t *zones,
                                                  uint16_t first, uint16_t count,
                                                  uint8_t key, uint8_t vel)
{
    const gmss_zone_t *best = 0;
    int best_span = 0x7FFFFFFF;
    for (uint16_t i = 0; i < count; i++) {
        const gmss_zone_t *z = &zones[first + i];
        if (!gmss_zone_matches(z, key, vel)) continue;
        int span = (int)z->vel_hi - (int)z->vel_lo;
        if (span < best_span) { best_span = span; best = z; }
    }
    return best;
}

/* ================================================================== */
/* ★★ 结构体尺寸的编译期断言                                            */
/* ================================================================== */
/*
 * 这三个必须与 PC 端 tools/gmss/gmss/layout.py 的
 * HEADER_SIZE / INSTR_SIZE / ZONE_SIZE 完全一致。
 *
 * ★ 为什么值得写这三个断言（血泪教训）：
 *   ZONE_SIZE 曾经在文档和 PC 端都写成 58，而 C 这边实际是 56。
 *   两边各写各的、谁也没问过对方，结果 zone 表每条错位 2 字节。
 *   这种错误不会崩溃、不会报错，只会让音频完全乱掉 ——
 *   靠"人眼核对注释"是防不住的，只有编译器能防。
 *
 * ★ 改这三个断言值之前，先改结构体，再让编译器告诉你新尺寸，
 *   最后同步 PC 端并跑 tools/check_struct_sizes.py 对拍。
 */
_Static_assert(sizeof(gmss_header_t)     == 64, "gmss_header_t 必须是 64 字节（PC 端 HEADER_SIZE）");
_Static_assert(sizeof(gmss_instrument_t) == 32, "gmss_instrument_t 必须是 32 字节（PC 端 INSTR_SIZE）");
_Static_assert(sizeof(gmss_zone_t)       == 56, "gmss_zone_t 必须是 56 字节（PC 端 ZONE_SIZE）");
_Static_assert(sizeof(gmss_chip_header_t) == 16, "gmss_chip_header_t 必须是 16 字节（PC 端 CHIP_HDR_SIZE）");

/* zone 内部关键字段的偏移也钉死 —— 尺寸对但偏移错同样是灾难 */
_Static_assert(offsetof(gmss_zone_t, sample_off)    ==  8, "sample_off 必须在偏移 8");
_Static_assert(offsetof(gmss_zone_t, sample_len)    == 12, "sample_len 必须在偏移 12");
_Static_assert(offsetof(gmss_zone_t, loop_start)    == 16, "loop_start 必须在偏移 16");
_Static_assert(offsetof(gmss_zone_t, loop_end)      == 20, "loop_end 必须在偏移 20");
_Static_assert(offsetof(gmss_zone_t, attack_samples) == 24, "attack_samples 必须在偏移 24");
_Static_assert(offsetof(gmss_zone_t, keyframe_off)  == 28, "keyframe_off 必须在偏移 28（曾漏掉这个字段）");
_Static_assert(offsetof(gmss_zone_t, keyframe_count) == 32, "keyframe_count 必须在偏移 32");
_Static_assert(offsetof(gmss_zone_t, root_key)      == 36, "root_key 必须在偏移 36");
_Static_assert(offsetof(gmss_zone_t, env_attack_ms) == 40, "env_attack_ms 必须在偏移 40");
_Static_assert(offsetof(gmss_zone_t, loop_xfade)    == 48, "loop_xfade 必须在偏移 48");

/* instrument 表的两个字段也不能错位 */
_Static_assert(offsetof(gmss_instrument_t, zone_first) == 0, "zone_first 必须在偏移 0");
_Static_assert(offsetof(gmss_instrument_t, bank)       == 4, "bank 必须在偏移 4");
_Static_assert(offsetof(gmss_instrument_t, name)       == 8, "name 必须在偏移 8");

#endif /* GMSS_FORMAT_H */
