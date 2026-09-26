/*
 * midi_in.c — MIDI 解析
 *
 * 分层：
 *   UART 中断 → 字节环形缓冲 → midi_poll() 解析 → 事件队列
 *
 * ★ 中断里只搬字节，不解析。解析器放在主循环，因为：
 *   - 中断要尽量短（31.25kBaud 下每字节 320us，本身不紧张，
 *     但和音频核抢总线是没必要的）
 *   - 解析逻辑要能单元测试，放中断里就没法喂字节测试了
 */
#include <string.h>

#include "pico/stdlib.h"
#include "hardware/uart.h"
#include "hardware/irq.h"

#include "board_config.h"
#include "midi_in.h"

/* ================================================================== */
/* 字节环形缓冲（中断写，主循环读）                                     */
/* ================================================================== */
static volatile uint8_t  s_rx_buf[MIDI_RX_BUF_SIZE];
static volatile uint16_t s_rx_head = 0;
static volatile uint16_t s_rx_tail = 0;
static volatile uint32_t s_rx_overflow = 0;
static volatile uint32_t s_rx_total = 0;
static volatile uint32_t s_framing_err = 0;

static inline void rx_push(uint8_t b)
{
    uint16_t next = (uint16_t)((s_rx_head + 1) % MIDI_RX_BUF_SIZE);
    if (next == s_rx_tail) {
        s_rx_overflow++;        /* 满了就丢；不覆盖未读数据 */
        return;
    }
    s_rx_buf[s_rx_head] = b;
    s_rx_head = next;
    s_rx_total++;
}

static inline bool rx_pop(uint8_t *b)
{
    if (s_rx_tail == s_rx_head) {
        return false;
    }
    *b = s_rx_buf[s_rx_tail];
    s_rx_tail = (uint16_t)((s_rx_tail + 1) % MIDI_RX_BUF_SIZE);
    return true;
}

/* ================================================================== */
/* MIDI 接收：硬件 UART1（RX = GP11）                                   */
/* ================================================================== */
/*
 * ★★★ 这里换过三次实现，记一下为什么最后是硬件 UART ★★★
 *
 *   v1  硬件 UART，但 RX 写成了 GP10。
 *       RP2350 上 GP10 只能做 UART1 的 TX/CTS，做不了 RX ——
 *       光耦接在了一个**发送**脚上，怎么都收不到数据；
 *       而且 TX 空闲时输出高电平，还会把光耦输出顶住
 *       （现场量到"光耦 VO 稳定 3.3V、对 3V3 电阻 0Ω"就是这个）。
 *
 *   v2  用 PIO 在 GP10 上软件实现 UART 接收（uart_rx.pio）。
 *       时序能对上（每 bit 8 个 PIO 周期、分频 600 整数），
 *       但**一进主循环就冻结** —— 初始化标记全过（含 05 = init 成功），
 *       主循环第一句 midi_poll() 就死。加了两道保险也没解决，
 *       原因始终没查到（需要 SWD 调试器才能看清）。
 *
 *   v3（现在）**飞线把光耦从 GP10 挪到 GP11**，用回硬件 UART。
 *       GP11 才是 UART1 的 RX（AUX 模式下）。
 *       中断驱动、不占主循环、没有任何阻塞调用 ——
 *       整类"轮询导致卡死"的问题从根上消失。
 *
 *   ★ 教训：**先把引脚选对，再谈实现。**
 *     引脚选错之后，后面所有"补救实现"都是在跟一个错误的前提较劲。
 */

static bool s_ready = false;   /* init 成功才允许主循环碰 UART */

static void __isr midi_uart_irq(void)
{
    uart_inst_t *u = (MIDI_UART_INSTANCE == 1) ? uart1 : uart0;

    while (uart_is_readable(u)) {
        rx_push((uint8_t)uart_getc(u));
    }
}

/* ★★★ 这里改成**主循环直接轮询 UART**，而不是只靠中断 ★★★
 *
 * 为什么：飞线到 GP11 之后，现场量到 GP11 有 1.3V（信号确实进芯片了），
 * 但一个字节都收不到 —— 说明问题出在"中断路径"上。
 *
 * 于是这里改成两条路都走：
 *   1. 中断照常使能（rx_push 从 ISR 里进）
 *   2. **主循环每轮也直接查一次 UART 的 RX FIFO**
 *
 * 轮询版本是安全的：`uart_is_readable()` 只是读一个状态位，**不会阻塞**；
 * 而且 FIFO 深度有限，循环次数天然有上限。
 * （之前 PIO 那个版本的 `pio_sm_get()` 是**阻塞**函数，才会卡死主循环 ——
 *   这两个不是一回事。）
 *
 * ★ 诊断意义：
 *     改完之后能收到 → 是中断路径的问题（优先级/使能/冲突）
 *     还是收不到     → 那问题在更前面（引脚功能号 / 波特率 / 极性）
 */
void midi_in_drain(void)
{
    if (!s_ready) {
        return;
    }
    uart_inst_t *u = (MIDI_UART_INSTANCE == 1) ? uart1 : uart0;
    while (uart_is_readable(u)) {
        rx_push((uint8_t)uart_getc(u));
    }
}

/* ================================================================== */
/* 解析器状态机                                                        */
/* ================================================================== */
/*
 * MIDI 消息长度（不含状态字节）：
 *   0x8n/0x9n/0xAn/0xBn/0xEn → 2 字节
 *   0xCn/0xDn                → 1 字节
 *   0xFn                     → 系统消息，本工程只处理真时事件
 */
static uint8_t s_status = 0;      /* 当前 running status，0 = 无 */
static uint8_t s_data[2];
static uint8_t s_ndata = 0;
static uint8_t s_expected = 0;    /* 该状态字节需要几个数据字节 */
static bool    s_in_sysex = false;

static uint8_t expected_data_bytes(uint8_t status)
{
    switch (status & 0xF0u) {
    case MIDI_NOTE_OFF:
    case MIDI_NOTE_ON:
    case MIDI_POLY_PRESSURE:
    case MIDI_CTRL_CHANGE:
    case MIDI_PITCH_BEND:
        return 2;
    case MIDI_PROGRAM_CHANGE:
    case MIDI_CHAN_PRESSURE:
        return 1;
    default:
        return 0;
    }
}

static bool make_event(midi_event_t *out)
{
    uint8_t st = s_status;
    uint8_t ch = st & 0x0Fu;

    switch (st & 0xF0u) {
    case MIDI_NOTE_ON:
        /* ★ 力度 0 的 Note On 等同 Note Off —— 很多设备这么用，
         *   不处理会导致"卡音"。 */
        out->type = (s_data[1] == 0) ? MIDI_EV_NOTE_OFF : MIDI_EV_NOTE_ON;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = s_data[1];
        return true;

    case MIDI_NOTE_OFF:
        out->type = MIDI_EV_NOTE_OFF;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = s_data[1];
        return true;

    case MIDI_CTRL_CHANGE:
        out->type = MIDI_EV_CONTROL;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = s_data[1];
        return true;

    case MIDI_PROGRAM_CHANGE:
        out->type = MIDI_EV_PROGRAM;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = 0;
        return true;

    case MIDI_PITCH_BEND:
        out->type = MIDI_EV_PITCH_BEND;
        out->channel = ch;
        /* 14 位：低 7 位在前 */
        out->bend = (uint16_t)(((uint16_t)s_data[1] << 7) | s_data[0]);
        out->data1 = 0;
        out->data2 = 0;
        return true;

    case MIDI_CHAN_PRESSURE:
        out->type = MIDI_EV_CHAN_PRESSURE;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = 0;
        return true;

    case MIDI_POLY_PRESSURE:
        out->type = MIDI_EV_POLY_PRESSURE;
        out->channel = ch;
        out->data1 = s_data[0];
        out->data2 = s_data[1];
        return true;

    default:
        return false;
    }
}

/*
 * 喂一个字节，若凑齐一条完整消息就**立即**产出事件。
 * 返回 true 表示 out 已填好。
 *
 * ★ 不要写成"先喂字节、再回头判断是否凑齐"——那样要靠比较
 *   s_ndata 的前后值来推断，一旦解析逻辑改动就会静默失效。
 *   这里在"收满最后一个数据字节"的分支里直接构造事件，路径唯一。
 */
static bool feed_byte(uint8_t b, midi_event_t *out)
{
    /* 真时事件（0xF8..0xFF）：可插在任意消息中间，不改变 running status */
    if (b >= MIDI_REALTIME_MIN) {
        return false;           /* 本项目不用时钟/播放控制，忽略 */
    }

    if (b & 0x80u) {
        /* ---- 状态字节 ---- */
        if (b == MIDI_SYSEX_START) {
            s_in_sysex = true;
            s_ndata = 0;
            return false;
        }
        if (b == MIDI_SYSEX_END) {
            s_in_sysex = false;
            s_ndata = 0;
            return false;
        }
        if (b >= 0xF0u) {
            /* 其他系统消息（0xF1..0xF7）：不支持，丢弃。
             * ★ 系统消息会**清除** running status（MIDI 规范）。 */
            s_status = 0;
            s_expected = 0;
            s_ndata = 0;
            s_in_sysex = false;
            return false;
        }
        s_status = b;
        s_expected = expected_data_bytes(b);
        s_ndata = 0;
        return false;
    }

    /* ---- 数据字节 ---- */
    if (s_in_sysex || s_status == 0 || s_expected == 0) {
        return false;           /* SysEx 内容 / 无 running status，丢弃 */
    }

    s_data[s_ndata++] = b & 0x7Fu;
    if (s_ndata < s_expected) {
        return false;           /* 还差数据字节 */
    }

    s_ndata = 0;                /* 收满，准备下一条（running status 保持） */
    return make_event(out);
}

bool midi_poll(midi_event_t *out)
{
    /* ★ 先把 PIO 收到的字节搬进环形缓冲。
     *   放这里而不是中断里：PIO 的 RX FIFO 只有 4 级，
     *   31250 波特下每字节 320µs，主循环远快于此，不会溢出。 */
    midi_in_drain();

    uint8_t b;
    while (rx_pop(&b)) {
        if (feed_byte(b, out)) {
            return true;
        }
    }
    return false;
}

/* 单元测试入口：绕过 UART，直接喂字节 */
bool midi_feed_byte(uint8_t b, midi_event_t *out)
{
    return feed_byte(b, out);
}

/* ================================================================== */
/* 初始化                                                              */
/* ================================================================== */

/* ★★ MIDI 接收走**硬件 UART**（GP11 = UART1 RX via AUX 功能）。
 *
 *   之前在这条注释里写着"走 PIO"，是因为当时光耦接在 GP10 上 ——
 *   而 GP10 只能做 UART1 的 TX / CTS。后来飞线把它挪到了 GP11，
 *   硬件 UART 就能用了，PIO 接收器（uart_rx.pio）随之停用。
 *
 *   详细的三次实现演变见下面"MIDI 接收"那一节。 */

/* ★ 引脚号必须和 UART1 的实际分组一致（见 board_config.h 的长注释）。
 *   写错的症状是"光耦在翻转但一个字节都收不到"。 */
_Static_assert(PIN_MIDI_RX == 11, "GP11 才是 UART1 的 RX（AUX 模式）");
_Static_assert(PIN_MIDI_TX == 10, "GP10 是 UART1 的 TX（AUX 模式）");
_Static_assert(MIDI_UART_INSTANCE == 1, "GPIO10/11 只映射到 UART1");
_Static_assert(MIDI_BAUD == 31250u, "MIDI 必须 31250 波特");

int midi_in_init(void)
{
    uart_inst_t *u = (MIDI_UART_INSTANCE == 1) ? uart1 : uart0;

    /* ★ 先设引脚功能，再 uart_init —— SDK 的示例就是这么排的
     *   （注释写着"doing this before uart_init avoids losing data"）。 */
    gpio_set_function(PIN_MIDI_RX, UART_FUNCSEL_NUM(u, PIN_MIDI_RX));
    gpio_set_function(PIN_MIDI_TX, UART_FUNCSEL_NUM(u, PIN_MIDI_TX));
    gpio_disable_pulls(PIN_MIDI_RX);    /* 外部已有 1kΩ 上拉 */

    uart_init(u, MIDI_BAUD);
    uart_set_format(u, 8, 1, UART_PARITY_NONE);
    uart_set_fifo_enabled(u, true);
    uart_set_hw_flow(u, false, false);

    /* 清掉上电时的杂散字节 */
    while (uart_is_readable(u)) {
        (void)uart_getc(u);
    }

    irq_set_exclusive_handler(MIDI_UART_INSTANCE == 1 ? UART1_IRQ : UART0_IRQ,
                              midi_uart_irq);
    irq_set_enabled(MIDI_UART_INSTANCE == 1 ? UART1_IRQ : UART0_IRQ, true);
    uart_set_irq_enables(u, true /* rx */, false /* tx */);

    s_status = 0;
    s_ndata = 0;
    s_expected = 0;
    s_in_sysex = false;

    s_ready = true;
    return 0;
}

uint32_t midi_rx_overflow_count(void) { return s_rx_overflow; }
uint32_t midi_bytes_received(void)    { return s_rx_total; }
uint32_t midi_framing_errors(void)    { return s_framing_err; }
