/*
 * i2s.c — PIO I2S 输出驱动（双缓冲 + DMA）
 *
 * 数据流：
 *     [音频引擎 core0] --填块--> s_buf[N] --DMA--> PIO TX FIFO --> LRCK/BCK/DIN
 *
 * ★★ 为什么是"双缓冲整块"而不是"环形缓冲逐帧"（这里改过一版，记下原因）
 *
 *   原设计：环形缓冲 + 每个元素一个 uint64（一整帧），DMA 按"帧"搬运，
 *   想让 PIO 不可能只取走半帧。
 *   编译后发现两个硬伤：
 *
 *     1. **RP2350 的 DMA 根本没有 64 位传输**。
 *        `DMA_SIZE_64` 不存在（只有 DMA_SIZE_8/16/32）。
 *        报错：'DMA_SIZE_64' undeclared; did you mean 'DMA_SIZE_32'?
 *        这个错在第一次真正编译时才暴露 —— 之前没有编译器，
 *        全靠人眼审代码，看不出来。
 *
 *     2. 就算退成 32 位逐字搬运，环形缓冲还有**跨界问题**：
 *        DMA 一次要搬 N 个字，如果 tail 靠近环尾，这一次搬运就会
 *        越过数组末尾读到非法内存。原实现的 IRQ 里没有处理回绕。
 *
 *   改成**双缓冲**之后两个问题一起消失：
 *     · 每次 DMA 搬的都是一个完整的、地址连续的块，不可能跨界；
 *     · 块内是 32 位字交织存放（L,R,L,R…），DMA 用 DMA_SIZE_32 正好；
 *     · PIO 的 autopull 每次取 32 位，顺序与内存里的 L,R 顺序一一对应，
 *       左/右声道不会错位（这正是原设计想防的问题，双缓冲同样防住了）。
 *
 * ★ 块的节奏：
 *     每块 512 帧 = 10.67ms @48kHz。
 *     DMA 播 A 块时引擎填 B 块；A 播完中断里切到 B。
 *     所以引擎有**整整一个块的时间（10.67ms）**去算下一块 ——
 *     这是把音频引擎独占 core0 的量化理由。
 *
 * ★ PCM5102A 的硬约束：BCK/LRCK 停超过 4 个 LRCK 周期，芯片会重新初始化
 *   并静音。所以中断里**绝不能等**：宁可补一块静音，也不能让 DMA 停。
 */
#include <string.h>

#include <pico/stdlib.h>
#include <hardware/pio.h>
#include <hardware/dma.h>
#include <hardware/clocks.h>
#include <hardware/irq.h>
#include <hardware/sync.h>

#include "board_config.h"
#include "i2s.h"
#include "i2s.pio.h"

/* ================================================================== */
/* 编译期校验：引脚分配必须满足 PIO 程序的结构要求                       */
/* ================================================================== */
_Static_assert(PIN_I2S_BCK == PIN_I2S_LRCK + 1,
               "BCK 必须紧跟 LRCK：side-set bit0=LRCK, bit1=BCK");
_Static_assert(PIN_I2S_DIN == PIN_I2S_LRCK + 2,
               "DIN 必须紧跟 BCK：out_base = side_set_base + 2");
_Static_assert(BOARD_I2S_PIO_CYCLES_PER_BCLK == 10,
               "i2s.pio 的循环体是 10 条指令；改了 .pio 就要同步改 board_config.h");
_Static_assert(BOARD_AUDIO_BUF_FRAMES >= 64,
               "块太小会导致中断过于频繁，且有欠载风险");

/* ================================================================== */
/* 内部状态                                                            */
/* ================================================================== */

static PIO  s_pio = pio0;
static uint s_sm = 0;
static uint s_offset = 0;
static int  s_dma_chan = -1;

/* 双缓冲。每块 BOARD_AUDIO_BUF_FRAMES 帧，交织存放：
 *     [0]=L0 [1]=R0 [2]=L1 [3]=R1 …
 * 每帧占 2 个 uint32，所以每块 BOARD_AUDIO_BUF_FRAMES*2 个字。
 * ★ DMA 用 DMA_SIZE_32，一个字一次传输；PIO 侧 autopull 也按 32 位取，
 *   顺序天然对应，不需要任何打包/解包。 */
#define BLK_FRAMES  BOARD_AUDIO_BUF_FRAMES
#define BLK_WORDS   (BLK_FRAMES * 2u)

static uint32_t s_buf[2][BLK_WORDS];

static volatile uint8_t  s_ready[2];      /* 1 = 已填满，等 DMA 播 */
static volatile uint8_t  s_play;          /* DMA 正在播的块号 */
static volatile uint8_t  s_fill;          /* 引擎正在填的块号 */
static volatile uint32_t s_fill_frames;   /* 当前填充块里已写多少帧 */

static dma_channel_config s_dma_cfg;

/* 诊断计数 */
static volatile uint32_t s_underruns = 0;    /* DMA 播到还没填好的块 */
static volatile uint32_t s_overruns = 0;     /* 引擎想填但块还没空出来 */
static volatile uint32_t s_dma_blocks = 0;   /* 已完成 DMA 块数 */


/* ================================================================== */
/* 分频计算                                                            */
/* ================================================================== */

/*
 * ★ 必须在 clocks 初始化之后调用。
 *   复位瞬间 clk_sys 来自 ROSC/clk_ref，不是 150 MHz；
 *   在 PLL 锁定前读 clock_get_hz() 会得到别的值，
 *   硬编码 150MHz 会在启动早期算出错的分频（症状：音高全错）。
 *
 * 返回 0 成功，-1 表示目标频率超过 clk_sys（物理不可行）。
 *
 * 期望结果（150MHz / 48kHz / 64bit帧 / 4周期每BCLK）：
 *   SM 频率 = 48000 × 64 × 4 = 12.288 MHz
 *   分频    = 150e6 / 12.288e6 = 12.20703125 = 12 + 53/256
 *   即 (div, frac) = (12, 53)，零量化误差
 */
static int i2s_compute_divider(uint32_t *out_int, uint32_t *out_frac)
{
    uint32_t sys_hz = clock_get_hz(clk_sys);
    /* ★ 按**整帧**周期数算，不是按 64 × 每 bit 周期数。
     *   旧版两者相等（都是 640），但现在的 i2s.pio 每声道多一条
     *   `set x, 31` 重新装载计数器，实际是 642 —— 按 640 算会让
     *   采样率偏高 48000 × 642/640 = 48150 Hz（+5.4 音分）。 */
    uint32_t want = BOARD_SAMPLE_RATE * BOARD_I2S_PIO_CYCLES_PER_FRAME;

    if (want == 0 || sys_hz < want) {
        return -1;
    }

    uint32_t div = sys_hz / want;
    if (div < 1 || div > 65535u) {
        return -1;              /* PIO 分频整数部分是 16 位 */
    }

    uint32_t rem = sys_hz % want;
    /* 8 位小数：frac = rem * 256 / want。
     * 用 64 位中间量：rem < want ≤ 12.288e6，左移 8 位后约 3.1e9，
     * 接近 uint32 上限，用 64 位更稳。 */
    uint32_t frac = (uint32_t)(((uint64_t)rem << 8) / want);

    *out_int = div;
    *out_frac = frac & 0xFFu;
    return 0;
}

/* ================================================================== */
/* DMA 中断：一个块播完了，切到另一块                                   */
/* ================================================================== */
/*
 * ★ 这个中断**必须极快**且**绝不能等**。
 *   I2S 的 LRCK 一旦停超过 4 个周期，PCM5102A 会重新初始化并静音
 *   （要几百毫秒才恢复），听感上是一个明显的"断音"。
 *   所以宁可补一块静音，也不能在这里自旋等引擎。
 */
static void __isr i2s_dma_irq(void)
{
    dma_hw->ints0 = 1u << (uint)s_dma_chan;     /* 清中断 */
    s_dma_blocks++;

    const uint8_t done = s_play;
    const uint8_t next = (uint8_t)(done ^ 1u);

    if (!s_ready[next]) {
        /* 欠载：引擎没在下一个块播完之前填好它。
         * 补静音保住时钟。引擎追上来后自然恢复，不需要重新初始化。 */
        s_underruns++;
        memset(s_buf[next], 0, sizeof(s_buf[next]));
    }
    s_ready[next] = 0;

    /* 切换：next 开始播，刚播完的 done 交还给引擎去填 */
    s_play = next;
    s_fill = done;
    s_fill_frames = 0;
    s_ready[done] = 0;

    dma_channel_set_read_addr((uint)s_dma_chan, s_buf[next], false);
    dma_channel_set_trans_count((uint)s_dma_chan, BLK_WORDS, true);
}

/* ================================================================== */
/* 对外接口                                                            */
/* ================================================================== */

int i2s_init(void)
{
    uint32_t div, frac;

    if (i2s_compute_divider(&div, &frac) != 0) {
        return -1;
    }

    /* --- 引脚：交给 PIO --- */
    for (uint i = 0; i < 3; i++) {
        uint gpio = PIN_I2S_SIDESET_BASE + i;
        pio_gpio_init(s_pio, gpio);
        gpio_set_drive_strength(gpio, GPIO_DRIVE_STRENGTH_4MA);
        gpio_set_slew_rate(gpio, GPIO_SLEW_RATE_FAST);
    }

    /* --- 状态机 ---
     * ★★ 用 **false**（拿不到就返回，不 panic）。
     *   用 true 的话，万一 PIO 资源被占满就会 panic ——
     *   而 panic 发生在 main() 的初始化阶段，后果是
     *   **数码管和声音一起哑掉，现场还看不出卡在哪**。
     *   这里改成返回不同的负数，让 main() 能把错误码显示到数码管上。 */
    int sm = pio_claim_unused_sm(s_pio, false);
    if (sm < 0) {
        return -2;              /* 2 = 没有空闲状态机 */
    }
    s_sm = (uint)sm;

    int off = pio_add_program(s_pio, &i2s_out_program);
    if (off < 0) {
        return -3;              /* 3 = PIO 指令槽放不下 */
    }
    s_offset = (uint)off;

    pio_sm_config c = i2s_out_program_get_default_config(s_offset);

    /* side-set 2 位：bit0=LRCK, bit1=BCK，基址 = LRCK */
    sm_config_set_sideset_pins(&c, PIN_I2S_SIDESET_BASE);

    /* out 1 位，基址 = DIN */
    sm_config_set_out_pins(&c, PIN_I2S_DIN, 1);

    /* TX/RX 合并，TX FIFO 深度翻倍到 8，能吸收更长的中断延迟 */
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);

    /* ★★ 左移（MSB first）+ **开 autopull** + 阈值 32。
     *
     * 开 autopull 是 i2s.pio 新版程序的硬性要求：
     *   程序里已经没有 `pull` 指令了（旧版每声道 `pull` + `set x,N`
     *   会多出 2 个 SM 周期，整帧 644 而不是 640，采样率偏低 10 音分）。
     *   现在靠 "OSR 空时 out 自动补" + `jmp !osre` 控制循环，
     *   每声道正好 320 周期。
     *
     * ★ 第 2 个参数是 shift_right，必须是 **false**：
     *   I2S 要求 MSB 先出。开成 true（LSB 先出）的症状是
     *   每个采样点的位序完全颠倒，输出听起来是白噪声。 */
    sm_config_set_out_shift(&c, false, true, 32);

    /* 运行时算出的分频 */
    sm_config_set_clkdiv_int_frac8(&c, div, (uint8_t)frac);

    /* 三个脚都设为输出 */
    const uint32_t pin_mask = (1u << PIN_I2S_LRCK) | (1u << PIN_I2S_BCK)
                              | (1u << PIN_I2S_DIN);
    pio_sm_set_pindirs_with_mask(s_pio, s_sm, pin_mask, pin_mask);
    /* 初始电平全 0：LRCK=0（左声道）、BCK=0 */
    pio_sm_set_pins_with_mask(s_pio, s_sm, 0, pin_mask);

    pio_sm_init(s_pio, s_sm, s_offset, &c);

    /* --- DMA --- */
    s_dma_chan = dma_claim_unused_channel(true);
    s_dma_cfg = dma_channel_get_default_config((uint)s_dma_chan);

    /* ★ 32 位传输。
     *   RP2350 的 DMA **没有** 64 位传输模式（只有 DMA_SIZE_8/16/32），
     *   所以一次搬一个 32 位字。块内是 L,R,L,R… 交织存放，
     *   PIO 的 autopull 也按 32 位取，顺序天然对齐，左右声道不会错位。 */
    channel_config_set_transfer_data_size(&s_dma_cfg, DMA_SIZE_32);
    channel_config_set_read_increment(&s_dma_cfg, true);
    channel_config_set_write_increment(&s_dma_cfg, false);
    channel_config_set_dreq(&s_dma_cfg, pio_get_dreq(s_pio, s_sm, true));

    dma_channel_configure((uint)s_dma_chan, &s_dma_cfg,
                          &s_pio->txf[s_sm],   /* 写目标：TX FIFO */
                          s_buf[0],            /* 读源：0 号块 */
                          BLK_WORDS,           /* 一次搬完整块 */
                          false);

    dma_channel_set_irq0_enabled((uint)s_dma_chan, true);
    irq_set_exclusive_handler(DMA_IRQ_0, i2s_dma_irq);
    /* ★ 优先级设最高：I2S 断流的代价（DAC 重新初始化、几百毫秒静音）
     *   远大于其它中断晚几百纳秒。 */
    irq_set_priority(DMA_IRQ_0, 0x00);
    irq_set_enabled(DMA_IRQ_0, true);

    /* --- 状态机与 DMA 配置好，但**先不启动** ---
     *
     * ★★★ 这里原来直接就把 DMA 开起来了，是个设计错误 ★★★
     *
     *   现场症状：欠载计数一路涨到 99、声音是持续不断的炒豆子声，
     *   而且**一个音符都不放（纯静音）时照样欠载**。
     *
     *   原因是启动顺序：
     *       i2s_init()   ... 这里 DMA 就开始播块 0（只有 21ms）
     *       sleep_ms(350)         ← 调试延时就 700ms 了
     *       engine_init()
     *       multicore_launch_core1()  ← 音频核到这里才起来
     *
     *   **DMA 在"没人填数据"的状态下空转了几百毫秒** ——
     *   块只有 21ms，所以欠载计数在音频核还没出生时就涨满了。
     *   之后每次切换都发现 s_ready[next] 是 0，一路欠载下去。
     *
     *   → 改成：**两块都预填静音，但等音频核准备好之后再启动 DMA**
     *     （由音频核第一条语句调 i2s_start()）。
     *     这样从第一个块开始，DMA 拿到的就一直是有效数据。
     */
    memset(s_buf, 0, sizeof(s_buf));
    s_ready[0] = 0;      /* 两块都交还给音频核去填 */
    s_ready[1] = 0;
    s_play = 0;
    s_fill = 0;
    s_fill_frames = 0;

    pio_sm_set_enabled(s_pio, s_sm, true);

    /* 先把 TX FIFO 灌满静音。
     * 不这样做的话，PIO 第一次 autopull 会立刻阻塞等 FIFO，
     * 而 DMA 又要等 DREQ —— 第一条 LRCK 会被拉长，DAC 可能误判格式。 */
    for (int i = 0; i < 8; i++) {
        pio_sm_put(s_pio, s_sm, 0);
    }

    return 0;
}

/* ================================================================== */
/* 启动 DMA —— 由音频核在进入主循环前调用一次                          */
/* ================================================================== */
/*
 * ★ 为什么要拆出这一步：见上面 i2s_init() 里那段说明。
 *   核心是"数据准备好之前不要开 DMA"。
 *
 * 调用时机：audio_engine() 的**第一件事**。
 * 调用之后，引擎就可以按正常流程往 s_fill 里填块了。
 */
void i2s_start(void)
{
    /* 此时两块都还是静音（i2s_init 里 memset 过），
     * 先播块 1，把块 0 留给引擎填 —— 这样引擎有一整个块周期的时间。 */
    s_play = 1;
    s_fill = 0;
    s_fill_frames = 0;
    s_ready[0] = 0;
    s_ready[1] = 1;      /* 块 1 是静音，可以直接播 */

    dma_channel_set_read_addr((uint)s_dma_chan, s_buf[1], false);
    dma_channel_set_trans_count((uint)s_dma_chan, BLK_WORDS, true);

    dma_channel_start((uint)s_dma_chan);
    return;
}

/* ================================================================== */
/* 引擎侧接口                                                          */
/* ================================================================== */

uint32_t i2s_free_frames(void)
{
    return BLK_FRAMES - s_fill_frames;
}

int i2s_push_frame(int16_t left, int16_t right)
{
    uint32_t n = s_fill_frames;
    if (n >= BLK_FRAMES) {
        s_overruns++;
        return -1;
    }
    /* 打包：每个 32 位字里 **高 16 位是音频数据**，低 16 位补 0。
     * PCM5102A 在 32bit 帧模式下取 MSB 对齐数据；
     * 若右对齐（数据放低 16 位）会被当成极小音量，症状是"几乎听不见"。 */
    const uint8_t blk = s_fill;
    s_buf[blk][n * 2 + 0] = ((uint32_t)(uint16_t)left)  << 16;
    s_buf[blk][n * 2 + 1] = ((uint32_t)(uint16_t)right) << 16;
    n++;
    s_fill_frames = n;
    if (n == BLK_FRAMES) {
        s_ready[blk] = 1;    /* 块填满，标就绪；等 DMA 中断来切 */
    }
    return 0;
}

int i2s_push_block(const int16_t *interleaved, uint32_t frames)
{
    /* ★ 写入期间关中断。
     *   因为 DMA 中断会改 s_fill，如果它在写入过程中切块，
     *   我们就会把数据写进一个刚开始播放的块里（听感是爆音）。
     *   代价：关中断约 2~3 µs（1024 次 32 位写），
     *   相对 10.67ms 的块周期可以忽略，也不会让 I2S 断流。 */
    uint32_t save = save_and_disable_interrupts();

    const uint8_t blk = s_fill;
    uint32_t n = s_fill_frames;
    uint32_t pushed = 0;

    while (pushed < frames && n < BLK_FRAMES) {
        s_buf[blk][n * 2 + 0] =
            ((uint32_t)(uint16_t)interleaved[pushed * 2 + 0]) << 16;
        s_buf[blk][n * 2 + 1] =
            ((uint32_t)(uint16_t)interleaved[pushed * 2 + 1]) << 16;
        n++;
        pushed++;
    }

    s_fill_frames = n;
    if (n == BLK_FRAMES) {
        s_ready[blk] = 1;
    }
    if (pushed < frames) {
        s_overruns++;
    }

    restore_interrupts(save);
    return (int)pushed;
}

uint32_t i2s_underrun_count(void) { return s_underruns; }
uint32_t i2s_overrun_count(void)  { return s_overruns; }
uint32_t i2s_dma_block_count(void){ return s_dma_blocks; }

void i2s_get_divider(uint32_t *div, uint32_t *frac)
{
    uint32_t v = s_pio->sm[s_sm].clkdiv;
    if (div) {
        *div = (v >> PIO_SM0_CLKDIV_INT_LSB) & 0xFFFFu;
    }
    if (frac) {
        *frac = (v >> PIO_SM0_CLKDIV_FRAC_LSB) & 0xFFu;
    }
}
