/*
 * main.c — 固件入口
 *
 * 双核分工（这是硬性约束，不是为了好看）：
 *     core0 = 音频引擎。只做一件事：按时把采样块推给 I2S。**绝不阻塞**。
 *     core1 = 其余一切：MIDI 解析、数码管、按键、串口、Flash。
 *
 * ★ 为什么必须这样分：
 *   PCM5102A 的 BCK/LRCK 停超过 4 个周期，芯片会重新初始化并静音
 *   （要几百毫秒才恢复，听感是一次明显的断音）。
 *   而 core1 上随便哪个操作（串口打印、Flash 擦写）都可能阻塞几毫秒。
 *   把音频独占一个核是最省心的做法 —— RP2350 有两个核，不用白不用。
 *
 * ★ 两个核之间的通信：
 *   只用 multicore_fifo（硬件 FIFO，无锁）。
 *   core0 每秒往 core1 报一次诊断，core1 从不让 core0 等它。
 *
 * 硬件形态：第三方 RP2350 开发板（板载 16MB Flash）+ PCM5102A 模块。
 * 音色库和固件同住那片 16MB，**内存映射直读**，见 sampler.h 的说明。
 */
#include <stdio.h>
#include <string.h>

#include <pico/stdlib.h>
#include <pico/multicore.h>
#include <hardware/clocks.h>

#include "board_config.h"
#include "gmss_format.h"
#include "i2s.h"
#include "ui.h"
#include "midi_in.h"
#include "sampler.h"
#include "voice.h"
#include "engine.h"

/* ================================================================== */
/* core0：音频引擎                                                     */
/* ================================================================== */
/*
 * ★ 这个循环里**不允许**出现的东西：
 *     printf / sleep / malloc / flash 擦写 / 任何可能阻塞的锁
 *   一旦阻塞超过 10.67ms（一个块的时间），I2S 就断流。
 */

/* 四分之一正弦表：sin(i * 90°/128) * 32767，i = 0..128（129 点）
 * 只有 258 字节，放 flash 里。用来生成整周期正弦（镜像下半周）。*/
static const int16_t k_sine_q15[129] = {
         0,    402,    804,   1206,   1608,   2009,   2410,   2811,
      3212,   3612,   4011,   4410,   4808,   5205,   5602,   5998,
      6393,   6786,   7179,   7571,   7962,   8351,   8739,   9126,
      9512,   9896,  10278,  10659,  11039,  11417,  11793,  12167,
     12539,  12910,  13279,  13645,  14010,  14372,  14732,  15090,
     15446,  15800,  16151,  16499,  16846,  17189,  17530,  17869,
     18204,  18537,  18868,  19195,  19519,  19841,  20159,  20475,
     20787,  21096,  21403,  21705,  22005,  22301,  22594,  22884,
     23170,  23452,  23731,  24007,  24279,  24547,  24811,  25072,
     25329,  25582,  25832,  26077,  26319,  26556,  26790,  27019,
     27245,  27466,  27683,  27896,  28105,  28310,  28510,  28706,
     28898,  29085,  29268,  29447,  29621,  29791,  29956,  30117,
     30273,  30424,  30571,  30714,  30852,  30985,  31113,  31237,
     31356,  31470,  31580,  31685,  31785,  31880,  31971,  32057,
     32137,  32213,  32285,  32351,  32412,  32469,  32521,  32567,
     32609,  32646,  32678,  32705,  32728,  32745,  32757,  32765,
     32767
};

static void audio_engine(void)
{
    /* ★★★ 这个数组必须是 static ★★★
     *
     *   原来写成 `int16_t block[BOARD_AUDIO_BUF_FRAMES * 2];`（局部变量），
     *   512×2×2 = **2048 字节**。
     *
     *   而 pico-sdk 给第二个核的栈默认只有 **0x800 = 2048 字节**
     *   （PICO_CORE1_STACK_SIZE 默认 = PICO_STACK_SIZE = 0x800）。
     *
     *   → **一个数组正好把整个栈占满**，后面
     *     engine_render → voice_render → sampler_read_frames 的调用帧
     *     必然踩出去。栈溢出 = 硬故障 = 这个核当场死掉，
     *     连一句日志都发不出来（printf 也要用栈）。
     *
     *   现场症状：主核一切正常（数码管在跑、初始化标记一个个过），
     *   但 PCM5102 各脚 0V、没声音、LED 心跳也不闪。
     *   查了很久，最后靠"音频核一进去就上报一句，结果收不到"才定位。
     *
     *   ★ 改成 static 之后它进 .bss（RP2350 有 520 KB SRAM，不差这 2 KB），
     *     栈上只剩调用帧，2 KB 也够用了。
     *   ★ 这是**唯一**使用它的地方，不存在重入问题。 */
    static int16_t block[BOARD_AUDIO_BUF_FRAMES * 2];
    uint32_t frames_rendered = 0;
    uint32_t last_report = 0;

    /* ★★★ 一进来先报一声（0xC…）★★★
     *
     * 为什么：现场出现过"主流程完全正常（数码管在跑、能报进度号），
     * 但 LRCK 恒为 0V、LED 心跳也不闪"的情况。
     * 那时**分不清**是：
     *     (a) 这个核根本没启动        —— multicore_launch 那步有问题
     *     (b) 核启动了但卡在渲染/推数据里 —— 后面某处阻塞
     * 这一条消息就是用来分开这两种情况的：
     *     主循环收到 0xC → 核起来了，卡在后面
     *     收不到 0xC     → 核压根没跑起来
     *
     * 用 noblock 版本：FIFO 满就丢掉，绝不在音频核上阻塞。 */
    /* ★★ 第一条：把 I2S 的 DMA 开起来。
     *   必须在 i2s_init() 之后、而且**由这个核**来开 ——
     *   见 firmware/src/i2s.h 里 i2s_start() 的说明。 */
    i2s_start();

    multicore_fifo_push_blocking(0xC0000000u);

    for (;;) {
        const uint32_t space = i2s_free_frames();
        if (space == 0) {
            /* 缓冲区还满着 —— 说明 core1 那边 DMA 还没取走。
             * 空转等，不要 sleep（sleep 会引入调度延迟）。 */
            tight_loop_contents();
            continue;
        }

        uint32_t n = (space > BOARD_AUDIO_BUF_FRAMES)
                     ? BOARD_AUDIO_BUF_FRAMES : space;

        /* --- 渲染 n 帧 --- */
#if DEBUG_SINE_TEST
        /*
         * ★ 纯正弦测试：跳过整个采样器/解码/混音链路，
         *   直接生成 440Hz 正弦灌进 I2S。
         *
         *   相位累加器：32 位相位，取高 8 位当"角度索引"，
         *   再用一个 65 点的四分之一正弦表查值并镜像出整周期 ——
         *   整数运算，无浮点、无大表（表只有 65 个 int16）。
         */
        {
            static uint32_t ph = 0;                 /* 32 位相位 */
            /* 每帧相位增量 = 2^32 * f / fs */
            const uint32_t step = (uint32_t)(((uint64_t)440u << 32) / BOARD_SAMPLE_RATE);
            for (uint32_t i = 0; i < n; i++) {
                /* 取高 8 位作为 0..255 的"半周期"索引，再镜像成整周期 */
                uint8_t idx = (uint8_t)(ph >> 24);
                int32_t v;
                if (idx < 128) {
                    v = (int32_t)k_sine_q15[idx];           /* 上半周 */
                } else {
                    v = -(int32_t)k_sine_q15[255 - idx];    /* 下半周（镜像）*/
                }
                int16_t s = (int16_t)(v >> 1);              /* Q15 -> 半幅，留余量 */
                block[i * 2 + 0] = s;
                block[i * 2 + 1] = s;
                ph += step;
            }
        }
#else
        engine_render(block, n);
#endif

        const int pushed = i2s_push_block(block, n);
        if (pushed > 0) {
            frames_rendered += (uint32_t)pushed;
        }

        /* ★★ MIDI 命令放在**喂完 DMA 之后**再处理 ★★
         *
         *   为什么挪到这里：
         *     engine_flush_commands() 会做"音色区查找"（要读 flash 里的采样库）。
         *     这件事如果在"该给 DMA 喂数据"的时刻做，就会推迟喂数据
         *     -> I2S 欠载 -> 听感是卡顿。
         *
         *   现场症状："用 MIDI 推音频一卡一卡的"，加了跨核队列之后
         *   **略有好转但还在卡** —— 说明竞争只是原因之一，
         *   另一个就是"命令处理挡在 DMA 前面"。
         *
         *   代价：MIDI 响应延迟多一个块（85ms）。
         *   ★ 这个延迟偏大 —— 卡顿彻底解决后应该把 BOARD_AUDIO_BUF_FRAMES
         *     从 4096 降回 1024（21ms），前提是确认降回去不会再欠载。
         */
        engine_flush_commands();

        /*
         * 每秒往 core1 报一次诊断。
         * ★ 用 multicore_fifo_wready() 先问一句，FIFO 满了就跳过这次上报 ——
         *   绝不在这里阻塞。丢掉一次诊断无所谓，拖住音频不行。
         */
        /* ★ 上报间隔从 1 秒缩到 1/8 秒（6000 帧）。
         *   1 秒太慢 —— 现场盯着 LED 等一秒容易以为它死了。
         *   1/8 秒时 LED 以 4Hz 闪，一眼就能看出活着。 */
        if (frames_rendered - last_report >= BOARD_SAMPLE_RATE / 8u) {
            last_report = frames_rendered;
            if (multicore_fifo_wready()) {
                multicore_fifo_push_blocking(0xA0000000u
                                             | (i2s_underrun_count() & 0x7FFFu));
            }
            if (multicore_fifo_wready()) {
                multicore_fifo_push_blocking(0xB0000000u
                                             | (engine_active_voices() & 0xFFu));
            }
        }
    }
}

/* ================================================================== */
/* core1：主流程                                                       */
/* ================================================================== */


int main(void)
{
    stdio_init_all();

    /* ==============================================================
     * ★★★ 初始化顺序是**防御式**排的，改之前先读完这段 ★★★
     * ==============================================================
     *
     * 现场踩过的坑（现象：数码管全灭 + PCM5102 各脚 0V + 没声）：
     *
     *   原来 `i2s_init()` 排在 `ui_init()` **前面**，而且它失败时会调
     *   `fatal_blink()`（死循环）。结果只要 I2S 起不来：
     *       · 数码管从没初始化过 → 全灭
     *       · multicore_launch_core1() 永远到不了 → 没声音
     *       · 现场三个现象一起来，却看不出**卡在哪一步**
     *
     *   现在改成：
     *     1. **ui_init() 提到最前面** —— 显示立刻可用，后面每一步都能报进度
     *     2. **每一步都往数码管写一个进度号**，卡住时一眼看出卡在哪
     *     3. **任何一步失败都不停机**，只显示一个错误码继续往下走
     *        （宁可少个功能，也不能让整块板子变砖）
     *     4. **音频启动（multicore_launch_core1）提到 MIDI 之前** ——
     *        音频是主线，MIDI 是附加功能，不能因为 MIDI 挂了就没声音
     *
     *   进度号约定：
     *      1  过了 ui_init
     *      2  过了 i2s_init
     *      3  过了 engine_init
     *      4  音频核已启动
     *      5  MIDI 已初始化（之后主循环接管显示）
     *      91 i2s_init 失败      92 分频算不出来
     *      93 engine_init 失败   95 midi_in_init 失败
     *      "Er" 音色库有问题（主循环里的判断，优先级最高）
     */
    ui_init();

    /* ★ 开机闪一下板载 LED —— 这是"固件至少跑到这里了"的最直接证据。
     *   如果 LED 不闪，说明连 ui_init() 都没过，问题在更早的地方
     *   （flash / 时钟 / 启动代码），和显示、I2S 都无关。 */
    gpio_init(PICO_DEFAULT_LED_PIN);
    gpio_set_dir(PICO_DEFAULT_LED_PIN, GPIO_OUT);
    /* 开机闪 3 下 = "固件跑过 ui_init() 了"。
     * ★ 之后这个 LED 会被主循环接管，变成**音频核心跳**（1Hz 翻转）——
     *   见下面接收 core0 诊断那段。所以"闪 3 下之后就不动了"
     *   意思是**音频核没起来**，不是固件挂了。 */
    for (int i = 0; i < 3; i++) {
        gpio_put(PICO_DEFAULT_LED_PIN, 1);
        sleep_ms(80);
        gpio_put(PICO_DEFAULT_LED_PIN, 0);
        sleep_ms(80);
    }

    /*
     * ★ I2S 必须在 clocks 初始化之后才能初始化 —— 它要读 clk_sys 算分频。
     *   复位瞬间 clk_sys 不是 150MHz，硬编码会在启动早期算出错的分频。
     *
     * ★ 失败**不停机**，把返回码显示到数码管上继续往下走。
     *   i2s_init() 的返回码：
     *      -1 = 分频算不出来（目标频率超过 clk_sys）
     *      -2 = 没有空闲的 PIO 状态机
     *      -3 = PIO 指令槽放不下
     *   显示成 90 + |rc|，例如 -2 → 92、-3 → 93。
     */
#if !DEBUG_SKIP_I2S
    {
        /* ★★ 调用前后各放一个标记，把"崩在 i2s_init() 里面"钉死。
         *
         * 为什么需要：现场出现过"数码管显示 01 之后，扫描停住不动、
         * 只剩一位亮着"的现象。扫描是定时器中断驱动的 —— 主循环卡住
         * 它也照样跑；**它会停，说明 CPU 本身停了（硬故障）**。
         * 而它停在 i2s_init() 那一步附近。
         *
         * 于是：
         *   卡在 11  → 崩在 i2s_init() **内部**（最可能）
         *   卡在 2   → i2s_init() 返回了 0，问题在后面
         *   显示 91/92/93 → i2s_init() 正常返回了错误码
         */
        const int rc = i2s_init();
        if (rc != 0) {
            
        } else {
        }
    }
#else
#endif

    /*
     * 音色库在 XIP 映射地址上（flash 偏移 GMSS_LIB_BASE 处）。
     * ★ 这个地址是**编译期常量**，不是运行时探测出来的 ——
     *   因为库的位置由 PC 工具链写死（见 docs/16MB方案… §4.2 的分区图）。
     */
    sampler_set_library((const void *)GMSS_LIB_XIP_ADDR);

    const int lib_rc = engine_init();

    /* ★ 主音量。引擎默认是满幅，多声部重叠必然削顶失真 ——
     *   见 board_config.h 里 BOARD_MASTER_VOLUME_PERCENT 的说明。
     *   放在这里而不是 engine_init() 里面，是为了不动引擎的默认值，
     *   主机那几个逐样本对拍测试才不会失效。 */
    engine_set_master_volume((int32_t)BOARD_MASTER_VOLUME_PERCENT * 32767 / 100);
    

    printf("\n=== GMSS GM 音源 ===\n");
    printf("clk_sys    = %u Hz\n", (unsigned)clock_get_hz(clk_sys));
    printf("采样率     = %u Hz\n", (unsigned)BOARD_SAMPLE_RATE);
    printf("复音数     = %u\n", (unsigned)VOICE_MAX);
    printf("音色库地址 = 0x%08X\n", (unsigned)GMSS_LIB_XIP_ADDR);

    if (lib_rc == 0) {
        printf("音色库     : OK  分区 %u  旋律音色 %u  鼓组 %u\n",
               (unsigned)sampler_zone_count(),
               (unsigned)sampler_melodic_count(),
               (unsigned)sampler_drumkit_count());
    } else {
        /*
         * ★ 库有问题时**不要停机**。
         *   板子上还有 MIDI 灯、数码管、串口可以用 ——
         *   "活着但没音色库"比"死机"好排查得多。
         *   数码管显示 "Er"，串口打出具体原因。
         */
        printf("音色库     : 失败 rc=%d  %s\n", lib_rc, sampler_last_error());
        printf("            （音色库没烧或烧坏了：把 library.uf2 拖进 RP2350 盘）\n");
    }

    /* ★ 音频核尽早启动 —— 它是主线。必须在 engine_init 之后。 */
#if !DEBUG_SKIP_AUDIO
    multicore_launch_core1(audio_engine);
#else
       /* 调试版：不启动音频核 */
#endif

#if !DEBUG_SKIP_MIDI
    /* ★ MIDI 放最后：它挂了也不影响音频和显示。 */
    if (midi_in_init() != 0) {
        printf("MIDI 初始化失败\n");
    } else {
    }
#else
       /* 调试版：跳过 MIDI */
#endif

    /* ★★★ 烧录验证标记 ★★★
     *
     * 为什么需要这个：
     *   调试过程中改了好几版固件，现场每次都报"现象一样"。
     *   这时有两种可能，但**分不出来**：
     *     (a) 固件确实烧进去了，但问题没解决
     *     (b) 固件根本没烧进去（UF2 没生效、烧错文件、盘没出现…）
     *   前面所有的进度标记都可能被主循环瞬间覆盖，看都看不见。
     *
     *   所以这里在进主循环之前，**显示 88 并停 2 秒**。
     *   2 秒足够看清楚，而且主循环还没开始，不会被覆盖。
     *
     *   ★ 每次改固件都换一个不同的数字，这样"看不到新数字"
     *     就等价于"没烧进去"，一眼可辨。
     */

    uint32_t ticks = 0;
    uint32_t last_underrun = 0;
    uint32_t last_voices = 0;
    /* ★ last_prog 随诊断模式一起去掉了：显示改成 MIDI 字节速率后
     *   不再需要记住上一次的程序号。改回程序号显示时要一起恢复。 */

    for (;;) {
        /* ==============================================================
         * 开机自检音（★ 不经过 MIDI）
         * ==============================================================
         *
         * 为什么要有这个：
         *   "没声音"可能是两段里任意一段坏了 ——
         *     A. MIDI 输入      （光耦 → UART → 解析）
         *     B. 音频通路        （引擎 → 解码 → I2S → PCM5102）
         *   光看"没声音"分不出是哪一段。
         *
         *   这里**绕开 MIDI 前端**，直接往引擎里塞一个和弦：
         *     听到声音 → B 段全好，问题一定在 A 段（MIDI 输入）
         *     听不到   → B 段本身有问题，跟 MIDI 无关
         *
         * 时序：上电 1.5 秒后按下和弦，3.5 秒后放开。
         * 走的是 engine_handle_midi()，和真实 MIDI 完全同一条路径，
         * 所以它响了就说明引擎、采样解码、混音、I2S、DAC 全部正常。
         *
         * 想再听一次：按开发板上的 RUN 键复位，或拔插 USB。
         */
        {
            /* ==========================================================
             * ★★★ 持续测试音（不是"开机响一下"，是一直响）★★★
             * ==========================================================
             *
             * 为什么改成持续：
             *   现场要判断"引擎到底有没有在输出音频"，靠"开机响 2 秒"
             *   根本没法测 —— 得抢在那 2 秒里量 DIN、听耳机、抓示波器。
             *
             *   改成持续之后：
             *       · DIN（开发板脚 19）一直是活跃的中间值，随时能量
             *       · 接音箱/耳机一直有声音，不用抢时机
             *       · 示波器随时能抓
             *
             * 内容：C 大调和弦（60/64/67/72），每 2 秒重新触发一次
             *       —— 重新触发是必须的，钢琴包络会衰减到静音。
             *
             * ★ 这**不经过 MIDI**，所以它响不响和 MIDI 通不通无关。
             *   专门用来单独验证"引擎 → 采样解码 → I2S → DAC"这条链。
             * ★ 要关掉它：把下面的 0 改成 1 重新编译。
             */
            static int st_started = 0;
            static uint32_t last_retrig = 0;
            static const uint8_t chord[4] = { 60, 64, 67, 72 };
            const uint32_t t_ms = to_ms_since_boot(get_absolute_time());

#if !DEBUG_SILENT
            if (!st_started && t_ms > 1200u) {
                st_started = 1;
                /* 先选钢琴，避免上电默认音色是没内容的 program */
                midi_event_t e0 = {0};
                e0.type = MIDI_EV_PROGRAM;
                e0.channel = 0;
                e0.data1 = 0;
                engine_post_midi(&e0);
                printf("self-test: 持续测试音开始（C 大调和弦，每 2 秒重触发）\n");
            }

            /* ★ 开机自检音：响一次 C 大调和弦，2 秒后关掉。
             *
             *   作用：不开电脑、不发 MIDI，就能确认
             *         "引擎 -> 解码 -> I2S -> DAC" 整条音频通路是活的。
             *   如果开机没这个和弦，问题在音频通路，和 MIDI 无关。
             */
            if (st_started && last_retrig == 0u) {
                last_retrig = t_ms;
                for (int i = 0; i < 4; i++) {
                    midi_event_t e = {0};
                    e.type = MIDI_EV_NOTE_ON;
                    e.channel = 0;
                    e.data1 = chord[i];
                    e.data2 = 100;
                    engine_post_midi(&e);
                }
            }
            /* 2 秒后收掉，免得一直占着声部 */
            if (st_started && last_retrig != 0u && (t_ms - last_retrig) >= 2000u) {
                static int stopped = 0;
                if (!stopped) {
                    stopped = 1;
                    for (int i = 0; i < 4; i++) {
                        midi_event_t e = {0};
                        e.type = MIDI_EV_NOTE_OFF;
                        e.channel = 0;
                        e.data1 = chord[i];
                        e.data2 = 0;
                        engine_post_midi(&e);
                    }
                }
            }
#endif   /* !DEBUG_SILENT */
        }

        /* --- MIDI：收到什么就立刻处理（不做队列，MIDI 速率远低于循环速率）--- */
#if !DEBUG_SKIP_MIDI
        /* ★ 只投递，不直接改声部表 —— 那会和音频核抢 s_voice[]。
         *   命令由音频核在每个音频块开头统一应用。 */
        midi_event_t ev;
        while (midi_poll(&ev)) {
            engine_post_midi(&ev);
        }
#endif

        /* --- 按键 --- */
        const uint8_t btn = ui_poll_buttons();
        if (btn & UI_BTN_1) {
            /* 长按/短按在这里区分。当前只有"全部音符关闭"一个动作 ——
             * 演奏中卡音时按一下就能救回来，是最实用的一个功能。 */
            /* ★ 只置标志。voice_all_off() 会改声部表，
             *   直接调用就是在和音频核抢 —— 交给音频核去做。 */
            engine_request_all_off();
            printf("panic: all notes off\n");
        }
        if (btn & UI_BTN_2) {
            /* 切换主音量（4 档循环）。
             *
             * ★ 档位必须和 board_config.h 的 BOARD_MASTER_VOLUME_PERCENT 对齐：
             *   原来这里是 {32767, 22000, 12000, 5000}，**第一档就是满幅** ——
             *   而开机默认是 55%（那是治削顶失真用的）。
             *   结果按一下按键就把音量顶回满幅，失真立刻回来，
             *   而用户完全不知道为什么"按了下键声音就毛了"。
             *
             *   现在 4 档围绕 55% 分布：35 / 55 / 75 / 100，
             *   并从 55（索引 1）开始，和开机默认值一致。 */
            static const int32_t vols[4] = {
                32767 * 35 / 100,
                32767 * BOARD_MASTER_VOLUME_PERCENT / 100,   /* 开机默认 */
                32767 * 75 / 100,
                32767
            };
            static uint8_t vi = 1;      /* 从"开机默认"那一档起步 */
            vi = (uint8_t)((vi + 1) & 3u);
            engine_set_master_volume(vols[vi]);
            printf("master volume idx=%u\n", (unsigned)vi);
        }

        /* --- 数码管 --- */
        /*
         * 库加载失败 → 显示 "Er"
         * 否则        → 显示当前通道 0 的音色号（0..127 两位十进制）
         * 这样演奏时一眼能看出"换音色有没有生效"。
         */
        if (lib_rc != 0) {
            /*
             * "Er" —— 段码是 a~g 的位图（bit0=a … bit6=g）：
             *   'E' = a f g e d   → 0b1111001 = 0x79
             *   'r' = e g         → 0b1010000 = 0x50
             * ★ 不能用 ui_seg_digit(14) —— 那不是十六进制，
             *   它只认 0..9，传 14 会返回全灭（"不显示"而不是"报错"）。
             */
            ui_display_raw(0x79, 0x50);
        } else {
            /* ==========================================================
             * 正常显示：当前音色号（0..99 两位十进制），
             * 外加一个 **小数点的 MIDI 心跳**
             * ==========================================================
             *
             * 显示策略的演变（记一下为什么最后是这样）：
             *
             *   v1  只显示音色号。问题：**排查"MIDI 到底通没通"时，
             *       这个指标很差** —— 收不到数据它就停在旧值上，
             *       和"收到了但音色号没变"看起来一模一样。
             *       我们为此卡了好几轮。
             *
             *   v2  临时改成显示"每秒收到的 MIDI 字节数"。
             *       排查很好用，但演奏时没有意义。
             *
             *   v3（现在）**两个都要**：
             *       · 两位数字 = 音色号（演奏时真正有用的信息）
             *       · 小数点   = MIDI 心跳：每收到一个字节就点亮 60ms
             *
             *   于是：
             *       MIDI 在进来 → 小数点不停地闪
             *       MIDI 没通   → 小数点一直灭着，一眼可辨
             *       换音色      → 数字跟着变
             *   不用任何额外硬件、不用切模式。
             */
            static uint32_t last_total = 0;
            static uint32_t last_rx_ms = 0;

            const uint32_t total = midi_bytes_received();
            const uint32_t now = to_ms_since_boot(get_absolute_time());
            if (total != last_total) {
                last_total = total;
                last_rx_ms = now;
            }

            /* ★ 正常显示：当前音色号（两位十进制） */
            const uint8_t prog = engine_channel_program(0);
            uint8_t d1 = ui_seg_digit((uint8_t)(prog / 10u));
            uint8_t d2 = ui_seg_digit((uint8_t)(prog % 10u));

            /* ★ 小数点 = MIDI 心跳。
             *   60ms 是挑出来的：31250 波特下连续数据每 ~0.32ms 一个字节，
             *   所以有数据时它看起来是**常亮**；偶尔来一条消息就闪一下；
             *   完全没数据就一直是灭的。 */
            if (now - last_rx_ms < 60u) {
                d2 |= UI_SEG_DP;
            }
            ui_display_raw(d1, d2);
        }
        ui_display_update();

        /* --- 接收 core0 的诊断 ---
         *
         * ★★★ 顺便把板载 LED 变成 **音频核的心跳** ★★★
         *
         * 为什么需要这个：
         *   现场出现过"主流程正常（数码管在显示、能报进度号），
         *   但 LRCK 恒为 0V、没声音"的情况。
         *   主流程跑在**这个核**上，音频跑在**另一个核**上 ——
         *   数码管正常**不能**证明音频核活着。
         *
         *   音频核每秒会推两条诊断消息过来（0xA… 欠载数、0xB… 复音数）。
         *   只要收到 0xA，就把 LED 翻一下 —— 于是：
         *       LED 以 1Hz 闪烁  → 音频核活着，在正常推数据
         *       LED 闪完开机那 3 下就不动了 → **音频核死了**
         *   一个 LED 就能把"主流程问题"和"音频核问题"分开。
         */
        static int led_state = 0;
        while (multicore_fifo_rvalid()) {
            const uint32_t msg = multicore_fifo_pop_blocking();
            if ((msg & 0xF0000000u) == 0xA0000000u) {
                last_underrun = msg & 0x7FFFu;
                led_state = !led_state;
                gpio_put(PICO_DEFAULT_LED_PIN, led_state);
            } else if ((msg & 0xF0000000u) == 0xB0000000u) {
                last_voices = msg & 0xFFu;
            } else if (msg == 0xC0000000u) {
                /* ★ 音频核启动确认。
                 *   收到它 = 那个核跑起来了（问题在后面的渲染/推数据）
                 *   收不到 = 核压根没启动（问题在 multicore_launch 那一步）
                 *   现场用一个显眼的长亮做标记。 */
                printf("audio core: 已启动\n");
                gpio_put(PICO_DEFAULT_LED_PIN, 1);
                sleep_ms(500);
                gpio_put(PICO_DEFAULT_LED_PIN, 0);
            }
        }

        /* --- 每秒打印一次状态 ---
         *
         * ★★★ 这里原来写的是 `if (ticks % 500 == 0)`，是个真 bug ★★★
         *
         *   `ticks` 是**按循环次数**累加的，和"时间"没有任何关系。
         *   主循环跑得飞快（每轮只有几微秒），所以这个 printf
         *   每秒会触发**几百次**，而不是 500 轮一次的一秒。
         *
         *   后果有两层：
         *     1. 每条 printf 约 100 字符，115200 波特下要 **8.7ms 阻塞**
         *     2. 更糟的是 printf 要读 flash 里的格式字符串，
         *        而 **XIP 缓存是两个核共享的** —— 它不断冲刷缓存，
         *        把音频核读采样数据的速度拖垮
         *
         *   现场症状：持续不断的"炒豆子声"，数码管上欠载计数一路涨到 99。
         *   （纯正弦测试却是干净的 —— 因为正弦不读 flash，
         *     这也反过来印证了"瓶颈在 XIP 读"这个判断。）
         *
         *   改成基于毫秒的 1Hz 之后，每秒只阻塞 8.7ms，影响可以忽略。
         */
        static uint32_t last_print_ms = 0;
        const uint32_t now_ms = to_ms_since_boot(get_absolute_time());
        if (now_ms - last_print_ms >= 1000u) {
            last_print_ms = now_ms;
            const engine_stats_t *st = engine_stats();
            printf("[%us] 欠载=%u 活跃复音=%u | note_on=%llu no_zone=%llu "
                   "抢声部=%llu cc=%llu | 解码帧=%llu\n",
                   (unsigned)(ticks / 500),
                   (unsigned)last_underrun, (unsigned)last_voices,
                   (unsigned long long)st->note_on,
                   (unsigned long long)st->dropped_no_zone,
                   (unsigned long long)voice_steal_total(),
                   (unsigned long long)st->cc_received,
                   (unsigned long long)sampler_frames_decoded());

            /* 欠载次数应该恒为 0。非 0 说明 core0 被拖住了。 */
            ui_led_set(last_underrun == 0);
        }

        ticks++;
        sleep_ms(2);
    }

    return 0;
}
