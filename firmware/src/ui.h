/*
 * ui.h — 2 位共阴数码管 + 2 按键 + 状态指示
 *
 * 硬件（以 board_config.h 为准，接线图见 docs/接线图与BOM.md）：
 *   段线 a~g + dp：GPIO15..GPIO22（**连续 8 根**），各串 180Ω
 *                 连续分配是为了能用一次 32 位掩码写更新整组段码，
 *                 避免切换时出现"半新半旧"的鬼影。
 *   位选 1/2：GPIO6 / GPIO7，经 NPN（S8050）到 GND，高电平点亮
 *   ★ 位选必须用三极管 —— 一位最多 8 段同时亮，阴极要灌约 48mA，
 *     而 RP2350 单个 pad 只有 ~8mA 能力。
 *   按键：GPIO8 / GPIO9，内部上拉，按下为低
 *   状态指示：开发板**板载**的 RGB 灯，不用外接
 *
 * 扫描由定时器中断驱动，不占主循环（主循环每 2ms 才转一圈，
 * 靠它扫描会闪）。
 */
#ifndef UI_H
#define UI_H

#include <stdint.h>
#include <stdbool.h>

/* 按键位掩码 */
#define UI_BTN_1  0x01u
#define UI_BTN_2  0x02u

/*
 * 初始化数码管 / 按键 / LED，并启动扫描定时器。
 * ★ 扫描中断跑在调用这个函数的核上。当前设计里是 core1，
 *   所以 core0（音频）不会被打断。
 */
void ui_init(void);

/*
 * 显示两个字符。
 * 参数是"段码"而不是 ASCII —— 段码表见 ui.c，调用方用 ui_seg_digit()
 * 或 ui_seg_raw() 取得。
 */
void ui_display_raw(uint8_t left_seg, uint8_t right_seg);

/* 0-9 的段码 */
uint8_t ui_seg_digit(uint8_t d);

/* 熄灭 */
#define UI_SEG_BLANK  0x00u

/* 常用符号 */
#define UI_SEG_MINUS  0x40u   /* 只有 g 段 */

/* 小数点。段码位图是 bit0=a … bit6=g、bit7=dp。
 * ★ 用途：main.c 拿它当 **MIDI 心跳** —— 每收到一个字节就点亮 60ms，
 *   于是"MIDI 到底通没通"用眼睛就能看出来。 */
#define UI_SEG_DP     0x80u

/*
 * 显示一个 0..99 的十进制数（用于音量、音色号、进度百分比）。
 * 大于 99 会显示 "--"。
 */
void ui_display_number(int value);

/* 兼容旧接口：每帧调一次，实际工作由中断完成，这里只做按键消抖 */
void ui_display_update(void);

/*
 * 读按键（带消抖与长按检测）。
 * 返回本周期内**新按下**的按键位掩码（边沿触发，不是电平）。
 */
uint8_t ui_poll_buttons(void);

/* 状态 LED */
void ui_led_set(bool on);
void ui_led_toggle(void);

#endif /* UI_H */
