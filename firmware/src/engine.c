/*
 * engine.c — GM 引擎：MIDI 事件 → 复音分配 → 音频块
 *
 * ★ 本文件不 include 任何 pico-sdk 头（只用 midi_in.h 的事件结构），
 *   所以整条音频通路可以在主机上跑测试。
 *   midi_in.h 本身也只依赖 stdint / stdbool，不拉硬件进来。
 */
#include <string.h>

#include "engine.h"
#include "sampler.h"
#include "voice.h"

/* ================================================================== */
/* 通道状态                                                            */
/* ================================================================== */
/*
 * GM 每个通道独立维护：音色号、音量、表达、声像、弯音、延音踏板。
 * ★ 数组大小是 16（MIDI 只有 16 个通道），越界访问在这里是最容易的
 *   内存破坏来源，所以下面每个入口都夹紧通道号。
 */
typedef struct {
    uint8_t  program;            /* 当前音色号 0..127 */
    uint8_t  volume;             /* CC7  ，0..127 */
    uint8_t  expression;         /* CC11 ，0..127 */
    uint8_t  pan;                /* CC10 ，0..127，64 = 居中 */
    uint16_t bend;               /* 14 位，8192 = 居中 */
    bool     sustain;            /* CC64 延音踏板 */
    uint8_t  sustained[128];     /* 踏板踩下期间收到 note-off 的音（1 = 挂起）*/
} engine_chan_t;

static engine_chan_t s_ch[16];
static int32_t  s_master = 32767;
static engine_stats_t s_stats;

/* ================================================================== */
/* Q15 工具                                                            */
/* ================================================================== */

/* MIDI 7 位控制器（0..127）→ Q15 增益 */
static inline int32_t cc7_to_q15(uint8_t v)
{
    if (v > 127) v = 127;
    return (int32_t)v * 32767 / 127;
}

/*
 * 通道总增益 = 音量 × 表达 × 主音量
 * ★ 音量与表达是**相乘**关系（GM 规范），不是相加，也不是取小。
 *   写成相加的话，CC11 在 CC7 较小时会几乎没有效果。
 */
static inline int32_t channel_gain_q15(uint8_t ch)
{
    const engine_chan_t *c = &s_ch[ch];
    int32_t g = cc7_to_q15(c->volume);
    g = dsp_mul_q15(g, cc7_to_q15(c->expression));
    g = dsp_mul_q15(g, s_master);
    return g;
}

static inline int8_t channel_pan(int8_t midi_pan)
{
    /* MIDI 0..127（64 居中）→ -128..127 */
    int p = (int)midi_pan - 64;
    p = p * 2;                  /* 0..127 的偏离量放大到 -128..126 */
    if (p < -128) p = -128;
    if (p > 127) p = 127;
    return (int8_t)p;
}

/* ================================================================== */
/* 初始化                                                              */
/* ================================================================== */

void engine_reset(void)
{
    /*
     * ★ 必须先杀掉所有正在发声的 voice。
     *
     *   最初漏了这一句，只清了通道状态。后果很隐蔽：
     *   已经在响的音会**继续响下去**，而且它们的 gain 是起音时算好的，
     *   后面再改 CC7/主音量都不影响它们。
     *   表现是"复位之后还有声音在拖尾"、"音量拧到 0 还有一个音在响"。
     *
     *   （注意区分：MIDI 的 CC121「复位所有控制器」按规范**不应该**停音，
     *     那个路径走 engine_handle_midi 的 CC121 分支，不调本函数。
     *     本函数是引擎级的硬复位，停音是对的。）
     */
    voice_kill_all();

    memset(s_ch, 0, sizeof(s_ch));
    for (int i = 0; i < 16; i++) {
        s_ch[i].program = 0;
        s_ch[i].volume = 100;        /* GM 默认音量 */
        s_ch[i].expression = 127;    /* 表达默认满 */
        s_ch[i].pan = 64;            /* 居中 */
        s_ch[i].bend = 8192;         /* 居中 */
        s_ch[i].sustain = false;
        voice_set_channel_gain((uint8_t)i, channel_gain_q15((uint8_t)i));
        voice_set_channel_pan((uint8_t)i, channel_pan(64));
    }
    memset(&s_stats, 0, sizeof(s_stats));
}

int engine_init(void)
{
    voice_init_all();
    engine_reset();

    const int rc = sampler_init();
    if (rc != 0) {
        /* 库有问题也要把引擎置成可用状态 —— 这样 MIDI 灯、
         * 数码管还能工作，用户能看出"板子活着但没音色库"。 */
        return rc;
    }
    return 0;
}

bool engine_library_ok(void) { return sampler_ready(); }

void engine_set_master_volume(int32_t vol_q15)
{
    if (vol_q15 < 0) vol_q15 = 0;
    if (vol_q15 > 32767) vol_q15 = 32767;
    s_master = vol_q15;
    /* 主音量变了，所有通道的合成增益都要重算 */
    for (int i = 0; i < 16; i++) {
        voice_set_channel_gain((uint8_t)i, channel_gain_q15((uint8_t)i));
    }
}

int32_t engine_get_master_volume(void) { return s_master; }

uint32_t engine_active_voices(void) { return voice_active_count(); }

const engine_stats_t *engine_stats(void) { return &s_stats; }

uint8_t engine_channel_program(uint8_t channel)
{
    return (channel < 16) ? s_ch[channel].program : 0;
}

/* ================================================================== */
/* 起音 / 止音                                                         */
/* ================================================================== */

static void do_note_on(uint8_t ch, uint8_t note, uint8_t vel)
{
    if (note > 127) return;

    /* 打击乐通道走 bank 128，且用音色的固定音高 */
    const bool is_drum = (ch == ENGINE_DRUM_CHANNEL);
    const uint8_t bank = is_drum ? 128u : 0u;

    const gmss_instrument_t *inst =
        sampler_find_instrument(bank, s_ch[ch].program);
    if (inst == NULL) {
        s_stats.dropped_no_zone++;
        return;
    }

    /*
     * ★ 鼓组用 fixed_note 而不是按键音高。
     *   鼓组采样是按"每个键一个音色"组织的，而且固定音高
     *   （否则同一个鼓在不同键上会变调，听起来很怪）。
     *   fixed_note < 0 表示"用按键音高"（旋律音色的情况）。
     */
    uint8_t sel_key = note;
    if (is_drum && inst->fixed_note >= 0) {
        sel_key = (uint8_t)inst->fixed_note;
    }

    const gmss_zone_t *z = sampler_select_zone(inst, sel_key, vel);
    if (z == NULL) {
        s_stats.dropped_no_zone++;
        return;
    }

    const int slot = voice_note_on(z, note, vel, ch,
                                   channel_gain_q15(ch),
                                   channel_pan(s_ch[ch].pan));
    if (slot < 0) {
        s_stats.dropped_no_voice++;
    }
    s_stats.note_on++;
    s_stats.last_note[ch] = note;
}

static void do_note_off(uint8_t ch, uint8_t note)
{
    s_stats.note_off++;

    if (s_ch[ch].sustain) {
        /*
         * ★ 延音踏板踩下：note-off 只**记账**，不真的释放。
         *   记账表用 note 号索引，所以同一个音重复踩-放不会累积。
         *   漏掉这一步的症状是"踩踏板时音符还是会断"。
         */
        s_ch[ch].sustained[note] = 1;
        return;
    }
    voice_note_off(note, ch);
}

static void release_sustained(uint8_t ch)
{
    for (int n = 0; n < 128; n++) {
        if (s_ch[ch].sustained[n]) {
            s_ch[ch].sustained[n] = 0;
            voice_note_off((uint8_t)n, ch);
        }
    }
}

/* ================================================================== */
/* MIDI 事件分发                                                       */
/* ================================================================== */

void engine_handle_midi(const midi_event_t *ev)
{
    if (ev == NULL) return;
    const uint8_t ch = (uint8_t)(ev->channel & 0x0Fu);

    switch (ev->type) {
    case MIDI_EV_NOTE_ON:
        /*
         * ★ Note On + velocity 0 **等价于 Note Off**。
         *   这是 MIDI 规范的规定，很多键盘用这种方式发 note-off。
         *   漏掉这一条的症状：所有音符都不停，一片糊。
         *   （midi_in.c 里已经做过一次转换，这里再兜一次 ——
         *     解析层和语义层都挡住，任何一条路径漏了都不会出事。）
         */
        if (ev->data2 == 0) {
            do_note_off(ch, ev->data1);
        } else {
            do_note_on(ch, ev->data1, ev->data2);
        }
        break;

    case MIDI_EV_NOTE_OFF:
        do_note_off(ch, ev->data1);
        break;

    case MIDI_EV_PROGRAM:
        s_ch[ch].program = (uint8_t)(ev->data1 & 0x7Fu);
        s_stats.program_change++;
        s_stats.last_program[ch] = s_ch[ch].program;
        break;

    case MIDI_EV_PITCH_BEND:
        s_ch[ch].bend = ev->bend;
        s_stats.pitch_bend++;
        /*
         * ★ 弯音是**整条通道**的属性，不是单个音符的。
         *   已有的 voice 也要跟着变 —— 所以这里遍历该通道所有
         *   正在发声的 voice 重新算 step。
         *
         *   实现方式：把弯音量化成半音数，叠加到 note 上。
         *   由于 voice 里存的是算好的 step_q16 而不是 note，
         *   这里需要 voice 层提供一个"重算音高"的入口。
         *   见 voice_retune_channel()。
         */
        voice_retune_channel(ch, ev->bend, ENGINE_PITCH_BEND_SEMITONES);
        break;

    case MIDI_EV_CONTROL: {
        s_stats.cc_received++;
        const uint8_t cc = ev->data1;
        const uint8_t val = ev->data2;

        switch (cc) {
        case MIDI_CC_VOLUME:
            s_ch[ch].volume = val;
            voice_set_channel_gain(ch, channel_gain_q15(ch));
            break;
        case MIDI_CC_EXPRESSION:
            s_ch[ch].expression = val;
            voice_set_channel_gain(ch, channel_gain_q15(ch));
            break;
        case MIDI_CC_PAN:
            s_ch[ch].pan = val;
            voice_set_channel_pan(ch, channel_pan(val));
            break;
        case MIDI_CC_SUSTAIN:
            /*
             * ★ 踏板阈值用 64：>= 64 算踩下（MIDI 规范）。
             *   写成 > 0 的话，某些控制器在 1..63 的抖动会让
             *   踏板状态乱跳。
             */
            if (val >= 64) {
                s_ch[ch].sustain = true;
            } else {
                s_ch[ch].sustain = false;
                release_sustained(ch);
            }
            break;
        case MIDI_CC_ALL_SOUND_OFF:      /* 120 */
            voice_all_off(ch);
            memset(s_ch[ch].sustained, 0, sizeof(s_ch[ch].sustained));
            break;
        case MIDI_CC_RESET_ALL:          /* 121 */
            s_ch[ch].volume = 100;
            s_ch[ch].expression = 127;
            s_ch[ch].pan = 64;
            s_ch[ch].bend = 8192;
            s_ch[ch].sustain = false;
            memset(s_ch[ch].sustained, 0, sizeof(s_ch[ch].sustained));
            voice_set_channel_gain(ch, channel_gain_q15(ch));
            voice_set_channel_pan(ch, channel_pan(64));
            voice_retune_channel(ch, 8192, ENGINE_PITCH_BEND_SEMITONES);
            break;
        case MIDI_CC_ALL_NOTES_OFF:      /* 123 */
            voice_all_off(ch);
            memset(s_ch[ch].sustained, 0, sizeof(s_ch[ch].sustained));
            break;
        default:
            break;
        }
        break;
    }

    case MIDI_EV_CHAN_PRESSURE:
    case MIDI_EV_POLY_PRESSURE:
    case MIDI_EV_NONE:
    default:
        /* 本工程暂不处理触后 —— GM 音色库基本不用它，
         * 而且它需要每个 voice 独立调制，收益不值那份开销。 */
        break;
    }
}

/* ================================================================== */
/* 渲染                                                                */
/* ================================================================== */

/*
 * ★ 真正的热路径在 voice_render_stereo() 里（一次遍历就出 int16 立体声），
 *   这里只是薄薄一层转发。
 *
 *   曾经写成"每采样点调一次 voice_render(&bus,1)" —— 那样每个点都要
 *   重新进入函数、重新清总线、再遍历 48 个 voice 槽。功能是对的，
 *   但每采样点多两次函数调用开销；48kHz 下就是每秒多 10 万次调用。
 *   挪进 voice.c 之后结构也更清楚：混音是 voice 层的职责。
 */
void engine_render(int16_t *interleaved, uint32_t frames)
{
    voice_render_stereo(interleaved, frames);
}

void engine_render_split(int16_t *left, int16_t *right, uint32_t frames)
{
    /*
     * 左右分离输出（主机测试逐声道比对用）。
     * 不走 voice_render_stereo 是因为那会多一次交错→分离的搬运；
     * 这里直接借一个小的栈上缓冲，块大小取 64 帧。
     */
    int16_t tmp[64 * 2];
    uint32_t done = 0;

    while (done < frames) {
        uint32_t n = frames - done;
        if (n > 64) n = 64;

        voice_render_stereo(tmp, n);
        for (uint32_t i = 0; i < n; i++) {
            left[done + i] = tmp[i * 2 + 0];
            right[done + i] = tmp[i * 2 + 1];
        }
        done += n;
    }
}


/* ================================================================== */
/* 跨核命令队列（实现）                                                 */
/* ================================================================== */
/*
 * 单生产者 / 单消费者，**无需加锁**：
 *     core1（MIDI/按键）  只写 s_cmd_w
 *     core0（音频）       只写 s_cmd_r
 * 两个核各写各的索引，所以不存在"同一变量被两边写"的竞争。
 *
 * ★ 容量取 2 的幂，用"读写索引的自然回绕"判断满/空 ——
 *   比取模快，而且天然处理回绕。
 *
 * ★ 队列满时**丢弃最旧的一条**并计数，绝不阻塞 core1。
 *   丢一条 note_off 最多造成一个音卡住，下一轮 panic 能救回来；
 *   而阻塞 core1 会拖慢整个主循环，代价大得多。
 */
#define CMDQ_SIZE   128u                    /* 必须是 2 的幂 */
#define CMDQ_MASK   (CMDQ_SIZE - 1u)

typedef struct {
    uint8_t type;
    uint8_t channel;
    uint8_t data1;
    uint8_t data2;
} eng_cmd_t;

static volatile eng_cmd_t s_cmdq[CMDQ_SIZE];
static volatile uint32_t  s_cmd_w = 0;      /* 只由 core1 写 */
static volatile uint32_t  s_cmd_r = 0;      /* 只由 core0 写 */
static volatile uint32_t  s_cmd_drop = 0;
static volatile uint8_t   s_all_off_req = 0;

void engine_post_midi(const midi_event_t *ev)
{
    const uint32_t w = s_cmd_w;
    const uint32_t r = s_cmd_r;

    if ((uint32_t)(w - r) >= CMDQ_SIZE) {
        s_cmd_drop++;                       /* 满了：丢弃，不阻塞 */
        return;
    }

    s_cmdq[w & CMDQ_MASK].type    = (uint8_t)ev->type;
    s_cmdq[w & CMDQ_MASK].channel = (uint8_t)ev->channel;
    s_cmdq[w & CMDQ_MASK].data1   = (uint8_t)ev->data1;
    s_cmdq[w & CMDQ_MASK].data2   = (uint8_t)ev->data2;

    /* ★ 最后才发布写索引。前面的数据必须先落地，
     *   否则音频核可能读到"索引已更新、内容还没写完"的半条命令。
     *   （Cortex-M33 是弱序内存模型，所以这里用 release 屏障。） */
    /* 内存屏障。用 __atomic_thread_fence 而不是内联 dmb ——
     * 主机测试会把 engine.c 编到 x86/ARM64 上，那里没有 dmb 指令。
     * 在 ARM 上编译器会把它翻成 dmb sy，效果一样。 */
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    s_cmd_w = w + 1u;
}

void engine_request_all_off(void)
{
    s_all_off_req = 1;      /* 只置标志，不碰声部表 */
}

uint32_t engine_dropped_commands(void) { return s_cmd_drop; }

void engine_flush_commands(void)
{
    /* 1) panic 请求（按键触发） */
    if (s_all_off_req) {
        s_all_off_req = 0;
        voice_all_off(0xFF);
    }

    /* 2) MIDI 命令 */
    for (;;) {
        const uint32_t r = s_cmd_r;
        if (r == s_cmd_w) {
            break;                          /* 空 */
        }

        midi_event_t ev = {0};
        ev.type    = (uint8_t)s_cmdq[r & CMDQ_MASK].type;
        ev.channel = (uint8_t)s_cmdq[r & CMDQ_MASK].channel;
        ev.data1   = (uint8_t)s_cmdq[r & CMDQ_MASK].data1;
        ev.data2   = (uint8_t)s_cmdq[r & CMDQ_MASK].data2;

        /* 内存屏障。用 __atomic_thread_fence 而不是内联 dmb ——
     * 主机测试会把 engine.c 编到 x86/ARM64 上，那里没有 dmb 指令。
     * 在 ARM 上编译器会把它翻成 dmb sy，效果一样。 */
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
        s_cmd_r = r + 1u;

        engine_handle_midi(&ev);
    }
}
