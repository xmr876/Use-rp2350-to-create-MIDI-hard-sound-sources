/*
 * flash_direct.h — 数据盘访问（QMI Direct Serial Mode）
 *
 * ==================================================================
 * ★★★ 三条死机规则 —— 违反任何一条都会立刻总线错误，不是"可能出问题"
 * ==================================================================
 *
 * 1. **direct 模式期间不能执行任何 flash 里的代码**
 *    QMI 文档："Memory-mapped accesses will generate bus errors when
 *    direct serial mode is enabled."
 *    我们正从启动盘 XIP 执行 → 取指打到 flash 就崩。
 *    对策：所有 direct 操作都在 __not_in_flash_func 里；
 *    期间不得触发任何位于 flash 的中断处理函数。
 *
 * 2. **每次操作前轮询 BUSY == 0**
 *    文档："Direct mode blocks new memory-mapped transfers, but can't halt
 *    a transfer that is already in progress. If there is a chance that
 *    memory-mapped transfers may be in progress, the busy flag should be
 *    polled for 0 before asserting the chip select."
 *    本模块内部已经做了，但**调用方仍要在调用前确认内核没在跑 flash 代码**。
 *
 * 3. **片选由硬件控制，不要用 GPIO 去驱动 CS0/CS1**
 *    DIRECT_CSR 里有 ASSERT_CS0N / ASSERT_CS1N 位。
 *    CS2/CS3 没有硬件支持，用 GPIO（GPIO8/GPIO9）软件控制。
 *
 * ==================================================================
 * 支持的片选
 * ==================================================================
 *   chip 0 → CS0（启动盘，W25Q32）        —— 硬件片选，**只读**（别写坏了固件）
 *   chip 1 → CS1（数据盘 1，GPIO19）      —— 硬件片选，可读写
 *   chip 2 → CS2（数据盘 2，GPIO8）       —— GPIO 软片选，可读写
 *   chip 3 → CS3（数据盘 3，GPIO9）       —— GPIO 软片选，可读写
 *
 * ==================================================================
 * 时序
 * ==================================================================
 *   SCK = sys_clk / (2 × CLKDIV)
 *   150MHz / (2 × 4) = 18.75 MHz
 *   理论吞吐 = SCK / 8 = 2.34 MB/s
 *
 *   ★ 需求（48 复音稳态 ADPCM）约 1.43 MB/s → 1.63 倍余量
 *     典型 24 复音时 3.3 倍余量
 *     推导见 tools/verify_qspi_bandwidth.py
 *
 * ==================================================================
 * 重要限制
 * ==================================================================
 *   ★ direct 模式**只支持 SPI mode 0**（CPOL=0 CPHA=0）。
 *     所以不能用 0xEB（Quad I/O，1-4-4）—— 那是给内存映射窗口用的。
 *     direct 模式用 **0x0B（1-1-1 Fast Read）**。
 *
 *     这意味着吞吐按"每字节 8 个 SCK"算（单线），不是四线的每字节 2 个 SCK。
 *     上面的 2.34 MB/s 已经是按单线算的，是准确的。
 */
#ifndef FLASH_DIRECT_H
#define FLASH_DIRECT_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

/* 片选编号（与 layout.py 的 chip_id 一致） */
#define FLASH_CHIP_BOOT   0     /* CS0，启动盘，只读 */
#define FLASH_CHIP_DATA0  1     /* CS1，数据盘 1 */
#define FLASH_CHIP_DATA1  2     /* CS2，数据盘 2 */
#define FLASH_CHIP_DATA2  3     /* CS3，数据盘 3 */
#define FLASH_CHIP_COUNT  4

/*
 * 初始化直接模式。
 * 做的事：
 *   - 配置 CS2/CS3 的 GPIO 为输出（CS0/CS1 由 QMI 硬件控制）
 *   - 配置 DIRECT_CSR 的 CLKDIV / RXDELAY
 *   - 探测各片的 JEDEC ID（用于诊断）
 *
 * 返回 0 成功；非 0 是"有片子没探测到"的位掩码
 * （bit0 = CS0, bit1 = CS1, ...）—— 返回值非 0 不算致命，
 *  调用方可以据此在数码管上提示"某片缺失"。
 *
 * ★ 必须在 clocks 初始化之后调用（要读 clk_sys 算分频）。
 */
int flash_direct_init(void);

/*
 * 读 JEDEC ID（3 字节：manufacturer, memory type, capacity）。
 * 返回 0 成功。W25Q128JV 应该是 0xEF 0x40 0x18。
 * ★ 这个方法**不需要** BUSY 轮询之外的前置条件，可安全用于探测。
 */
int flash_read_jedec_id(int chip, uint8_t id[3]);

/*
 * 读数据。**这是采样器的主要接口。**
 *
 * chip : 片选编号
 * addr : 片内字节地址（24 位）
 * dst  : 目标缓冲区
 * len  : 字节数
 *
 * ★ 缓冲区必须在 RAM 里（不能是 flash 里的常量区）——
 *   否则 DMA/CPU 写它会碰到 XIP 保护。
 *
 * ★ 本函数是**短临界区**（4KB 约 1.8ms）。调用方：
 *     - 在 core1 调用（不要放 core0，会打断音频）
 *     - 调用期间 core1 的中断应屏蔽（或在 RAM 里）
 */
int flash_read(int chip, uint32_t addr, void *dst, size_t len);

/*
 * 校验和辅助：从 flash 读一段并算 FNV-1a（与 PC 工具链一致）。
 * 用于烧录后校验。
 */
uint32_t flash_fnv1a(int chip, uint32_t addr, size_t len);

/* ---- 写入路径（USB 刷音色库用，P7 阶段启用）---- */

/*
 * 写数据（自动处理 擦除-写-等待）。
 * ★ 只允许写 chip 1..3；写 chip 0 会返回错误（保护固件）。
 * erase_first = true 时先擦除覆盖到的扇区（4KB）。
 */
int flash_write(int chip, uint32_t addr, const void *src, size_t len,
                bool erase_first);

/* 擦除一个 4KB 扇区 */
int flash_erase_sector(int chip, uint32_t addr);

/* ------------------------------------------------------------------ */
/* 诊断                                                               */
/* ------------------------------------------------------------------ */

/* 统计：读操作的次数与总字节数（用于评估总线负载） */
typedef struct {
    uint32_t reads;
    uint32_t bytes;
    uint32_t busy_waits;      /* 轮询 BUSY 的循环次数，反映总线拥挤程度 */
    uint32_t errors;
} flash_stats_t;

void flash_get_stats(int chip, flash_stats_t *out);
void flash_reset_stats(void);

/* 读回当前生效的 SCK 频率（Hz），用于调试 */
uint32_t flash_get_sck_hz(void);

#endif /* FLASH_DIRECT_H */
