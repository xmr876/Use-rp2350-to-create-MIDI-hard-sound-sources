/*
 * i2s.h — PIO I2S 输出接口
 *
 * 音频引擎（core0）通过 i2s_push_block() 往里写，DMA 自动取走。
 */
#ifndef I2S_H
#define I2S_H

#include <stdint.h>
#include <stdbool.h>

/*
 * 初始化 I2S 输出。
 *
 * ★ 必须在 clocks_init() 之后调用 —— 内部要读 clock_get_hz(clk_sys)，
 *   复位瞬间这个值是 ROSC/clk_ref，不是 150 MHz。
 *
 * 返回 0 成功；-1 表示目标频率超过 clk_sys（分频会小于 1，物理不可行）。
 */
int i2s_init(void);

/* ★ 启动 DMA 输出。**由音频核在进主循环前调用一次**。
 *
 *   为什么拆成两步而不是在 i2s_init() 里直接开：
 *     DMA 一开就按 21ms 一块的节奏要数据。如果这时音频核还没起来
 *     （i2s_init 和 multicore_launch_core1 之间隔着初始化代码），
 *     DMA 就会空转几百毫秒、欠载计数一路涨满，之后再也追不回来。
 *     现场症状是"纯静音也欠载、声音是持续炒豆子声"。 */
void i2s_start(void);

/*
 * 环形缓冲里还能写多少帧。
 * 引擎在每个缓冲周期开始时查看，决定这次能算多少。
 */
uint32_t i2s_free_frames(void);

/*
 * 推入一帧。环满时返回 -1 并丢弃该帧（不覆盖未播数据）。
 * 这是"每帧一次"的接口，只在引擎走逐帧路径时用。
 */
int i2s_push_frame(int16_t left, int16_t right);

/*
 * 批量推入交错立体声帧（推荐路径）。
 * interleaved 布局：L0 R0 L1 R1 …
 * 返回实际推入的帧数；小于 frames 说明环满了。
 */
int i2s_push_block(const int16_t *interleaved, uint32_t frames);

/* 诊断计数 */
uint32_t i2s_underrun_count(void);   /* DMA 追上引擎的次数（应恒为 0） */
uint32_t i2s_overrun_count(void);    /* 环满丢帧次数 */
uint32_t i2s_dma_block_count(void);

/* 读回当前实际生效的分频，用于调试（期望 48kHz/150MHz 下是 div=12 frac=53） */
void i2s_get_divider(uint32_t *div, uint32_t *frac);

#endif /* I2S_H */
