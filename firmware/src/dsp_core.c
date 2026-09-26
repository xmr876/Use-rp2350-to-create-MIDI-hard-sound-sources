/*
 * dsp_core.c — 定点 DSP 核心实现
 *
 * 本文件**不依赖任何 pico-sdk**，可以在 PC 上用普通 C 编译器编译做单元测试。
 */
#include "dsp_core.h"
#include "dsp_pitch_table.h"
#include "dsp_tables.h"

/* ================================================================== */
/* 包络                                                                */
/* ================================================================== */

/*
 * 段时长 → 每更新周期的增量（Q15.16 定点）
 *
 * 每毫秒的更新次数 = 48000 / DSP_ENV_UPDATE_INTERVAL / 1000 = 3
 * 段内总更新次数   = ms * 3
 *
 * ★ 为什么用定点而不是整数：
 *   整数版 slope = span / (ms*3) 会截断。对长时段，
 *   每次少走的那一点会累积：实测 decay 5000ms 实际走了 8192ms（超 64%）。
 *   定点版 slope_q16 = (span << 16) / (ms*3)，把小数部分保留下来，
 *   所有时长误差都收敛到 1 个更新周期（0.33ms）之内。
 *
 * ★ 这个除法只在 note-on 时执行一次，不在热路径上。
 */
#define ENV_UPDATES_PER_MS   (48000u / DSP_ENV_UPDATE_INTERVAL / 1000u)   /* = 3 */
#define ENV_ONE              (32767 << 16)   /* Q15.16 的 1.0 */

static int32_t env_slope_q16(uint16_t ms, int32_t span_q16, int32_t min_step)
{
    int32_t n;
    int32_t step;

    if (ms == 0) {
        ms = 1;
    }
    n = (int32_t)ms * (int32_t)ENV_UPDATES_PER_MS;
    if (n < 1) {
        n = 1;
    }
    step = span_q16 / n;
    if (step < 0) {
        step = -step;               /* 调用方决定符号 */
    }
    if (step < min_step) {
        step = min_step;
    }
    return step;
}

int dsp_env_init(dsp_env_t *e, uint16_t attack_ms, uint16_t decay_ms,
                 int32_t sustain, uint16_t release_ms)
{
    int32_t span_q16;
    int clamped = 0;

    if (e == 0) {
        return 0;
    }

    /* sustain 夹紧到 [0, 32767]；32767 表示不衰减 */
    if (sustain < 0) {
        sustain = 0;
        clamped = 1;
    }
    if (sustain > 32767) {
        sustain = 32767;
        clamped = 1;
    }

    /* attack 至少 1ms，否则会有咔哒声 */
    if (attack_ms < 1) {
        attack_ms = 1;
        clamped = 1;
    }
    /* release 至少 5ms，理由同上 */
    if (release_ms < 5) {
        release_ms = 5;
        clamped = 1;
    }
    if (decay_ms < 1) {
        decay_ms = 1;
        clamped = 1;
    }

    e->stage = ENV_IDLE;
    e->level_q16 = 0;
    e->slope_q16 = 0;
    e->counter = 0;
    e->sustain_q16 = sustain << 16;

    /* attack：0 → 满幅 */
    e->attack_step = env_slope_q16(attack_ms, ENV_ONE, 1);

    /* decay：满幅 → sustain */
    span_q16 = ENV_ONE - e->sustain_q16;
    e->decay_step = (span_q16 > 0)
        ? -env_slope_q16(decay_ms, span_q16, 1)
        : 0;

    /* release：从任意位置 → 0，按满幅估斜率 */
    e->release_step = -env_slope_q16(release_ms, ENV_ONE, 1);

    return clamped;
}

void dsp_env_trigger(dsp_env_t *e)
{
    if (e == 0) {
        return;
    }
    e->stage = ENV_ATTACK;
    e->level_q16 = 0;
    e->slope_q16 = e->attack_step;
    e->counter = 0;          /* 下一个采样点就更新 */
}

void dsp_env_release(dsp_env_t *e)
{
    if (e == 0) {
        return;
    }
    if (e->stage == ENV_IDLE) {
        return;              /* 已经结束，不用重复触发 */
    }
    e->stage = ENV_RELEASE;
    e->slope_q16 = e->release_step;
    e->counter = 0;
}

bool dsp_env_is_active(const dsp_env_t *e)
{
    return (e != 0) && (e->stage != ENV_IDLE);
}

/* ================================================================== */
/* 音高步进                                                            */
/* ================================================================== */

/*
 * step = 2^(semitone / 12)，Q16.16
 *
 * ★ 返回 0 表示参数非法（调用方必须检查，否则采样会以步进 0 卡住）
 *   正常范围是 2048(=-60半音) .. 3142178(=+67半音)，不会是 0。
 */
uint32_t dsp_pitch_step(int note, int root_key, int tune_cents)
{
    int32_t semitone_q4;       /* 半音数，Q4 定点（1/16 半音精度） */
    int32_t semitone_int;
    int32_t idx;
    int32_t i0;
    int32_t frac;              /* Q16 小数部分 */
    uint32_t a, b, step;

    /* 音分并入：semitone = (note - root) + tune_cents/100
     * 用 Q4 表示：× 16，所以 tune_cents * 16 / 100 = tune_cents * 4 / 25
     */
    semitone_q4 = (int32_t)(note - root_key) * PITCH_TAB_SUB
                + (int32_t)tune_cents * PITCH_TAB_SUB / 100;

    /* 这里要向下取整（负数也是），所以不能用 C 的整数除法 */
    semitone_int = semitone_q4 / PITCH_TAB_SUB;
    if (semitone_q4 < 0 && (semitone_q4 % PITCH_TAB_SUB) != 0) {
        semitone_int -= 1;
    }

    /* 反推回表索引（保证非负的小数部分） */
    idx = semitone_q4 - semitone_int * PITCH_TAB_SUB;   /* 0 .. SUB-1 */
    i0 = (semitone_int - PITCH_TAB_LO_SEMITONE) * PITCH_TAB_SUB + idx;

    /* 越界夹紧到表范围（极端参数时宁可音高不准，也不要读到表外内存） */
    if (i0 < 0) {
        i0 = 0;
    }
    if (i0 > PITCH_TAB_LEN - 2) {
        i0 = PITCH_TAB_LEN - 2;
    }

    /* 表步长已经是 1/16 半音，所以 idx 本身就是表内的小数位置 */
    frac = 0;                  /* 表的细分已覆盖，无需再插值 */
    a = k_pitch_q16[i0];
    b = k_pitch_q16[i0 + 1];

    /*
     * 因为表细分到 1/16 半音，而输入精度也是 1/16 半音，
     * idx 一定是整数 → 直接取表值即可，frac 恒为 0。
     * 保留插值代码是为了将来把 SUB 改小（省 Flash）时不用改这里。
     */
    step = a + (uint32_t)(((int64_t)((int32_t)b - (int32_t)a) * frac) >> 16);

    if (step == 0) {
        step = 0x00010000u;    /* 兜底：原速 */
    }
    return step;
}

/* ================================================================== */
/* 力度曲线                                                            */
/* ================================================================== */

/*
 * MIDI 力度 → Q15 增益
 *
 * 表在 dsp_tables.h，由 tools/gen_dsp_tables.py 生成：
 *   k_vel_q15[vel] = round((vel/127)^2 * 32767)
 *
 * 平方律是 GM 音源最常见的力度响应。
 * 表 128 项可以直接索引，不需要插值——比"64 项 + 插值"更快也更准。
 */
int32_t dsp_vel_to_q15(uint8_t vel)
{
    /* vel 一定 <= 127，但保险起见还是夹一下，防止越界读表 */
    if (vel > 127) {
        vel = 127;
    }
    return k_vel_q15[vel];
}

/* ================================================================== */
/* dB(×100) → Q15 线性增益                                             */
/* ================================================================== */

/*
 * 表在 dsp_tables.h：
 *   k_db_q15[d] = round(10^(-d/20) * 32767)，d = 0..96
 *
 * ★ 查询时向下取整到 1dB。1dB 约是刚好可辨的差异，对音色平衡足够。
 *   若将来需要更细，把生成脚本改成 0.5dB 步长即可，函数不用动。
 *
 * ★ 注意表项最小是 1 而不是 0 —— 0 会让后续乘法永久静音，
 *   衰减 96dB 已经等同静音，用 1 表示。
 */
int32_t dsp_db100_to_q15(int32_t db100)
{
    int32_t db;

    if (db100 >= 0) {
        /* 正增益在 GM 里很少见，直接给满幅避免削波 */
        return 32767;
    }

    /* db100 是负的，取绝对值后向下取整到整 dB */
    db = (-db100) / 100;
    if (db > 96) {
        db = 96;
    }
    return k_db_q15[db];
}
