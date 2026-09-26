/*
 * sampler.c — 音色库访问与采样解码
 *
 * ★ 不 include 任何 pico-sdk 头 —— 见 sampler.h 顶部说明。
 *   目标上通过 sampler_set_library() 指向 XIP 映射地址；
 *   主机测试时指向加载进内存的镜像。
 *
 * ★★ 与 Python 参考实现的逐位一致性
 *   解码算法必须与 tools/gmss/gmss/adpcm.py 的 decode_hybrid() 完全一致，
 *   差一个 LSB 都会在 PCM→ADPCM 交界和循环点产生咔哒。
 *   两个容易写错、也最容易在移植时写错的地方：
 *
 *   1. **整数除法的取整方向**
 *      Python 的 // 是向下取整（-7 // 2 == -4），C 的 / 是向零取整（-7 / 2 == -3）。
 *      Python 侧专门写了 _trunc_div() 来模拟 C 语义，说明这个差异是真的踩过的。
 *      本文件里所有除法都保证是**非负操作数**（帧号、字节偏移），所以两种语义一致；
 *      唯一涉及负数的是 predictor 的更新，那里全程用加法和 clamp，不做除法。
 *
 *   2. **nibble 的奇偶**
 *      混合编码里 ADPCM 流覆盖**整个**采样缓冲区，第 p 个采样点的 nibble
 *      在 blob[pcm_bytes + (p>>1)]，奇偶由 **p&1** 决定 —— 用的是 p，不是 p-attack。
 *      写成 p-attack 的话，一旦 attack 是奇数，所有 nibble 的奇偶整体翻转，
 *      解出来全是噪声（而且只在 attack 为奇数时复现，极难查）。
 */
#include <string.h>

#include "sampler.h"

/* ================================================================== */
/* 定长表与常量                                                        */
/* ================================================================== */

/* 标准 IMA/DVI ADPCM 步长表（89 项，与 adpcm.py 的 STEP_TABLE 逐项一致） */
static const int32_t k_step_table[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
    2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
    18500, 20350, 22385, 24623, 27086, 29794, 32767,
};

/* 步长索引调整表（16 项，与 adpcm.py 的 INDEX_TABLE 逐项一致） */
static const int8_t k_index_table[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
};

/* 编译期自检：表长度写错会静默解出噪声，必须挡住 */
_Static_assert(sizeof(k_step_table) / sizeof(k_step_table[0]) == 89,
               "IMA 步长表必须是 89 项");
_Static_assert(sizeof(k_index_table) / sizeof(k_index_table[0]) == 16,
               "IMA 索引表必须是 16 项");

/* 关键帧间隔必须与 PC 端 adpcm.KEYFRAME_INTERVAL 一致 */
_Static_assert(GMSS_ADPCM_KEYFRAME_INTERVAL == 128,
               "关键帧间隔改了必须同步改 tools/gmss/gmss/adpcm.py");

/* ================================================================== */
/* 内部状态                                                            */
/* ================================================================== */

static const uint8_t *s_lib;           /* 指向 gmss_header_t */
static const gmss_header_t *s_hdr;
static const uint8_t *s_zone_tab;
static const uint8_t *s_instr_tab;
static bool     s_ready;
static const char *s_err = "尚未初始化";
static uint64_t s_frames_decoded;

/* ================================================================== */
/* 小工具                                                              */
/* ================================================================== */

static inline int16_t clamp16(int32_t v)
{
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return (int16_t)v;
}

/*
 * 把 flash 绝对偏移转成指针。
 * ★ 目标与主机通用：偏移减掉库基址才是库内偏移。
 *   sample_off 保证 >= GMSS_LIB_BASE（layout.py 会校验），不会下溢。
 */
static inline const uint8_t *flash_ptr(uint32_t off)
{
    return s_lib + (off - GMSS_LIB_BASE);
}

/* 小端读 16 位（**逐字节拼，不假设对齐**） */
static inline int16_t rd_i16le(const uint8_t *p)
{
    return (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

/* ================================================================== */
/* FNV-1a 32                                                           */
/* ================================================================== */
/*
 * ★ 与 layout.fnv1a() 必须逐位一致。
 *   FNV-1a 的迭代是 h = (h ^ b) * P，**每一步都截断到 32 位**。
 *   截断破坏了乘法的分配律，所以任何"向量化/并行化"的写法都不成立
 *   （Python 侧为此专门写了长注释记录这件事）。
 *   这里就用最朴素的逐字节循环。
 *
 * 覆盖范围：zone 表 + 128 槽旋律音色表 + 鼓组表。
 * **不覆盖采样池** —— 那是 15MB 量级，逐字节算要几百毫秒，
 * 而且真正会先坏掉的通常是表区。采样池的完整性靠"烧录后回读比对"保证。
 */
static uint32_t fnv1a(const uint8_t *p, uint32_t len)
{
    uint32_t h = 0x811C9DC5u;
    for (uint32_t i = 0; i < len; i++) {
        h ^= p[i];
        h *= 0x01000193u;
    }
    return h;
}

/* ================================================================== */
/* ADPCM 单 nibble 解码                                                */
/* ================================================================== */
/*
 * 与 adpcm.decode_nibble() 逐步对应：
 *   step  = STEP_TABLE[step_index]
 *   delta = (step >> 3)
 *         + (code & 4 ? step      : 0)
 *         + (code & 2 ? step >> 1 : 0)
 *         + (code & 1 ? step >> 2 : 0)
 *   predictor = clamp16(predictor ± delta)      // code & 8 决定符号
 *   step_index = clamp(step_index + INDEX_TABLE[code], 0, 88)
 *
 * ★ 注意：delta 的构造用的是"先取 step>>3，再按位置加 step / step>>1 / step>>2"，
 *   这不是"step * (code&7) / 8"的等价写法（后者会差 LSB）。
 *   必须照抄，不能"化简"。
 */
static inline int16_t adpcm_decode_nibble(int16_t *pred, uint8_t *step_idx,
                                          uint8_t code)
{
    const int32_t step = k_step_table[*step_idx];

    int32_t delta = step >> 3;
    if (code & 4u) delta += step;
    if (code & 2u) delta += step >> 1;
    if (code & 1u) delta += step >> 2;

    int32_t p = *pred;
    if (code & 8u) {
        p -= delta;
    } else {
        p += delta;
    }
    *pred = clamp16(p);

    int32_t si = (int32_t)*step_idx + (int32_t)k_index_table[code & 0xFu];
    if (si < 0)  si = 0;
    if (si > 88) si = 88;
    *step_idx = (uint8_t)si;

    return *pred;
}

/* ================================================================== */
/* 关键帧表读取                                                        */
/* ================================================================== */
/*
 * 每条 4 字节：predictor(int16 LE) + step_index(uint8) + pad
 * （与 adpcm.AdpcmState.to_bytes 的 "<hBB" 一致）
 *
 * ★ kf_table 可能落在**奇数地址**（keyframe_off 可以是奇数），
 *   所以这里逐字节拼，不做 int16 指针解引用。
 */
static void keyframe_read(const uint8_t *kf_table, uint32_t index,
                          int16_t *pred, uint8_t *step_idx)
{
    const uint8_t *e = kf_table + index * 4u;
    *pred = rd_i16le(e);
    *step_idx = e[2];
    if (*step_idx > 88) *step_idx = 88;
}

/* 解析一个 zone 的关键帧表指针；没有表时返回 NULL */
static const uint8_t *zone_kf_table(const gmss_zone_t *z)
{
    if (z->keyframe_off == GMSS_LOOP_NONE || z->keyframe_count == 0) {
        return NULL;
    }
    return flash_ptr(z->sample_off) + z->keyframe_off;
}

/* ================================================================== */
/* 初始化                                                              */
/* ================================================================== */

void sampler_set_library(const void *lib_base)
{
    s_lib = (const uint8_t *)lib_base;
    s_hdr = NULL;
    s_zone_tab = NULL;
    s_instr_tab = NULL;
    s_ready = false;
}

const char *sampler_last_error(void) { return s_err; }
bool sampler_ready(void) { return s_ready; }
const gmss_header_t *sampler_header(void) { return s_hdr; }

int sampler_init(void)
{
    s_ready = false;

    if (s_lib == NULL) {
        s_err = "音色库指针为空（sampler_set_library 没调？）";
        return -1;
    }

    const gmss_header_t *h = (const gmss_header_t *)s_lib;

    if (h->magic != GMSS_MAGIC) {
        s_err = "魔数不对：该地址上没有 GMSS 音色库";
        return -1;
    }
    if (h->version_major != GMSS_VERSION_MAJOR ||
        h->header_size != sizeof(gmss_header_t)) {
        s_err = "版本或头部长度不支持（PC 工具链与固件版本不匹配？）";
        return -2;
    }
    if (h->sample_rate != GMSS_SAMPLE_RATE) {
        s_err = "采样率不是 48000（全库必须统一，固件不做采样率转换）";
        return -3;
    }

    /* 表区边界检查：挡在越界读之前 */
    const uint32_t zone_bytes  = (uint32_t)h->total_zones * sizeof(gmss_zone_t);
    const uint32_t instr_bytes = (128u + h->drumkit_count) * sizeof(gmss_instrument_t);
    const uint32_t tab_end     = h->zone_table_off + zone_bytes + instr_bytes;
    if (h->zone_table_off < sizeof(gmss_header_t) ||
        h->instr_table_off < h->zone_table_off ||
        tab_end > h->pool_off) {
        s_err = "表区越界（zone 表/音色表跑进采样池了）";
        return -5;
    }

    /* 校验和：覆盖 zone 表 + 128 槽旋律表 + 鼓组表 */
    const uint32_t sum = fnv1a(s_lib + h->zone_table_off,
                               zone_bytes + instr_bytes);
    if (sum != h->checksum) {
        s_err = "校验和不符：音色库没烧全或已损坏";
        return -4;
    }

    s_hdr = h;
    s_zone_tab = s_lib + h->zone_table_off;
    s_instr_tab = s_lib + h->instr_table_off;
    s_frames_decoded = 0;
    s_ready = true;
    s_err = "";
    return 0;
}

uint32_t sampler_zone_count(void)   { return s_hdr ? s_hdr->total_zones : 0; }
uint16_t sampler_melodic_count(void){ return s_hdr ? s_hdr->instrument_count : 0; }
uint16_t sampler_drumkit_count(void){ return s_hdr ? s_hdr->drumkit_count : 0; }
uint64_t sampler_frames_decoded(void){ return s_frames_decoded; }

/* ================================================================== */
/* 查询                                                                */
/* ================================================================== */

static inline const gmss_zone_t *zone_at(uint32_t i)
{
    return (const gmss_zone_t *)(s_zone_tab + i * sizeof(gmss_zone_t));
}

static inline const gmss_instrument_t *instr_at(uint32_t i)
{
    return (const gmss_instrument_t *)(s_instr_tab + i * sizeof(gmss_instrument_t));
}

const gmss_instrument_t *sampler_find_instrument(uint8_t bank, uint8_t program)
{
    if (!s_ready) return NULL;

    if (bank == 128) {
        /* 鼓组：紧跟在 128 槽旋律表之后 */
        for (uint16_t i = 0; i < s_hdr->drumkit_count; i++) {
            const gmss_instrument_t *it = instr_at(128u + i);
            if (it->program == program) return it;
        }
        return NULL;
    }

    /* 旋律：program 直接索引（PC 端保证补齐到 128 槽） */
    if (program > 127) return NULL;
    const gmss_instrument_t *it = instr_at(program);
    if (it->zone_count == 0) return NULL;    /* 空槽 */
    return it;
}

const gmss_zone_t *sampler_select_zone(const gmss_instrument_t *inst,
                                       uint8_t key, uint8_t vel)
{
    if (!s_ready || inst == NULL || inst->zone_count == 0) return NULL;

    const gmss_zone_t *best = NULL;
    int best_span = 0x7FFFFFFF;

    /* ★ 与 gmss_format.h 的 gmss_select_zone() 同一套逻辑：
     *   在匹配的 zone 里挑**力度跨度最窄**的 —— 这就是 SF2 的
     *   "最具体者优先"语义。跨度相同时取先出现的（稳定、可复现）。 */
    for (uint16_t i = 0; i < inst->zone_count; i++) {
        const gmss_zone_t *z = zone_at((uint32_t)inst->zone_first + i);
        if (key < z->key_lo || key > z->key_hi) continue;
        if (vel < z->vel_lo || vel > z->vel_hi) continue;
        const int span = (int)z->vel_hi - (int)z->vel_lo;
        if (span < best_span) {
            best_span = span;
            best = z;
        }
    }
    return best;
}

/* ================================================================== */
/* 随机访问解码                                                        */
/* ================================================================== */

/*
 * 把 ADPCM 状态定位到第 target 帧：
 *   1. k = target / 128，从 kf_table[k] 恢复状态
 *   2. 从 k*128 顺序解码到 target，丢弃结果
 * 与 adpcm.decode_at() 的第 1~3 步一致。
 */
static void adpcm_seek(const uint8_t *blob, uint32_t pcm_bytes,
                       const uint8_t *kf_table,
                       uint32_t target, int16_t *pred, uint8_t *step_idx)
{
    uint32_t k = target / GMSS_ADPCM_KEYFRAME_INTERVAL;

    if (kf_table != NULL && k < 0xFFFFFFFFu) {
        /* 关键帧数量由调用方保证覆盖；这里只做保守处理 */
        keyframe_read(kf_table, k, pred, step_idx);
    } else {
        *pred = 0;
        *step_idx = 0;
        k = 0;
    }

    uint32_t p = k * GMSS_ADPCM_KEYFRAME_INTERVAL;
    while (p < target) {
        const uint8_t byte = blob[pcm_bytes + (p >> 1)];
        const uint8_t code = (p & 1u) ? (byte & 0x0Fu) : (uint8_t)(byte >> 4);
        adpcm_decode_nibble(pred, step_idx, code);
        p++;
    }
}

void sampler_read_frames(const gmss_zone_t *z, uint32_t start,
                         uint32_t count, int16_t *out)
{
    if (z == NULL || out == NULL || count == 0) return;

    const uint8_t *blob = flash_ptr(z->sample_off);
    const uint32_t pcm_bytes = z->attack_samples * 2u;
    const uint32_t total = z->sample_len;

    /*
     * 与 adpcm.decode_hybrid() 的分段完全一致：
     *   n_head = min(count, max(0, attack - start))
     *   rest_start = max(start, attack)
     */
    uint32_t n_head = 0;
    if (start < z->attack_samples) {
        const uint32_t avail = z->attack_samples - start;
        n_head = (count < avail) ? count : avail;
    }

    /* --- PCM16 段：直接读，不用解码器 --- */
    for (uint32_t i = 0; i < n_head; i++) {
        const uint32_t p = start + i;
        out[i] = (p < total) ? rd_i16le(blob + p * 2u) : 0;
    }

    const uint32_t rest_n = count - n_head;
    if (rest_n == 0) return;
    if (z->codec != GMSS_CODEC_ADPCM4) {
        /* 纯 PCM16 的 zone：后面全部直接读 */
        for (uint32_t i = 0; i < rest_n; i++) {
            const uint32_t p = start + n_head + i;
            out[n_head + i] = (p < total) ? rd_i16le(blob + p * 2u) : 0;
        }
        s_frames_decoded += count;
        return;
    }

    uint32_t p = start + n_head;
    if (p < z->attack_samples) p = z->attack_samples;

    int16_t pred = 0;
    uint8_t si = 0;
    const uint8_t *kf = zone_kf_table(z);
    adpcm_seek(blob, pcm_bytes, kf, p, &pred, &si);

    for (uint32_t i = 0; i < rest_n; i++) {
        if (p >= total) { out[n_head + i] = 0; p++; continue; }
        const uint8_t byte = blob[pcm_bytes + (p >> 1)];
        const uint8_t code = (p & 1u) ? (byte & 0x0Fu) : (uint8_t)(byte >> 4);
        out[n_head + i] = adpcm_decode_nibble(&pred, &si, code);
        p++;
    }

    s_frames_decoded += count;
}

/* ================================================================== */
/* 连续流解码器                                                        */
/* ================================================================== */

/*
 * 解出第 p 帧并推进状态。
 *
 * ★ adpcm_primed 这个标志是必要的，不能用"是否已 seek 过"代替：
 *   播放起点可能落在 PCM 段（此时 ADPCM 状态还没意义），
 *   直到第一次读到 p >= attack_samples 才需要把状态定位过去。
 *   漏掉这个标志的症状：PCM 段播完后 ADPCM 从状态 0 开始解，
 *   交界处一个巨大的跳变（听感是明显的"啪"）。
 */
static int16_t decode_frame_at(gmss_stream_t *s, uint32_t p)
{
    const gmss_zone_t *z = s->zone;

    if (p >= z->sample_len) return 0;

    if (gmss_is_pcm_frame(z, p)) {
        return rd_i16le(s->blob + p * 2u);
    }

    const uint32_t pcm_bytes = z->attack_samples * 2u;

    if (!s->adpcm_primed) {
        adpcm_seek(s->blob, pcm_bytes, s->kf_table, p, &s->pred, &s->step_idx);
        s->adpcm_primed = true;
    }

    const uint8_t byte = s->blob[pcm_bytes + (p >> 1)];
    const uint8_t code = (p & 1u) ? (byte & 0x0Fu) : (uint8_t)(byte >> 4);
    return adpcm_decode_nibble(&s->pred, &s->step_idx, code);
}

void gmss_stream_seek(gmss_stream_t *s, const gmss_zone_t *z, uint32_t start)
{
    s->zone = z;
    s->blob = flash_ptr(z->sample_off);
    s->kf_table = zone_kf_table(z);
    s->adpcm_primed = false;
    s->pred = 0;
    s->step_idx = 0;

    /*
     * ★ 语义约定：**seek(start) 之后第一次 gmss_stream_next() 返回第 start 帧。**
     *
     *   gmss_stream_next() 返回的是 nxt，所以 nxt 必须已经是 frame(start)，
     *   而 cur（插值的左端点）应该是 frame(start-1)。
     *
     *   ★ 这里最初写错了：seek 里摆成 cur=frame(start)、nxt=frame(start+1)，
     *     于是第一次 next() 返回的是 frame(start+1) —— **整体错开一帧**。
     *     症状极其隐蔽：音频听起来是正常的（就是整体晚了一个采样点），
     *     但循环点每绕一圈就错开一帧，而且和 Python 参考解码器对不上。
     *     是 tools/verify_sampler_host.py 的逐样本对拍照出来的。
     *
     *   起始帧没有"前一帧"，就让 cur = nxt（插值恒等于 frame(0)）。
     *   这比让 cur=0 好：后者会在第一个采样周期内做一次 0→frame(0) 的淡入，
     *   对起音本来就很陡的采样（钢琴、鼓）反而制造了一个假的爬升。
     */
    if (start >= z->sample_len) {
        s->dec_next = z->sample_len;
        s->cur = 0;
        s->nxt = 0;
        return;
    }

    if (start == 0) {
        s->nxt = decode_frame_at(s, 0);
        s->cur = s->nxt;
        s->dec_next = 1;
        return;
    }

    /* 需要 cur = frame(start-1)、nxt = frame(start)。
     * decode_frame_at 会按帧号自己决定走 PCM 还是 ADPCM，
     * 并在第一次进入 ADPCM 段时把状态定位过去 —— 所以
     * "start-1 在 PCM、start 在 ADPCM" 这种跨界的组合也是对的。 */
    s->cur = decode_frame_at(s, start - 1u);
    s->nxt = decode_frame_at(s, start);
    s->dec_next = start + 1u;
}

int16_t gmss_stream_next(gmss_stream_t *s)
{
    const int16_t out = s->nxt;
    s->cur = s->nxt;
    s->nxt = decode_frame_at(s, s->dec_next);
    s->dec_next++;
    return out;
}
