/*
 * engine.h — GM 引擎：MIDI 事件 → 复音分配 → 音频块
 *
 * 分层（每一层都可以在主机上单独测试）：
 *
 *     engine.c    MIDI 语义 → 什么时候起音、多大声、哪个音色   ← 本文件
 *        ↓
 *     voice.c     48 复音、音高移位、包络、混音                （纯逻辑）
 *        ↓
 *     sampler.c   从内存映射音色库取采样、PCM/ADPCM 解码       （纯逻辑）
 *        ↓
 *     i2s.c       PIO + DMA 输出                              （硬件相关）
 *     midi_in.c   UART 收字节、解析 MIDI                      （硬件相关）
 *
 * ★ engine.c 里**没有一行硬件代码** —— 它只吃 midi_event_t、吐 int16 采样。
 *   所以整条音频通路都能在主机上跑测试，不需要开发板。
 *
 * GM 语义要点
 * -----------
 *   · 通道 9（0 基）是打击乐通道 → bank 128，用固定音高
 *     ★ 是 **0 基的 9**（也就是第 10 个通道）。写成 10 就错了，
 *       症状是所有鼓都变成旋律音色。
 *   · 鼓组音色用 instrument 的 fixed_note，而不是按键音高
 *   · Note On + velocity 0 **等价于 Note Off**（MIDI 规范）
 *   · CC7 通道音量 / CC11 表达 / CC10 声像；两者相乘
 *   · CC64 延音踏板：踩下时 note-off 只记账不释放，抬起时才真正释放
 *   · CC120/123 全部音符关闭；CC121 复位所有控制器
 *   · 弯音轮 14 位，默认 ±2 半音
 */
#ifndef ENGINE_H
#define ENGINE_H

#include <stdint.h>
#include <stdbool.h>

#include "gmss_format.h"
#include "midi_in.h"
#include "dsp_core.h"

/* 弯音范围（半音）。GM 默认 ±2 */
#ifndef ENGINE_PITCH_BEND_SEMITONES
#define ENGINE_PITCH_BEND_SEMITONES 2
#endif

/* 打击乐通道：**0 基**的第 10 个通道 */
#define ENGINE_DRUM_CHANNEL  9u

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */

/*
 * 初始化引擎。
 * 会调用 sampler_init()（校验并启用音色库）与 voice_init_all()。
 *
 * 返回 sampler_init() 的返回值（0 = 成功）。
 * ★ 库校验失败时引擎仍可用，但不会出声 —— 见 engine_library_ok()。
 */
int engine_init(void);

/* 音色库是否加载成功 */
bool engine_library_ok(void);

/* ------------------------------------------------------------------ */
/* MIDI 输入                                                           */
/* ------------------------------------------------------------------ */

/* 处理一个 MIDI 事件（来自 midi_in.c 的 midi_poll / midi_feed_byte） */
void engine_handle_midi(const midi_event_t *ev);

/* ------------------------------------------------------------------ */
/* 音频渲染                                                            */
/* ------------------------------------------------------------------ */

/*
 * 渲染 frames 个立体声帧到 interleaved（L,R,L,R…）。
 *
 * ★ 这个函数是 core0 音频路径的唯一入口，必须**不阻塞、不分配、不调用
 *   任何可能睡着的函数**。它内部只做定点运算和内存读。
 */
void engine_render(int16_t *interleaved, uint32_t frames);

/*
 * 同 engine_render，但输出到左右分离的两个 int16 数组。
 * 方便主机测试逐声道比对。
 */
void engine_render_split(int16_t *left, int16_t *right, uint32_t frames);

/* ------------------------------------------------------------------ */
/* 控制与诊断                                                          */
/* ------------------------------------------------------------------ */

/* 主音量，Q15（0..32767）。默认 32767 */
void engine_set_master_volume(int32_t vol_q15);

/* ==================================================================
 * ★★★ 跨核命令队列 —— 消除 MIDI 核与音频核之间的数据竞争 ★★★
 * ==================================================================
 *
 * 问题（现场症状："用 MIDI 推音频一卡一卡的"）：
 *   core0 在 voice_render_stereo() 里**每个采样点**遍历 s_voice[]；
 *   core1 收到 note_on 时直接改 s_voice[]（v->pos = 0，v->phase = 0…）。
 *   两边没有任何同步 —— 音频核正在读某个声部时，MIDI 核把它的
 *   采样位置重置了，指针突然跳变。听感就是咔哒、跳音、一卡一卡。
 *
 *   （之前用"开机自检和弦"测不出来，因为那不走 MIDI；
 *     用 flood 也测不出来，因为太快，噪声把咔哒声盖住了。）
 *
 * 做法（单生产者/单消费者无锁队列）：
 *   core1 只投递命令，**永远不碰 s_voice[]**；
 *   core0 在每个音频块开头统一排空队列、应用命令，然后才渲染。
 *   于是声部表**只被音频核写**，竞争从根上消失。
 *
 * ★ engine_handle_midi() 保留原样（立即应用）—— 主机测试要靠它做
 *   逐样本对拍（44566 条记录），所以不能改它的语义。
 * ================================================================== */

/* 投递一条 MIDI 事件（core1 调用）。队列满则丢弃并计数，绝不阻塞。 */
void engine_post_midi(const midi_event_t *ev);

/* 请求"全部音符关闭"（core1 调用，只置一个标志，不碰声部表）。 */
void engine_request_all_off(void);

/* 排空命令队列并应用（core0 在每个音频块开头调用一次）。 */
void engine_flush_commands(void);

/* 因队列满而丢弃的事件数（诊断用）。 */
uint32_t engine_dropped_commands(void);
int32_t engine_get_master_volume(void);

/* 当前活跃复音数 */
uint32_t engine_active_voices(void);

/* 统计（用于串口打印与性能评估） */
typedef struct {
    uint64_t note_on;            /* 收到的 Note On 次数 */
    uint64_t note_off;
    uint64_t dropped_no_zone;    /* 找不到匹配 zone 而丢弃的次数 */
    uint64_t dropped_no_voice;   /* 抢声部次数（voice 层统计） */
    uint64_t cc_received;
    uint64_t pitch_bend;
    uint64_t program_change;
    uint32_t last_program[16];   /* 每个通道当前的音色号 */
    uint8_t  last_note[16];      /* 最近一个音（给数码管显示用） */
} engine_stats_t;

const engine_stats_t *engine_stats(void);

/* 当前通道的音色号（给 UI 显示用） */
uint8_t engine_channel_program(uint8_t channel);

/* 复位所有控制器（CC121）与所有音符 */
void engine_reset(void);

#endif /* ENGINE_H */
