/*
 * flash_direct.c — 数据盘访问（QMI Direct Serial Mode）
 *
 * ★★★ 本文件里所有 flash 操作函数都标记了 __not_in_flash_func。
 *     没有这个标记，函数本身会在 flash 里，进 direct 模式后取指就崩。
 *     链接器会把它们放到 RAM。
 *
 * 实现要点（对照 pico-sdk hardware/structs/qmi.h 的位域）：
 *
 *   DIRECT_CSR:
 *     [0]     EN            使能 direct 模式
 *     [1]     BUSY          传输中标志，片选未释放前必须等它归零
 *     [2]     ASSERT_CS0N   拉低 CS0（即使 EN=0 也生效）
 *     [3]     ASSERT_CS1N   拉低 CS1
 *     [10]    TXFULL
 *     [11]    TXEMPTY
 *     [16]    RXEMPTY
 *     [17]    RXFULL
 *     [29:22] CLKDIV        SCK = sys_clk / (2 × CLKDIV)
 *     [31:30] RXDELAY
 *
 *   DIRECT_TX（写 FIFO，每条记录是一个"传输单元"）:
 *     [15:0]  DATA     要移出的数据
 *     [18]    DWIDTH   0 = 单线（SPI mode 0），1 = 四线
 *     [19]    OE       输出使能；读数据时必须为 0
 *     [20]    NOPUSH   1 = 不把收到的数据推进 RX FIFO
 *
 *   ★ NOPUSH 的用法（这里踩过一次）：
 *     **接收数据必须靠"NOPUSH=0 且 OE=0"的 TX 记录来产生时钟**，
 *     RX FIFO 是硬件自动推进去的，不需要软件 pop 来驱动。
 *     pop 只是"取走结果"，push 才是"产生时钟"。
 *     所以读 N 字节 = push N 个 (OE=0, NOPUSH=0) 的零记录，再 pop N 次。
 */
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "hardware/structs/qmi.h"
#include "hardware/gpio.h"

#include "board_config.h"
#include "flash_direct.h"

/* ================================================================== */
/* CS2 / CS3 的 GPIO 片选（CS0/CS1 由 QMI 硬件控制）                    */
/* ================================================================== */
/*
 * ★ CS1 = GPIO19 走 QMI 的 XIP_CS1n 功能（F9）。
 *   勘误 E14：bootrom 在 CS1 挂非 GPIO0 时会配错 pad，
 *   所以这里**必须自己配 pad**：
 *     - 设为 F9 功能
 *     - 使能输入
 *     - 关掉内部上下拉
 *   漏了这步的症状是第二片 Flash 完全读不到数据。
 */
#define FLASH_CS_GPIO_MASK  ((1u << PIN_FLASH_CS2) | (1u << PIN_FLASH_CS3))

/* ================================================================== */
/* 底层：direct 模式寄存器操作                                         */
/* ================================================================== */

/* 轮询 BUSY == 0。返回等待的循环次数（诊断用）。
 *
 * ★ 必须在每次操作**前**调用：QMI 文档说 direct 模式无法中止
 *   已经在进行的内存映射传输，所以要先等它结束。
 *   本函数带 __not_in_flash_func —— 因为它会在 direct 模式开启后执行，
 *   而那时任何 flash 取指都会总线错误。 */
static uint32_t __not_in_flash_func(wait_not_busy)(void)
{
    uint32_t spins = 0;
    while (qmi_hw->direct_csr & QMI_DIRECT_CSR_BUSY_BITS) {
        spins++;
    }
    return spins;
}

static inline void __not_in_flash_func(assert_cs)(int chip)
{
    uint32_t csr = qmi_hw->direct_csr;
    switch (chip) {
    case FLASH_CHIP_BOOT:
        csr |= QMI_DIRECT_CSR_ASSERT_CS0N_BITS;
        break;
    case FLASH_CHIP_DATA0:
        csr |= QMI_DIRECT_CSR_ASSERT_CS1N_BITS;
        break;
    case FLASH_CHIP_DATA1:
        gpio_put(PIN_FLASH_CS2, 0);     /* 低有效 */
        break;
    case FLASH_CHIP_DATA2:
        gpio_put(PIN_FLASH_CS3, 0);
        break;
    default:
        break;
    }
    qmi_hw->direct_csr = csr;
}

static inline void __not_in_flash_func(deassert_cs)(int chip)
{
    uint32_t csr = qmi_hw->direct_csr;
    switch (chip) {
    case FLASH_CHIP_BOOT:
        csr &= ~QMI_DIRECT_CSR_ASSERT_CS0N_BITS;
        break;
    case FLASH_CHIP_DATA0:
        csr &= ~QMI_DIRECT_CSR_ASSERT_CS1N_BITS;
        break;
    case FLASH_CHIP_DATA1:
        gpio_put(PIN_FLASH_CS2, 1);
        break;
    case FLASH_CHIP_DATA2:
        gpio_put(PIN_FLASH_CS3, 1);
        break;
    default:
        break;
    }
    qmi_hw->direct_csr = csr;
}

/*
 * 推一个字节并产生时钟。
 * oe = 0 表示这是"接收"字节（芯片驱动数据线，我们只提供时钟）。
 */
static inline void __not_in_flash_func(tx_byte)(uint8_t b, bool oe)
{
    uint32_t rec = (uint32_t)b;
    if (oe) {
        rec |= QMI_DIRECT_TX_OE_BITS;
    }
    /* DWIDTH = 0（单线 / SPI mode 0）
     * NOPUSH = 0（接收时要把结果推进 RX FIFO）
     * IWIDTH = 0（单线指令） */
    while (qmi_hw->direct_csr & QMI_DIRECT_CSR_TXFULL_BITS) {
        /* 等 TX FIFO 有空位 */
    }
    qmi_hw->direct_tx = rec;
}

static inline uint8_t __not_in_flash_func(rx_byte)(void)
{
    while (qmi_hw->direct_csr & QMI_DIRECT_CSR_RXEMPTY_BITS) {
        /* 等接收数据到位 */
    }
    return (uint8_t)(qmi_hw->direct_rx & 0xFFu);
}

/* ================================================================== */
/* 初始化和片选配置                                                    */
/* ================================================================== */

int __not_in_flash_func(flash_direct_init)(void)
{
    /* --- CS2 / CS3：普通 GPIO 输出，初始为高（未选中） --- */
    gpio_init(PIN_FLASH_CS2);
    gpio_set_dir(PIN_FLASH_CS2, GPIO_OUT);
    gpio_put(PIN_FLASH_CS2, 1);
    gpio_set_drive_strength(PIN_FLASH_CS2, GPIO_DRIVE_STRENGTH_4MA);

    gpio_init(PIN_FLASH_CS3);
    gpio_set_dir(PIN_FLASH_CS3, GPIO_OUT);
    gpio_put(PIN_FLASH_CS3, 1);
    gpio_set_drive_strength(PIN_FLASH_CS3, GPIO_DRIVE_STRENGTH_4MA);

    /* --- CS1：交给 QMI 的 XIP_CS1n 功能 ---
     * ★ 勘误 E14：bootrom 在 CS1 挂非 GPIO0 时会配错 pad，
     *   所以这里必须自己配。漏了的症状是第二片完全读不到。 */
    gpio_set_function(PIN_FLASH_CS1, GPIO_FUNC_XIP_CS1);
    gpio_set_input_enabled(PIN_FLASH_CS1, true);
    gpio_set_pulls(PIN_FLASH_CS1, false, false);   /* 关内部上下拉 */
    gpio_set_drive_strength(PIN_FLASH_CS1, GPIO_DRIVE_STRENGTH_4MA);

    /* --- 进 direct 模式 ---
     * ★ 进之前先确保没有正在进行的内存映射传输，否则 BUSY 会一直是 1。 */
    wait_not_busy();

    uint32_t csr = qmi_hw->direct_csr;
    csr &= ~(QMI_DIRECT_CSR_CLKDIV_BITS | QMI_DIRECT_CSR_RXDELAY_BITS);
    csr |= ((FLASH_DIRECT_CLKDIV << QMI_DIRECT_CSR_CLKDIV_LSB)
            & QMI_DIRECT_CSR_CLKDIV_BITS);
    csr |= ((FLASH_DIRECT_RXDELAY << QMI_DIRECT_CSR_RXDELAY_LSB)
            & QMI_DIRECT_CSR_RXDELAY_BITS);
    csr |= QMI_DIRECT_CSR_EN_BITS;
    qmi_hw->direct_csr = csr;

    /* --- 探测各片的 JEDEC ID --- */
    int missing = 0;
    for (int chip = 0; chip < FLASH_CHIP_COUNT; chip++) {
        uint8_t id[3] = { 0, 0, 0 };
        if (flash_read_jedec_id(chip, id) != 0) {
            missing |= (1 << chip);
            continue;
        }
        if (id[0] != FLASH_JEDEC_WINBOND) {
            missing |= (1 << chip);
        }
        /* capacity 字节：CS0 应该是 0x16(W25Q32)，其余 0x18(W25Q128)。
         * 这里只检查厂商 ID，容量不符另行用数码管提示。 */
    }

    return missing;
}

/* ================================================================== */
/* 读操作                                                              */
/* ================================================================== */

uint32_t __not_in_flash_func(flash_get_sck_hz)(void)
{
    uint32_t div = (qmi_hw->direct_csr & QMI_DIRECT_CSR_CLKDIV_BITS)
                   >> QMI_DIRECT_CSR_CLKDIV_LSB;
    if (div == 0) {
        div = 256;
    }
    return clock_get_hz(clk_sys) / (2u * div);
}

int __not_in_flash_func(flash_read_jedec_id)(int chip, uint8_t id[3])
{
    if (chip < 0 || chip >= FLASH_CHIP_COUNT) {
        return -1;
    }

    wait_not_busy();
    assert_cs(chip);

    /* 0x9F：单线，无地址，无 dummy，紧跟 3 字节 ID */
    tx_byte(FLASH_CMD_JEDEC_ID, true);

    /* ★ 接收字节靠 OE=0 的记录产生时钟 */
    tx_byte(0x00, false);
    id[0] = rx_byte();
    tx_byte(0x00, false);
    id[1] = rx_byte();
    tx_byte(0x00, false);
    id[2] = rx_byte();

    /* 等最后一次传输结束再释放片选 */
    wait_not_busy();
    deassert_cs(chip);

    return 0;
}

/*
 * 读一段数据。
 *
 * 命令序列（W25Q128JV 的 0x0B Fast Read，1-1-1）：
 *   0x0B | addr[23:16] | addr[15:8] | addr[7:0] | dummy(1 字节) | data...
 *
 * ★ 为什么用 0x0B 而不是 0xEB：
 *   direct 模式只支持 SPI mode 0 的**单线**传输，
 *   0xEB 是 1-4-4 四线命令，只有在内存映射窗口里才用。
 */
int __not_in_flash_func(flash_read)(int chip, uint32_t addr,
                                    void *dst, size_t len)
{
    if (chip < 0 || chip >= FLASH_CHIP_COUNT) {
        return -1;
    }
    if (len == 0) {
        return 0;
    }
    if (addr > 0xFFFFFFu) {
        return -1;              /* 24 位地址，16MB 上限 */
    }

    uint8_t *out = (uint8_t *)dst;

    s_stats[chip].busy_waits += wait_not_busy();
    assert_cs(chip);

    tx_byte(FLASH_CMD_READ_FAST, true);
    tx_byte((uint8_t)(addr >> 16), true);
    tx_byte((uint8_t)(addr >> 8), true);
    tx_byte((uint8_t)(addr), true);
    tx_byte(0x00, true);        /* dummy 字节 */

    /* 连续读。每读 256 字节检查一次 FIFO 深度会不会溢出 ——
     * RX FIFO 只有 4 深（DIRECT_RX 是 io_ro_32，实现上是小 FIFO），
     * 所以必须"推一个、取一个"交替，不能先推 N 个再取 N 个。
     * ★ 这里踩过一次：先推 8 个再取，会因为 RX FIFO 满而 stall，
     *   表现为卡死（BUSY 永不归零）。 */
    for (size_t i = 0; i < len; i++) {
        tx_byte(0x00, false);
        out[i] = rx_byte();
    }

    wait_not_busy();
    deassert_cs(chip);

    s_stats[chip].reads++;
    s_stats[chip].bytes += (uint32_t)len;

    return 0;
}

uint32_t __not_in_flash_func(flash_fnv1a)(int chip, uint32_t addr, size_t len)
{
    /* FNV-1a 32bit，与 tools/gmss/gmss/layout.py 的 fnv1a() 必须逐位一致。
     * ★ 注意：FNV-1a 是串行的，无法向量化（每步截断到 32 位破坏了分配律），
     *   所以这里就是简单的逐字节循环。 */
    uint32_t h = 0x811C9DC5u;
    uint8_t buf[256];

    while (len > 0) {
        size_t n = (len > sizeof(buf)) ? sizeof(buf) : len;
        if (flash_read(chip, addr, buf, n) != 0) {
            return 0;
        }
        for (size_t i = 0; i < n; i++) {
            h ^= buf[i];
            h *= 0x01000193u;
        }
        addr += (uint32_t)n;
        len -= n;
    }
    return h;
}

/* ================================================================== */
/* 写入路径（P7 阶段实现，现在先留接口并明确拒绝）                      */
/* ================================================================== */

int __not_in_flash_func(flash_erase_sector)(int chip, uint32_t addr)
{
    if (chip == FLASH_CHIP_BOOT) {
        return -1;              /* 保护固件，绝不擦启动盘 */
    }
    if (chip < 0 || chip >= FLASH_CHIP_COUNT) {
        return -1;
    }
    (void)addr;

    /* TODO(P7)：实现流程
     *   1. 0x06 Write Enable
     *   2. 0x20 + addr[23:0]  (4KB 扇区擦除)
     *   3. 轮询 0x05 状态寄存器 bit0 (BUSY) 直到归零
     *      —— W25Q128 的 4KB 擦除典型 45ms，最大 400ms
     *   4. 擦除期间**不能释放片选**
     */
    return -1;
}

int __not_in_flash_func(flash_write)(int chip, uint32_t addr,
                                     const void *src, size_t len,
                                     bool erase_first)
{
    if (chip == FLASH_CHIP_BOOT) {
        return -1;              /* 保护固件 */
    }
    if (chip < 0 || chip >= FLASH_CHIP_COUNT) {
        return -1;
    }
    (void)addr; (void)src; (void)len; (void)erase_first;

    /* TODO(P7)：实现流程
     *   1. 若 erase_first：按 4KB 对齐擦除覆盖到的扇区
     *   2. 0x06 Write Enable
     *   3. 0x02 Page Program（★ 每页最多 256 字节，且不能跨页）
     *   4. 每页后轮询状态寄存器 BUSY
     */
    return -1;
}

/* ================================================================== */
/* 诊断                                                                */
/* ================================================================== */

static flash_stats_t s_stats[FLASH_CHIP_COUNT];

void __not_in_flash_func(flash_get_stats)(int chip, flash_stats_t *out)
{
    if (out == NULL || chip < 0 || chip >= FLASH_CHIP_COUNT) {
        return;
    }
    *out = s_stats[chip];
}

void __not_in_flash_func(flash_reset_stats)(void)
{
    memset(s_stats, 0, sizeof(s_stats));
}
