/*
 * voice.h — 复音管理（48 复音）与混音
 *
 * ★ 与 sampler.c / dsp_core.c 一样**不 include 任何 pico-sdk 头**，
 *   这样能在主机上编译做单元测试。硬件相关的部分全在 engine.c / main.c。
 *
 * 一个 voice 的生命周期
 * ---------------------
 *   note_on  → 分配 voice、seek 到采样起点、包络进 ATTACK
 *   播放中   → 每输出采样点推进相位、取插值、乘包络与增益、混入总线
 *   循环     → 位置越过 loop_end 就回绕到 loop_start（重新 seek 解码器）
 *   note_off → 包络进 RELEASE
 *   包络归零 → 释放 voice
 *
 * ★★ 相位与位置的关系（这里最容易错）
 *   pos_q16 是**帧号**的 Q16.16 定点数：
 *       frame = pos_q16 >> 16        （整数帧号）
 *       frac  = pos_q16 & 0xFFFF     （帧内插值系数）
 *   推进：pos_q16 += step_q16，step_q16 是"每个输出采样前进多少帧"，
 *   由 dsp_pitch_step() 给出（Q16.16，1.0 = 0x10000）。
 *
 *   插值需要 frame(F) 和 frame(F+1) 两个点，所以 voice 缓存
 *   (s0, s1) = (frame(F), frame(F+1)) 这个滑动窗口。
 *   ★ 不是"缓存当前和下一个"那么简单：一开始写成了 s0=frame(F)、
 *     s1=frame(F-1)（顺序反了），插值系数就作用在了左端点之外，
 *     听感是高频毛刺。滑动窗口的左端点必须**正好是 floor(pos)**。
 *
 * ★ 关于爆音（click）的两个来源：
 *   1. 循环回绕 —— 回绕后必须**重新 seek 解码器**，不能只改 pos。
 *      ADPCM 是有状态的编码，位置跳了状态不跳就会解出垃圾。
 *      已处理，见 voice.c 的 voice_seek。
 *   2. 抢声部 —— 当前**不做淡出**，直接复用 slot。
 *      原因是一个 slot 同时只能装一个音，淡出需要旧音继续跑，
 *      而 slot 已经被新音占了。缓解手段是新音的包络从 0 起。
 *      48 复音对 GM 独奏/小编制基本不会真的抢；真要无咔哒抢占，
 *      得改成每 slot 双缓冲，内存和混音复杂度都翻倍，当前不划算。
 */
#ifndef VOICE_H
#define VOICE_H

#include <stdint.h>
#include <stdbool.h>

#include "gmss_format.h"
#include "sampler.h"
#include "dsp_core.h"
#include "board_config.h"     /* ★ 需要 BOARD_MAX_VOICES */

/* 复音数 —— 单一来源是 board_config.h 的 BOARD_MAX_VOICES。
 *
 * ★★★ 这里原来写的是 `#define VOICE_MAX 48u`（硬编码），
 *     而上面的注释却写着"与 board_config.h 的 BOARD_MAX_VOICES 保持一致"。
 *     **注释是对的，代码没接上。**
 *
 *     后果：现场做"降复音数能不能减轻卡顿"的实验时，
 *     把 BOARD_MAX_VOICES 从 48 改成 24、又改成 8，
 *     **声部表始终是 s_voice[48]** —— 三次实验全是空操作，
 *     却得出了"降复音没用"的错误结论。
 *
 *     现在真正接上。要改复音数只改 board_config.h 那一处。
 *
 * 实测参考（用数码管显示活跃声部数测出来的）：
 *     20 个左右同时发声开始出现卡顿，说明这台机器的实际承载上限约 20。
 *     所以 BOARD_MAX_VOICES 取 16，留一点余量。 */
#ifndef VOICE_MAX
#define VOICE_MAX  BOARD_MAX_VOICES
#endif

/* ------------------------------------------------------------------ */
/* 单个复音                                                            */
/* ------------------------------------------------------------------ */
typedef struct {
    bool     active;
    bool     releasing;          /* 已收到 note-off */
    uint8_t  note;               /* MIDI 音符 */
    uint8_t  channel;            /* 0..15 */
    uint8_t  vel;                /* 1..127 */
    uint32_t age;                /* 分配序号，越大越新；抢占时挑最小的 */

    const gmss_zone_t *zone;
    gmss_stream_t stream;

    /* 插值滑动窗口：s0 = frame(frame_idx)，s1 = frame(frame_idx + 1) */
    int16_t  s0;
    int16_t  s1;
    uint32_t frame_idx;

    uint64_t pos_q16;            /* 播放位置，Q16.16 帧 */
    uint32_t step_q16;           /* 每输出采样的帧增量，Q16.16 */

    dsp_env_t env;
    int32_t  gain;               /* Q15：力度 × zone 层间增益（不含包络与通道） */
    int8_t   pan;                /* -128..127 */

    uint16_t pad;                /* 显式占位，避免结构体尺寸随编译器变化 */
} voice_t;

/* ------------------------------------------------------------------ */
/* 初始化与状态                                                        */
/* ------------------------------------------------------------------ */

void voice_init_all(void);

/* 活跃复音数（含正在释放的） */
uint32_t voice_active_count(void);

/* 统计：累计分配次数、因无空闲而抢占的次数（用于评估复音是否够用） */
uint64_t voice_alloc_total(void);
uint64_t voice_steal_total(void);

/* ------------------------------------------------------------------ */
/* 触发                                                               */
/* ------------------------------------------------------------------ */

/*
 * 在 zone 上起一个音。
 *
 * zone        : sampler_select_zone() 选出来的分区（内部保存指针，
 *               调用方必须保证它指向的内存一直有效 —— 本工程里
 *               音色库是内存映射的只读数据，天然满足）
 * note        : MIDI 音符（用于算音高；鼓组传 fixed_note）
 * vel         : 1..127
 * channel     : 0..15（用于 note_off 匹配与声像）
 * gain_q15    : 通道级增益（音量 × expression），Q15
 * pan         : -128..127
 *
 * 返回分配到的 voice 下标；-1 表示失败（zone 为空或采样长度为 0）。
 */
int voice_note_on(const gmss_zone_t *zone, uint8_t note, uint8_t vel,
                  uint8_t channel, int32_t gain_q15, int8_t pan);

/*
 * 释放某个通道上的某个音（或全部通道，channel == 0xFF）。
 * 只影响已发声的 voice；不存在的音静默忽略。
 */
void voice_note_off(uint8_t note, uint8_t channel);

/* 立即释放某个通道上的所有音（channel == 0xFF 表示全部） */
void voice_all_off(uint8_t channel);

/* 立即杀掉所有音（不淡出）—— 只在 panic / 重新加载音色库时用 */
void voice_kill_all(void);

/*
 * 更新某个通道的增益/声像（收到 CC7/CC10/CC11 时调）。
 * 只影响该通道**当前正在发声**的 voice；已经起音的力度不受影响。
 */
void voice_set_channel_gain(uint8_t channel, int32_t gain_q15);
void voice_set_channel_pan(uint8_t channel, int8_t pan);

/*
 * 重算某通道所有正在发声的 voice 的音高（收到弯音轮时调）。
 *
 * ★ 弯音是**整条通道**的属性，不是单个音符的：已经在响的音也要跟着弯，
 *   否则只有后按下的音会弯，听起来像出了故障。
 *   实现上不需要给 voice 存"当前弯音"状态 —— 直接把弯音折算成音分，
 *   叠加到 zone 的 tune_cents 上重算 step_q16 即可，幂等且无累积误差。
 *
 * bend      : 14 位弯音值，8192 = 居中
 * semitones : 弯音范围（GM 默认 ±2 半音）
 */
void voice_retune_channel(uint8_t channel, uint16_t bend, int semitones);

/* ------------------------------------------------------------------ */
/* 渲染                                                               */
/* ------------------------------------------------------------------ */

/*
 * 渲染 frames 个采样点到总线。
 *
 * ★ 推进顺序（每一步都有理由，不要"优化"掉）：
 *     1. 算插值采样      ← 必须用**推进前**的 pos/包络
 *     2. 混入总线
 *     3. 推进 pos_q16
 *     4. 若整数帧号变了 → 滑动窗口、必要时解码
 *     5. 循环检查 / 结束检查
 *     6. 推进包络
 *
 *   把 3 放在 1 之前会整体偏移一个采样点；
 *   把 6 放在 2 之前会让包络比采样点早一拍。
 */
void voice_render(dsp_bus_t *bus, uint32_t frames);

/*
 * ★ 真正的热路径：渲染 frames 个采样点，直接输出交错的 int16 立体声。
 *
 *   比"每采样点调一次 voice_render(&bus,1)"快得多 ——
 *   后者每个点都要重新进函数、清总线，48kHz 下每秒多十万次调用。
 *   engine_render() 就是转发到这里的。
 */
void voice_render_stereo(int16_t *lr_interleaved, uint32_t frames);

/*
 * 只推进包络与位置、不混音（用于"测量最高复音数"之类的离线分析）。
 * 正常播放用 voice_render()。
 */
void voice_render_silent(uint32_t frames);

#endif /* VOICE_H */
