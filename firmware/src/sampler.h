/*
 * sampler.h — 音色库访问与采样解码（PCM16 / IMA-ADPCM4 混合编码）
 *
 * ★ 本文件与 sampler.c 都**不 include 任何 pico-sdk 头**，
 *   只依赖 gmss_format.h 和标准 C。这样同一份代码能：
 *     · 在目标上跑（音色库从 XIP 内存映射地址读）
 *     · 在 PC 上用 zig/cc 编译成 x86-64 做单元测试，
 *       并与 tools/gmss/gmss/player.py 的 Python 参考解码器逐样本对拍
 *
 *   这是刻意的设计：采样解码是全工程最容易"差一个 LSB 就出咔哒"的地方，
 *   必须在没有硬件的情况下就能验证。
 *
 * 数据流
 * ------
 *   板载 Flash（16MB，CS0）
 *     ├─ 0x000000  固件
 *     └─ 0x040000  GMSS 音色库  ← sampler 从这里读
 *           ├─ gmss_header_t
 *           ├─ zone 表       (56B × N)
 *           ├─ 音色表        (32B × 136)
 *           └─ 采样池        ← sample_off 指向这里
 *
 *   ★ sample_off 是**整片 Flash 的绝对偏移**，不是库内偏移。
 *     固件里把它转成指针的方式是：
 *         ptr = lib + (sample_off - GMSS_LIB_BASE)
 *     目标上 lib = 0x10040000（XIP 映射），主机上 lib = 加载进内存的镜像首地址。
 *     同一个表达式两边都对。
 *
 * ★★ 关于 unaligned 访问（踩过一次，记下来）
 *   keyframe_off = 采样数据长度 = attack*2 + ADPCM字节数。
 *   attack*2 一定是偶数，但 ADPCM 字节数 = (n+1)/2 **可能是奇数**，
 *   所以关键帧表有可能落在**奇数地址**上。
 *   Cortex-M33 支持非对齐 LDRH，但会慢，而且换平台就未必支持。
 *   → 关键帧的 predictor **逐字节拼**，不做 int16 指针解引用。
 *     关键帧只在 note-on 和循环回绕时读，不在热路径上，慢一点无所谓。
 *   （PCM16 段则一定是 2 字节对齐的：sample_off 是 4KB 对齐，
 *     段内偏移又是 start*2，所以可以直接 int16 读。）
 */
#ifndef SAMPLER_H
#define SAMPLER_H

#include <stdint.h>
#include <stdbool.h>

#include "gmss_format.h"

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

/*
 * 指定音色库在**内存里的位置**。
 * 目标上：sampler_init() 会自动指向 XIP 映射地址，不用手动调。
 * 主机测试：加载 library.bin 到内存后调用本函数指过去。
 */
void sampler_set_library(const void *lib_base);

/*
 * 校验并启用音色库。
 *
 * 做三件事：
 *   1. 检查 magic / version / header_size
 *   2. 检查 sample_rate == 48000（全库统一，固件不做采样率转换）
 *   3. 重算 FNV-1a 校验和，与头部记录比对
 *
 * 返回：
 *    0  成功
 *   -1  magic 不对（该位置没有音色库）
 *   -2  版本/头长度不支持
 *   -3  采样率不是 48000
 *   -4  校验和不符（库没烧全 / 烧错了 / 坏了）
 *   -5  zone 表或音色表越界
 *
 * ★ 校验和不符时**必须拒绝加载**，不能"凑合播"。
 *   半截的库会让 sample_off 指到垃圾数据，症状是刺耳的噪声，
 *   比直接静音难查得多。
 */
int sampler_init(void);

/* 上一次 sampler_init 的失败原因（人可读，用于串口打印） */
const char *sampler_last_error(void);

/* 库是否可用 */
bool sampler_ready(void);

/* 头部指针（未初始化时为 NULL） */
const gmss_header_t *sampler_header(void);

/* ------------------------------------------------------------------ */
/* 查询                                                                */
/* ------------------------------------------------------------------ */

/*
 * 按 (bank, program) 找音色表条目。
 * bank: 0 = 旋律（program 0..127），128 = 鼓组
 * 找不到返回 NULL。
 */
const gmss_instrument_t *sampler_find_instrument(uint8_t bank, uint8_t program);

/*
 * 在音色内按 (key, vel) 选 zone —— SF2 的"最具体者优先"语义：
 * 在所有匹配的 zone 里挑**力度跨度最窄**的那个。
 *
 * ★ 鼓组音色（fixed_note >= 0）由调用方把 key 换成 fixed_note 再调，
 *   本函数不管这件事。
 *
 * 找不到匹配返回 NULL。
 */
const gmss_zone_t *sampler_select_zone(const gmss_instrument_t *inst,
                                       uint8_t key, uint8_t vel);

/* ------------------------------------------------------------------ */
/* 采样解码                                                            */
/* ------------------------------------------------------------------ */

/*
 * ADPCM 解码器状态（可序列化 = 关键帧表里的一条）
 * 与 tools/gmss/gmss/adpcm.py 的 AdpcmState 一一对应。
 */
typedef struct {
    int16_t  predictor;
    uint8_t  step_index;    /* 0..88 */
} gmss_adpcm_state_t;

/*
 * 从 zone 的第 start 帧开始，连续解出 count 帧到 out[]。
 *
 * ★ 这是**随机访问**接口：内部会查关键帧表把状态跳到位，
 *   适合 note-on 取头几个点、或调试对拍。
 *   **热路径不要用它** —— 每个采样点都调一次会退化成 O(每帧重解码)。
 *   连续播放请用 gmss_stream_t（见下）。
 *
 * 超出 sample_len 的部分补 0（不报错，方便调用方少写边界判断）。
 */
void sampler_read_frames(const gmss_zone_t *z, uint32_t start,
                         uint32_t count, int16_t *out);

/*
 * 混合编码的分界点。p < attack_samples → PCM16；否则 ADPCM。
 * 这就是解码路径的分支条件，单独抽出来是为了让主机测试能覆盖它。
 */
static inline bool gmss_is_pcm_frame(const gmss_zone_t *z, uint32_t p)
{
    return p < z->attack_samples;
}

/* ------------------------------------------------------------------ */
/* 连续流解码器（voice 用它）                                          */
/* ------------------------------------------------------------------ */

/*
 * ★ 为什么需要它：
 *   带音高移位的播放，读取位置是逐点前进的（step 可大于或小于 1），
 *   而且要线性插值 —— 需要"当前帧"和"下一帧"两个已解码的点。
 *
 *   做法：顺序推进解码器，维护 (cur, nxt) 两个点。
 *     位置越过一个整数帧 → cur = nxt，再解一帧填 nxt。
 *   这样**每前进一个整数帧只解码一次**，不需要每点重查关键帧。
 *
 *   循环回绕是唯一需要重新定位的地方：从关键帧恢复状态后，
 *   顺序解码追上 loop_start（最多 127 个 nibble）。
 *
 * 帧号语义：dec_next 是"下一个要解码的帧号"。
 *           cur 对应 dec_next-2，nxt 对应 dec_next-1。
 *           （也就是 cur 是当前播放位置取整后的点）
 */
typedef struct {
    const gmss_zone_t *zone;
    const uint8_t     *blob;        /* 采样数据首字节（已解析成指针） */
    const uint8_t     *kf_table;    /* 关键帧表首字节，无表时为 NULL */

    uint32_t dec_next;              /* 下一个要解出的帧号 */
    int16_t  pred;                  /* ADPCM 状态 */
    uint8_t  step_idx;
    bool     adpcm_primed;          /* ADPCM 状态是否已定位（见 sampler.c 说明）*/

    int16_t  cur;                   /* 帧 dec_next-2 的值 */
    int16_t  nxt;                   /* 帧 dec_next-1 的值 */
} gmss_stream_t;

/*
 * 把流定位到第 start 帧。
 * z 必须是有效 zone；blob / kf_table 由 sampler 内部解析，调用方不用管。
 *
 * ★ 语义：**seek(start) 之后第一次 gmss_stream_next() 返回第 start 帧。**
 *   内部把 (cur, nxt) 摆成 (frame(start-1), frame(start))，
 *   start==0 时没有前一帧，取 cur = nxt = frame(0)。
 */
void gmss_stream_seek(gmss_stream_t *s, const gmss_zone_t *z, uint32_t start);

/*
 * 解出下一帧并推进。
 * 返回到达 sample_len 之后的行为由调用方决定（本函数会返回 0）。
 */
int16_t gmss_stream_next(gmss_stream_t *s);

/* 当前帧 / 下一帧（供插值用），不推进 */
static inline int16_t gmss_stream_cur(const gmss_stream_t *s) { return s->cur; }
static inline int16_t gmss_stream_nxt(const gmss_stream_t *s) { return s->nxt; }

/* ------------------------------------------------------------------ */
/* 诊断                                                               */
/* ------------------------------------------------------------------ */

/* 全库唯一的采样分区数 / 旋律音色数 / 鼓组数（加载成功后有效） */
uint32_t sampler_zone_count(void);
uint16_t sampler_melodic_count(void);
uint16_t sampler_drumkit_count(void);

/* 累计解码了多少帧（用于性能评估与"真的在播吗"的判断） */
uint64_t sampler_frames_decoded(void);

#endif /* SAMPLER_H */
