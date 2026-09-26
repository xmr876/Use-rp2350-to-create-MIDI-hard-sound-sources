/*
 * dsp_core.h — 定点 DSP 核心（纯算法，无硬件依赖）
 *
 * 设计约束（全部来自可行性文档 §3.2 的 CPU 预算）：
 *   150 MHz / 48 kHz = 3125 周期每采样点，48 复音 → 每音色 65 周期
 *   所以：不用浮点、不用除法、不调用库函数
 *
 * 数值格式约定（全工程统一，别混）：
 *   采样值       int16_t    -32768..32767
 *   增益/包络    q15_t      -32768..32767 表示 -1.0..~1.0（32767 ≈ 1.0）
 *   相位/步进    uint32_t   32 位无符号，高 16 位是整数部分 → 0..65535.9999
 *   音高比率     uint32_t   同上，1.0 = 0x00010000
 *
 * 本文件**不 include 任何 pico-sdk 头**，这样可以在 PC 上用普通 C 编译器
 * 直接编译做单元测试。硬件相关的东西一律放在别的模块里。
 */
#ifndef DSP_CORE_H
#define DSP_CORE_H

#include <stdint.h>
#include <stdbool.h>

typedef int16_t q15_t;

/* 采样值饱和到 int16 */
static inline int16_t dsp_sat16(int32_t v)
{
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return (int16_t)v;
}

/* ------------------------------------------------------------------ */
/* Q15 定点乘法                                                        */
/* ------------------------------------------------------------------ */
/*
 * (a * b) >> 15，带四舍五入。
 *
 * ★ 注意 -32768 * -32768 = 2^30 会溢出 int32 吗？不会（2^30 < 2^31）。
 *   但 a*b 最大是 32768*32767 ≈ 2^30，加上 0x4000 后仍安全。
 * ★ 用 int32_t 中间量，别用 int16_t —— 那会截断成垃圾。
 */
static inline int32_t dsp_mul_q15(int32_t a, int32_t b)
{
    return (a * b + 0x4000) >> 15;
}

/* 无四舍五入版（快一点，用在包络等对精度不敏感处） */
static inline int32_t dsp_mul_q15_fast(int32_t a, int32_t b)
{
    return (a * b) >> 15;
}

/* ------------------------------------------------------------------ */
/* 移位除法（避免真正的除法指令）                                       */
/* ------------------------------------------------------------------ */

/* v / 3，用于 3 片 Flash 交错等场合 */
static inline uint32_t dsp_div3(uint32_t v)
{
    /* v/3 = (v * 0xAAAB) >> 17，对 0..65535 范围精确 */
    return (v * 0xAAABu) >> 17;
}

/* ------------------------------------------------------------------ */
/* 线性插值重采样                                                      */
/* ------------------------------------------------------------------ */
/*
 * 相位格式：32 位，高 16 位 = 整数采样索引，低 16 位 = 小数部分
 *
 *   position = (index << 16) | frac
 *
 * 取两个相邻采样做线性插值：
 *   out = s[i] + ((s[i+1] - s[i]) * frac) >> 16
 *
 * ★ 用 64 位中间量避免溢出：diff 最大 65535，frac 最大 65535，
 *   乘积最大约 2^32，用 int64 稳妥。M33 上 64 位乘法是 1~2 周期，
 *   比两次 32 位乘法拼接简单得多。
 */
static inline int16_t dsp_lerp(int16_t s0, int16_t s1, uint32_t frac)
{
    int32_t diff = (int32_t)s1 - (int32_t)s0;
    int64_t acc = (int64_t)diff * (int64_t)frac;
    int32_t delta = (int32_t)(acc >> 16);
    return dsp_sat16((int32_t)s0 + delta);
}

/*
 * 4 点 Hermite 插值（音质更好，但约 2 倍开销）
 *
 * 系数用 Catmull-Rom 形式，全定点。仅在 CPU 有余量时对高音区启用。
 */
static inline int16_t dsp_hermite(int16_t sm1, int16_t s0, int16_t s1, int16_t s2,
                                  uint32_t frac)
{
    int32_t f = (int32_t)(frac >> 16);          /* 0..65535 的高 16 位当 0..1 */
    /* 用 Q15 系数近似，避免浮点 */
    int32_t c0 = sm1, c1 = s0, c2 = s1, c3 = s2;

    /* Catmull-Rom:
     *   p(t) = 0.5 * ( (2*c1) + (-c0+c2)*t
     *                + (2*c0-5*c1+4*c2-c3)*t^2
     *                + (-c0+3*c1-3*c2+c3)*t^3 )
     * 用 >>1 代替 *0.5，t 用 Q15
     */
    int32_t t = f;                              /* Q15 近似 */
    int32_t t2 = (t * t) >> 15;                 /* Q15: t^2 (>>15 补回) */
    int32_t t3 = (t2 * t) >> 15;

    int32_t a = (c2 - c0);
    int32_t b = 2 * c0 - 5 * c1 + 4 * c2 - c3;
    int32_t c = -c0 + 3 * c1 - 3 * c2 + c3;

    /* 各项定标到 Q15 后相加 */
    int32_t acc = (2 * c1) << 15;               /* 常数项 */
    acc += a * t;
    acc += (b * t2) >> 15;
    acc += (c * t3) >> 15;

    return dsp_sat16(acc >> 16);
}

/* ------------------------------------------------------------------ */
/* 包络：分段线性 ADSR                                                 */
/* ------------------------------------------------------------------ */
/*
 * ★ 为什么不每采样点更新包络：
 *   每点更新需要比较 + 加法 + 乘法，48 复音下要吃掉可观周期。
 *   做法：包络每 DSP_ENV_UPDATE_INTERVAL 个采样点更新一次。
 *
 * ★ 为什么用 16.16 定点而不是整数增量：
 *   第一版用「整数斜率 = 32767 / (ms*3)」做增量，长时段误差极大——
 *   实测 attack 2000ms 实际 2184ms、decay 5000ms 实际 8192ms（超 64%）。
 *   原因是整数除法截断，每步少走一点，累积成秒级误差。
 *   改成定点后所有时长误差都在 1 个更新周期（0.33ms）以内。
 *
 * 电平格式：level_q16 是 Q15.16 —— 高 16 位是 Q15 电平，低 16 位是小数。
 *           对外暴露的电平 = level_q16 >> 16。
 */
#define DSP_ENV_UPDATE_INTERVAL 16

typedef enum {
    ENV_IDLE = 0,
    ENV_ATTACK,
    ENV_DECAY,
    ENV_SUSTAIN,
    ENV_RELEASE,
} dsp_env_stage_t;

typedef struct {
    dsp_env_stage_t stage;
    int32_t level_q16;      /* Q15.16 当前电平，0 .. 32767<<16 */
    int32_t slope_q16;      /* 每个更新周期的增量，Q15.16（可为负）*/
    int32_t sustain_q16;    /* Q15.16 目标保持电平 */
    uint32_t counter;       /* 距离下次更新还有几个采样点 */
    /* 各段斜率（预先算好，运行时不做除法） */
    int32_t attack_step;
    int32_t decay_step;
    int32_t release_step;
} dsp_env_t;

/*
 * 用毫秒参数初始化包络。
 * 返回 0 成功；1 表示有参数被夹紧（调用方可以打个日志，但不是错误）。
 */
int dsp_env_init(dsp_env_t *e, uint16_t attack_ms, uint16_t decay_ms,
                 int32_t sustain, uint16_t release_ms);

void dsp_env_trigger(dsp_env_t *e);     /* note-on */
void dsp_env_release(dsp_env_t *e);     /* note-off */
bool dsp_env_is_active(const dsp_env_t *e);

/* 当前电平（Q15，0..32767） */
static inline int32_t dsp_env_level(const dsp_env_t *e)
{
    return e->level_q16 >> 16;
}

/*
 * 推进包络一个采样点，返回当前电平（Q15）。
 * 热路径：只有减法、条件分支和一次移位。
 */
static inline int32_t dsp_env_tick(dsp_env_t *e)
{
    if (e->counter == 0) {
        e->counter = DSP_ENV_UPDATE_INTERVAL;
        switch (e->stage) {
        case ENV_ATTACK:
            /*
             * ★★ 必须"先判断再加"，不能"加完再判断"。
             *
             *   写成 `level += slope; if (level >= TARGET) { level = TARGET; ... }`
             *   有一个致命情况：如果 slope 不能整除 TARGET，最后一次加法会
             *   落在目标**下方** 1 个 LSB（实测 2147418111，而目标是 2147418112）。
             *   判断不成立 → stage 不切换 → 继续加 → **下一次直接溢出 int32**。
             *
             *   溢出后 level_q16 变成一个大负数，包络电平成了负值，
             *   乘以采样值会让输出整段反相满幅 ——
             *   听感是"起音时一声爆音，然后这个音就哑了"。
             *
             *   这个缺陷在 ARM 上是静默环绕（不报错），
             *   是主机测试里 zig 的整数溢出检查抓出来的：
             *       panic: signed integer overflow: 2147418111 + 715806037
             *     —— 见 tools/verify_engine_host.py
             *
             *   改成"再加一步会不会到/超过目标"就没有这个问题：
             *   slope 恒正，level 恒 <= 目标，加法不可能溢出。
             */
            if (e->level_q16 >= (32767 << 16) - e->slope_q16) {
                e->level_q16 = (32767 << 16);
                e->stage = ENV_DECAY;
                e->slope_q16 = e->decay_step;
            } else {
                e->level_q16 += e->slope_q16;
            }
            break;
        case ENV_DECAY:
            e->level_q16 += e->slope_q16;
            if (e->level_q16 <= e->sustain_q16) {
                e->level_q16 = e->sustain_q16;
                e->stage = ENV_SUSTAIN;
                e->slope_q16 = 0;
            }
            break;
        case ENV_SUSTAIN:
            /* 保持，什么都不做 */
            break;
        case ENV_RELEASE:
            e->level_q16 += e->slope_q16;
            if (e->level_q16 <= 0) {
                e->level_q16 = 0;
                e->stage = ENV_IDLE;
                e->slope_q16 = 0;
            }
            break;
        case ENV_IDLE:
        default:
            e->level_q16 = 0;
            break;
        }
    }
    e->counter--;
    return e->level_q16 >> 16;
}

/* ------------------------------------------------------------------ */
/* 一阶低通滤波（用于音色亮度控制的简化模型）                           */
/* ------------------------------------------------------------------ */
/*
 * y[n] = y[n-1] + a * (x[n] - y[n-1])，a 为 Q15
 * 一阶足够表达 GM 音色的"亮度"差异，二阶的音质收益不值得那 2 倍开销。
 */
typedef struct {
    int32_t z;              /* 状态 */
    int32_t a;              /* Q15 系数，0..32767 */
} dsp_onepole_t;

static inline void dsp_onepole_reset(dsp_onepole_t *f)
{
    f->z = 0;
}

static inline int32_t dsp_onepole_tick(dsp_onepole_t *f, int32_t x)
{
    f->z += dsp_mul_q15(f->a, x - f->z);
    return f->z;
}

/* ------------------------------------------------------------------ */
/* 立体声混音总线                                                      */
/* ------------------------------------------------------------------ */
/*
 * 用 int32 累加，最后一次性饱和到 int16。
 * ★ 不要在每个音色上做饱和 —— 那会引入大量非线性失真，
 *   而且在 48 复音下开销翻倍。统一在总线输出处饱和。
 */
typedef struct {
    int32_t l;
    int32_t r;
} dsp_bus_t;

static inline void dsp_bus_clear(dsp_bus_t *b)
{
    b->l = 0;
    b->r = 0;
}

/*
 * 把单声道采样按声像混入总线。
 * pan: -128(全左) .. +127(全右)，0 = 居中
 * gain: Q15 总增益（包络 × 力度 × 通道音量）
 *
 * 等功率声像用近似式：
 *   left  = gain * (128 - pan) / 256
 *   right = gain * (pan + 128) / 256
 * 这是线性声像，不是等功率；听感上够用，且省掉一次开方。
 */
static inline void dsp_bus_mix(dsp_bus_t *b, int16_t sample, int32_t gain, int pan)
{
    int32_t g;
    int32_t gl, gr;

    g = dsp_mul_q15(gain, sample);

    /* pan 夹紧到 [-128, 127] */
    if (pan < -128) pan = -128;
    if (pan > 127) pan = 127;

    gl = (g * (128 - pan)) >> 8;
    gr = (g * (pan + 128)) >> 8;

    b->l += gl;
    b->r += gr;
}

/* 总线输出：饱和到 int16 并打包成 I2S 帧 */
static inline int16_t dsp_bus_pop_l(const dsp_bus_t *b)
{
    return dsp_sat16(b->l);
}

static inline int16_t dsp_bus_pop_r(const dsp_bus_t *b)
{
    return dsp_sat16(b->r);
}

/* ------------------------------------------------------------------ */
/* 工具：从 dB(×100) 转 Q15 线性增益                                   */
/* ------------------------------------------------------------------ */
/*
 * 用于把 zone 的 gain_db100 转成播放增益。
 * 用查表 + 线性插值，避免运行时 pow()。
 * 表见 dsp_core.c；覆盖 -96dB .. +12dB。
 */
int32_t dsp_db100_to_q15(int32_t db100);

/* 从 MIDI 音符与根音算音高步进（Q16.16）
 *
 *   step = 2^((note - root_key + tune_cents/100) / 12)
 *
 * 内部用定点 pow2 实现（查表 + 插值），约 20 周期。
 */
uint32_t dsp_pitch_step(int note, int root_key, int tune_cents);

/* MIDI 力度 → Q15 增益（GM 常用曲线：gain = (vel/127)^2 的近似） */
int32_t dsp_vel_to_q15(uint8_t vel);

#endif /* DSP_CORE_H */
