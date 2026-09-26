/*
 * voice.c — 复音管理与混音
 *
 * 见 voice.h 顶部的设计说明（相位语义、滑动窗口、爆音来源）。
 *
 * ★ 本文件不 include 任何 pico-sdk 头，可在主机上编译测试。
 */
#include <string.h>

#include "voice.h"

/* ================================================================== */
/* 状态                                                                */
/* ================================================================== */

static voice_t  s_voice[VOICE_MAX];
static uint32_t s_age;              /* 单调递增的分配序号 */
static uint64_t s_alloc_total;
static uint64_t s_steal_total;

/* 每个 MIDI 通道的增益与声像（CC7 / CC11 / CC10 更新） */
static int32_t  s_chan_gain[16];
static int8_t   s_chan_pan[16];

/* ================================================================== */
/* 初始化                                                              */
/* ================================================================== */

void voice_init_all(void)
{
    memset(s_voice, 0, sizeof(s_voice));
    s_age = 0;
    s_alloc_total = 0;
    s_steal_total = 0;

    for (int i = 0; i < 16; i++) {
        s_chan_gain[i] = 32767;     /* 默认满音量 */
        s_chan_pan[i] = 0;          /* 默认居中 */
    }
}

uint32_t voice_active_count(void)
{
    uint32_t n = 0;
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        if (s_voice[i].active) n++;
    }
    return n;
}

uint64_t voice_alloc_total(void) { return s_alloc_total; }
uint64_t voice_steal_total(void) { return s_steal_total; }

/* ================================================================== */
/* 采样流定位（含滑动窗口）                                            */
/* ================================================================== */

/*
 * 把 voice 的采样流定位到第 start 帧，并填好插值滑动窗口。
 *
 * ★ 为什么抽成函数：note-on 和**循环回绕**都要用它，
 *   而回绕是最容易漏掉重新 seek 的地方 —— ADPCM 是有状态的编码，
 *   位置跳了状态不跳就会解出垃圾，听感是循环点上的一声"啪"。
 */
static void voice_seek(voice_t *v, uint32_t start)
{
    const gmss_zone_t *z = v->zone;
    if (start >= z->sample_len) start = 0;

    gmss_stream_seek(&v->stream, z, start);

    /* seek(start) 之后第一次 next() 返回 frame(start)，
     * 第二次返回 frame(start+1) —— 正好是插值需要的两个端点。 */
    v->s0 = gmss_stream_next(&v->stream);
    v->s1 = gmss_stream_next(&v->stream);
    v->frame_idx = start;
}

/*
 * 把滑动窗口右移，直到左端点等于 target_frame。
 * step > 1（升调）时一次要跳过好几帧，所以用 while 而不是 if。
 */
static void voice_slide_to(voice_t *v, uint32_t target_frame)
{
    while (v->frame_idx < target_frame) {
        v->s0 = v->s1;
        v->s1 = gmss_stream_next(&v->stream);
        v->frame_idx++;
    }
}

/* ================================================================== */
/* 复音分配                                                            */
/* ================================================================== */

/*
 * 找一个可用的 voice。
 *
 * 优先级：
 *   1. 完全空闲的
 *   2. 已经进入 release 的里面**最老**的
 *   3. 还在发声的里面最老的
 *
 * ★ 为什么挑"最老"而不是"最轻"：
 *   最轻的往往是刚起音的音，抢掉它反而更明显。
 *   最老的那个通常已经衰减得差不多了。
 *
 * ★ 关于抢占产生的咔哒：
 *   一个 slot 同时只能装一个音，所以**没法**给被抢占的音做淡出
 *   （淡出需要它继续跑，但 slot 已经被新音占了）。
 *   缓解手段有两个，都已生效：
 *     · 新音的包络从 0 起（不是从旧音的电平跳变过去）
 *     · 48 复音对 GM 独奏/小编制来说基本不会真的抢
 *   真要做无咔哒抢占，得改成"每个 slot 双缓冲"，代价是内存翻倍、
 *   混音路径复杂化。当前规模下不划算。
 */
static int voice_alloc_slot(void)
{
    /* 1. 空闲 */
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        if (!s_voice[i].active) return (int)i;
    }

    /* 2. 正在 release 的最老的 */
    int best = -1;
    uint32_t best_age = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        if (s_voice[i].releasing && s_voice[i].age < best_age) {
            best_age = s_voice[i].age;
            best = (int)i;
        }
    }
    if (best >= 0) { s_steal_total++; return best; }

    /* 3. 任意最老的 */
    best_age = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        if (s_voice[i].age < best_age) {
            best_age = s_voice[i].age;
            best = (int)i;
        }
    }
    s_steal_total++;
    return best;
}

/* ================================================================== */
/* 触发                                                               */
/* ================================================================== */

int voice_note_on(const gmss_zone_t *zone, uint8_t note, uint8_t vel,
                  uint8_t channel, int32_t gain_q15, int8_t pan)
{
    if (zone == NULL || zone->sample_len == 0) return -1;

    const int slot = voice_alloc_slot();
    if (slot < 0) return -1;

    voice_t *v = &s_voice[slot];

    v->active = true;
    v->releasing = false;
    v->note = note;
    v->channel = (uint8_t)(channel & 0x0Fu);
    v->vel = vel;
    v->age = ++s_age;
    v->zone = zone;
    v->pan = pan;

    /*
     * 增益 = 力度曲线 × 通道增益 × zone 的层间平衡增益。
     * ★ zone 的 gain_db100 是"层间音量平衡"用的（同一音色的不同力度层
     *   录音电平不一样，靠它对齐），必须乘进来，
     *   否则换力度层会有音量跳变 —— 这是很容易漏掉的一项。
     */
    v->gain = dsp_mul_q15(dsp_vel_to_q15(vel), gain_q15);
    v->gain = dsp_mul_q15(v->gain, dsp_db100_to_q15(zone->gain_db100));
    if (v->gain < 0) v->gain = 0;

    /* 音高：以 zone 的 root_key 为基准，含 zone 自己的微调音分 */
    v->step_q16 = dsp_pitch_step(note, zone->root_key, zone->tune_cents);
    if (v->step_q16 == 0) v->step_q16 = 1;   /* 防呆：不能原地不动 */

    /*
     * 播放起点固定为 0。
     * ★ 不用 loop_start 起音：起音瞬态（attack 段）是音色辨识的关键，
     *   从循环点起音听起来像"从中间插进来"。
     */
    v->pos_q16 = 0;
    voice_seek(v, 0);

    (void)dsp_env_init(&v->env, zone->env_attack_ms, zone->env_decay_ms,
                       zone->env_sustain, zone->env_release_ms);
    dsp_env_trigger(&v->env);

    s_alloc_total++;
    return slot;
}

void voice_note_off(uint8_t note, uint8_t channel)
{
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        voice_t *v = &s_voice[i];
        if (!v->active || v->releasing) continue;
        if (v->note != note) continue;
        if (channel != 0xFF && v->channel != channel) continue;
        dsp_env_release(&v->env);
        v->releasing = true;
    }
}

void voice_all_off(uint8_t channel)
{
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        voice_t *v = &s_voice[i];
        if (!v->active || v->releasing) continue;
        if (channel != 0xFF && v->channel != channel) continue;
        dsp_env_release(&v->env);
        v->releasing = true;
    }
}

void voice_kill_all(void)
{
    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        s_voice[i].active = false;
        s_voice[i].releasing = false;
    }
}

void voice_set_channel_gain(uint8_t channel, int32_t gain_q15)
{
    if (channel > 15) return;
    if (gain_q15 < 0) gain_q15 = 0;
    if (gain_q15 > 32767) gain_q15 = 32767;
    s_chan_gain[channel] = gain_q15;
}

void voice_set_channel_pan(uint8_t channel, int8_t pan)
{
    if (channel > 15) return;
    s_chan_pan[channel] = pan;
}

void voice_retune_channel(uint8_t channel, uint16_t bend, int semitones)
{
    if (channel > 15) return;

    /*
     * 弯音值 0..16383（8192 居中）→ 音分偏移。
     *   cents = (bend - 8192) * semitones * 100 / 8192
     *
     * ★ 用 32 位中间量：(bend-8192) 最大 ±8192，× semitones(2) × 100 = ±1638400，
     *   int32 装得下。写成 16 位会在最大弯音处溢出。
     *
     * ★ 直接用整数截断而不是四舍五入：最大误差 1 音分，
     *   远低于可辨阈（5 音分），不值得多一次加法和移位。
     */
    const int32_t cents =
        ((int32_t)bend - 8192) * (int32_t)semitones * 100 / 8192;

    for (uint32_t i = 0; i < VOICE_MAX; i++) {
        voice_t *v = &s_voice[i];
        if (!v->active || v->channel != channel) continue;

        const gmss_zone_t *z = v->zone;
        uint32_t step = dsp_pitch_step(v->note, z->root_key,
                                       (int)z->tune_cents + (int)cents);
        if (step == 0) step = 1;
        v->step_q16 = step;
    }
}

/* ================================================================== */
/* 渲染                                                                */
/* ================================================================== */

/*
 * 推进一个 voice 一个采样点。
 *
 * 入参 out_gain 回传**本采样点应当使用的 Q15 总增益**
 * （= 通道增益 × zone 增益 × 力度 × 包络电平），在推进入口处采样。
 *
 * ★ 为什么把增益一起回传，而不是让调用方读 dsp_env_level()：
 *   那样读到的会是**推进后**的包络值，包络与采样点错开一拍。
 *   单看一个采样点听不出来，但它会让"包络从 0 精确起音"这个性质失效，
 *   主机测试里做逐样本比对时也会对不上。
 *
 * 返回本采样点的插值输出。返回后 voice 已推进到下一个输出采样点。
 */
static inline int16_t voice_step(voice_t *v, int32_t *out_gain)
{
    const gmss_zone_t *z = v->zone;

    /* --- 1. 取当前点（推进前的位置与包络）--- */
    const uint32_t frac = (uint32_t)(v->pos_q16 & 0xFFFFu);
    const int16_t s = dsp_lerp(v->s0, v->s1, frac);

    /* 总增益 = 通道增益 × voice 增益（力度×zone） × 包络电平 */
    const int32_t env = dsp_env_level(&v->env);
    *out_gain = dsp_mul_q15(dsp_mul_q15(v->gain, s_chan_gain[v->channel]), env);

    /* --- 2. 推进位置 --- */
    v->pos_q16 += v->step_q16;

    /* --- 3. 循环 / 结束判断 --- */
    const bool looped = ((z->flags & GMSS_ZF_LOOPED) != 0) &&
                        (z->loop_start != GMSS_LOOP_NONE) &&
                        (z->loop_end > z->loop_start) &&
                        (z->loop_end <= z->sample_len);

    uint32_t f = (uint32_t)(v->pos_q16 >> 16);

    if (looped && f >= z->loop_end) {
        /*
         * ★ 回绕：位置减掉一个循环长度，**并且必须重新 seek 解码器**。
         *   只改 pos 不改解码器状态的话，ADPCM 状态还停在 loop_end 附近，
         *   解出来的是垃圾 —— 听感是循环点上一声"啪"再接一小段噪声。
         *
         * ★ 回绕后要把 f 夹回 [loop_start, loop_end)：
         *   升调（step > 1）时一次可能跨过整个循环段，
         *   单纯减一个循环长度仍可能落在 loop_end 之外。
         */
        const uint32_t back = z->loop_end - z->loop_start;
        const int64_t np = (int64_t)v->pos_q16 - ((int64_t)back << 16);
        v->pos_q16 = (np < 0) ? 0 : (uint64_t)np;

        f = (uint32_t)(v->pos_q16 >> 16);
        if (f >= z->loop_end) f = z->loop_start;      /* 极端升调时兜底 */
        if (f < z->loop_start) f = z->loop_start;

        /* 把位置对齐到夹紧后的帧号，保证 pos 与解码器一致 */
        v->pos_q16 = ((uint64_t)f << 16) | (v->pos_q16 & 0xFFFFu);
        voice_seek(v, f);
    } else if (!looped && f >= z->sample_len) {
        /*
         * 非循环采样播完了 → 立即结束。
         *
         * ★ 这里不触发 release 是有意的：GM 的 one-shot 采样都被裁到
         *   接近 0 结尾，直接停不会有可闻的咔哒；而触发 release
         *   会让一个已经没声音的 voice 白占几秒钟 slot。
         *   如果将来发现某些采样结尾不为 0，改成"2ms 强制淡出"即可。
         */
        v->active = false;
        return s;
    }

    /* --- 4. 滑动插值窗口 --- */
    voice_slide_to(v, (uint32_t)(v->pos_q16 >> 16));

    /* --- 5. 推进包络 --- */
    (void)dsp_env_tick(&v->env);
    if (!dsp_env_is_active(&v->env)) {
        v->active = false;
    }

    return s;
}

void voice_render(dsp_bus_t *bus, uint32_t frames)
{
    for (uint32_t n = 0; n < frames; n++) {
        for (uint32_t i = 0; i < VOICE_MAX; i++) {
            voice_t *v = &s_voice[i];
            if (!v->active) continue;

            int32_t g;
            const int16_t s = voice_step(v, &g);

            if (g > 0) {
                dsp_bus_mix(bus, s, g, v->pan);
            }
        }
    }
}

void voice_render_stereo(int16_t *lr_interleaved, uint32_t frames)
{
    /*
     * ★ 热路径。每采样点：
     *     清总线 → 遍历 48 个 voice 槽 → 混音 → 饱和 → 写出
     *
     *   总线用 int32 累加、最后一次性饱和。**不要**在每个 voice 上饱和：
     *   那会引入大量非线性失真，而且 48 复音下饱和操作的开销翻倍。
     */
    for (uint32_t n = 0; n < frames; n++) {
        dsp_bus_t bus;
        dsp_bus_clear(&bus);

        for (uint32_t i = 0; i < VOICE_MAX; i++) {
            voice_t *v = &s_voice[i];
            if (!v->active) continue;

            int32_t g;
            const int16_t s = voice_step(v, &g);
            if (g > 0) {
                dsp_bus_mix(&bus, s, g, v->pan);
            }
        }

        lr_interleaved[n * 2 + 0] = dsp_bus_pop_l(&bus);
        lr_interleaved[n * 2 + 1] = dsp_bus_pop_r(&bus);
    }
}

void voice_render_silent(uint32_t frames)
{
    for (uint32_t n = 0; n < frames; n++) {
        int32_t g;
        for (uint32_t i = 0; i < VOICE_MAX; i++) {
            if (s_voice[i].active) (void)voice_step(&s_voice[i], &g);
        }
    }
}
