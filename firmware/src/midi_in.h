/*
 * midi_in.h — MIDI 输入解析（UART1 @ 31250 8N1）
 *
 * 硬件（见 board_config.h / docs/原理图设计说明.md §4.1）：
 *   6N138 光耦 → GPIO10 = UART1 RX（F11）
 *   ★ 必须用 UART1 —— GPIO10/11 上没有 UART0
 *   ★ 光耦相对电流环反相，但到 UART RX 极性正好对，不要加反相器
 *
 * 设计：
 *   - UART 中断只负责把字节塞进环形缓冲，不做解析（中断要短）
 *   - 解析在主循环/core1 里做，支持 running status
 *   - 解析出的**语义事件**（note on/off、CC、program change）入队给音频核
 */
#ifndef MIDI_IN_H
#define MIDI_IN_H

#include <stdint.h>
#include <stdbool.h>

/* MIDI 状态字节 */
#define MIDI_NOTE_OFF        0x80u
#define MIDI_NOTE_ON         0x90u
#define MIDI_POLY_PRESSURE   0xA0u
#define MIDI_CTRL_CHANGE     0xB0u
#define MIDI_PROGRAM_CHANGE  0xC0u
#define MIDI_CHAN_PRESSURE   0xD0u
#define MIDI_PITCH_BEND      0xE0u
#define MIDI_SYSEX_START     0xF0u
#define MIDI_SYSEX_END       0xF7u
#define MIDI_REALTIME_MIN    0xF8u   /* 0xF8..0xFF 是真时事件，可插在任意位置 */

/* 常用 CC 号 */
#define MIDI_CC_VOLUME       7
#define MIDI_CC_PAN          10
#define MIDI_CC_EXPRESSION   11
#define MIDI_CC_SUSTAIN      64
#define MIDI_CC_ALL_NOTES_OFF 123
#define MIDI_CC_ALL_SOUND_OFF 120
#define MIDI_CC_RESET_ALL     121

/* 解析出的事件类型 */
typedef enum {
    MIDI_EV_NONE = 0,
    MIDI_EV_NOTE_ON,
    MIDI_EV_NOTE_OFF,
    MIDI_EV_PROGRAM,
    MIDI_EV_CONTROL,
    MIDI_EV_PITCH_BEND,
    MIDI_EV_CHAN_PRESSURE,
    MIDI_EV_POLY_PRESSURE,
} midi_ev_type_t;

typedef struct {
    midi_ev_type_t type;
    uint8_t channel;      /* 0..15 */
    uint8_t data1;        /* note / program / cc 号 */
    uint8_t data2;        /* velocity / cc 值 */
    uint16_t bend;        /* 14 位弯音，0..16383，中心 8192 */
} midi_event_t;

/* 环形缓冲大小：31250 baud ≈ 3.1 kB/s，256 字节约 80ms 余量 */
#define MIDI_RX_BUF_SIZE  256
#define MIDI_EVQ_SIZE     64

/*
 * 初始化 UART1 与中断。
 * ★ 必须在 clocks 初始化之后调用（UART 波特率依赖 clk_peri）。
 * 返回 0 成功。
 */
int midi_in_init(void);

/*
 * 从原始字节流解析出下一个完整事件。
 * 返回 true 表示 out 被填入一个事件。
 *
 * ★ 支持 running status（连续同类型消息省略状态字节）。
 * ★ 真时事件（0xF8..0xFF）被忽略，不破坏 running status。
 */
/* 把 PIO RX FIFO 里的字节搬进内部环形缓冲。
 * 由 midi_poll() 自动调用；单独暴露出来是为了让自检脚本能主动抽一次。 */
void midi_in_drain(void);

bool midi_poll(midi_event_t *out);

/*
 * 直接喂一个字节（用于**单元测试**，绕过 UART）。
 * 返回 true 表示这个字节凑齐了一条完整消息，out 被填入事件。
 *
 * ★ 有了这个接口，解析逻辑就能在 PC 上用 Python 转写验证
 *   （见 tools/verify_midi.py），不需要硬件。
 *   生产代码请用 midi_poll()。
 */
bool midi_feed_byte(uint8_t b, midi_event_t *out);

/* 诊断 */
uint32_t midi_rx_overflow_count(void);   /* 环形缓冲溢出次数（应恒为 0） */
uint32_t midi_bytes_received(void);
uint32_t midi_framing_errors(void);

#endif /* MIDI_IN_H */
