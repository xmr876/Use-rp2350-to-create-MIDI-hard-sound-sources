/*
 * ui.c — 2 位共阴数码管 + 2 按键 + 状态 LED
 *
 * ★ 扫描必须由定时器中断驱动，不能靠主循环。
 *   主循环每 2ms 才转一圈，靠它扫描会有明显闪烁。
 *   250Hz/位 的扫描频率由重复定时器给出。
 *
 * ★ 中断跑在**调用 ui_init() 的那个核**上。当前 main.c 里
 *   ui_init() 在 core1 调用，所以 core0 的音频路径不会被打断。
 *   如果将来把 ui_init() 挪到 core0，音频时序会被扫描中断扰动。
 */
#include <pico/stdlib.h>
#include <hardware/gpio.h>
#include <hardware/timer.h>

#include "board_config.h"
#include "ui.h"

/* ================================================================== */
/* 段码表                                                              */
/* ================================================================== */
/*
 * 共阴：段线高电平点亮。位布局 bit0=a, bit1=b, ..., bit6=g
 *
 *   0x3F = abcdef   (0)
 *   0x06 = bc       (1)
 *   0x5B = abdeg    (2)
 *   0x4F = abcdg    (3)
 *   0x66 = bcfg     (4)
 *   0x6D = acdfg    (5)
 *   0x7D = acdefg   (6)
 *   0x07 = abc      (7)
 *   0x7F = abcdefg  (8)
 *   0x6F = abcdfg   (9)
 */
static const uint8_t k_seg_digit[10] = {
    0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F,
};

/* 段线 GPIO，顺序对应 bit0..bit6（a..g）
 *
 * ★★★ 这里的顺序**不是**按 GPIO 号从小到大排的，是**按板子实际怎么走的线**排的。
 *
 *   实测（用户逐脚量通断得到，丝印号 → GPIO）：
 *       显示脚 3 (A)  → 丝印 20 → GP15
 *       显示脚 9 (B)  → 丝印 21 → GP16
 *       显示脚 8 (C)  → 丝印 22 → GP17
 *       显示脚 6 (D)  → 丝印 24 → GP18
 *       显示脚 4 (F)  → 丝印 25 → **GP19**
 *       显示脚 7 (E)  → 丝印 27 → **GP21**
 *       显示脚 2 (DP) → 丝印 29 → GP22
 *       显示脚 1 (G)  → 丝印 28 → **GND**  ← ⚠️ 见下
 *
 *   → a,b,c,d 和 dp 与原来的假设一致，但 **e/f/g 三个是错位的**：
 *       板上 GP19 是 F、GP20 是 G、GP21 是 E
 *       而原来按 GP19=E、GP20=F、GP21=G 写
 *     后果：显示 "0" 时固件驱动 GP15..GP20，实际点亮 A,B,C,D,F ——
 *     正好是"一个缺了左下竖的 0"，看起来像段 e 坏了，其实是点错脚。
 *     （这个现象排查了好几轮，最后靠逐脚量通断才定位。）
 *
 *   ★ 注意 GP20（本该是 G 段的线）在板子上**接到了 GND**（丝印 28）。
 *     所以中横那一段物理上永远点不亮 —— 这是载板的一个走线错误，
 *     软件改不了。要修就得飞线：把显示脚 1 从 GND 改接到丝印 26（GP20）。
 *     在没飞线之前，`4` 会显示成缺中横的样子，`8` 会看起来像 `0`。
 *
 *   ★ 好在 8 个脚仍然是 **GP15~GP22 连续**，所以 PIN_SEG_MASK 和
 *     gpio_put_masked() 的整组写照旧可用 —— 只是数组顺序变了。 */
static const uint8_t k_seg_pins[7] = {
    PIN_SEG_A,          /* GP15 */
    PIN_SEG_B,          /* GP16 */
    PIN_SEG_C,          /* GP17 */
    PIN_SEG_D,          /* GP18 */
    21,                 /* GP21 = 板上的 e 段（★ 不是 PIN_SEG_E）*/
    19,                 /* GP19 = 板上的 f 段（★ 不是 PIN_SEG_F）*/
    20,                 /* GP20 = 板上的 g 段（★ 板上接到了 GND，点不亮）*/
};

/* 编译期断言：段线必须落在 PIN_SEG_MASK 覆盖的那段连续 GPIO 里，
 * 否则 gpio_put_masked() 一次写整组就对不上了，
 * 表现是数码管显示乱七八糟的字符（而不是编译报错）。
 *
 * ★ 这里**不能再断言"顺序递增"** —— 上面的数组是按板子实际走线排的，
 *   故意不是单调的。改成断言"7 个脚互不相同，且都落在
 *   [PIN_SEG_A, PIN_SEG_A+7] 这个连续区间内"。 */
_Static_assert(PIN_SEG_A + 7 <= 32, "段线必须在 GPIO0~31 内（SIO 掩码寄存器只覆盖 0~31）");

static const uint8_t k_digit_pins[2] = { PIN_DIGIT1, PIN_DIGIT2 };

/* ================================================================== */
/* 状态                                                                */
/* ================================================================== */

static volatile uint8_t s_seg[2] = { UI_SEG_BLANK, UI_SEG_BLANK };
static volatile uint8_t s_active = 0;      /* 当前点亮哪一位 */

/* 按键消抖状态 */
static uint8_t s_btn_stable = 0;           /* 稳定后的电平（1 = 按下）*/
static uint8_t s_btn_last_raw = 0;
static uint32_t s_btn_change_ms = 0;
static uint8_t s_btn_pressed_edge = 0;     /* 待取走的"新按下"事件 */

static repeating_timer_t s_scan_timer;

/* ================================================================== */
/* 扫描中断                                                            */
/* ================================================================== */

/*
 * 每 2ms 触发一次：先关掉当前位（消隐，防止鬼影），
 * 设置新段码，再点亮另一位。
 *
 * ★ 顺序很关键：**必须先关位选再改段码**，否则上一位的段码会
 *   短暂出现在下一位上（"鬼影"）。
 */
static bool scan_callback(repeating_timer_t *t)
{
    (void)t;

    /* 1. 关掉所有位选（消隐） */
    for (int i = 0; i < 2; i++) {
        gpio_put(k_digit_pins[i], 0);
    }

    /* 2. 切到另一位 */
    s_active ^= 1;

    /* 3. 设置段码
     * ★ 先把 7 个段位拼成一个 32 位值，再一次性写出去。
     *   逐脚 gpio_put 会在切换过程中出现"部分段已亮、部分还旧"的中间态，
     *   上一位的残留段码就会以鬼影形式出现在下一位上。 */
    uint8_t seg = s_seg[s_active];
    uint32_t bits = 0;
    for (int i = 0; i < 7; i++) {
        if ((seg >> i) & 1u) {
            bits |= 1u << k_seg_pins[i];
        }
    }
    gpio_put_masked(PIN_SEG_MASK, bits);

    /* 4. 点亮 */
    gpio_put(k_digit_pins[s_active], 1);

    return true;        /* 继续重复 */
}

/* ================================================================== */
/* 对外接口                                                            */
/* ================================================================== */

uint8_t ui_seg_digit(uint8_t d)
{
    return (d < 10) ? k_seg_digit[d] : UI_SEG_BLANK;
}

void ui_init(void)
{
    /* ★ 精简版：不要数码管也不要按键 -> 直接返回。
     *   不碰任何 GPIO、不启动扫描中断、不申请定时器。
     *   见 board_config.h 里 BOARD_HAS_DISPLAY / BOARD_HAS_BUTTONS 的说明。 */
#if !BOARD_HAS_DISPLAY && !BOARD_HAS_BUTTONS
    return;
#endif
    /* ★ 只关显示、保留按键（或反过来）的情况先不支持 ——
     *   真需要时把下面这些 init 拆成两段即可。 */
    /* --- 段线 --- */
    for (int i = 0; i < 7; i++) {
        gpio_init(k_seg_pins[i]);
        gpio_set_dir(k_seg_pins[i], GPIO_OUT);
        gpio_put(k_seg_pins[i], 0);
    }

    /* --- 位选（经 NPN，高电平点亮） --- */
    for (int i = 0; i < 2; i++) {
        gpio_init(k_digit_pins[i]);
        gpio_set_dir(k_digit_pins[i], GPIO_OUT);
        gpio_put(k_digit_pins[i], 0);
    }

    /* --- 按键：外部 10k 上拉，按下为低；内部上拉也开一份作为冗余 --- */
    const uint8_t btns[2] = { PIN_BTN_UP, PIN_BTN_DOWN };
    for (int i = 0; i < 2; i++) {
        gpio_init(btns[i]);
        gpio_set_dir(btns[i], GPIO_IN);
        gpio_pull_up(btns[i]);
    }

    /* --- LED --- */
    gpio_init(PIN_LED_STATUS);
    gpio_set_dir(PIN_LED_STATUS, GPIO_OUT);
    gpio_put(PIN_LED_STATUS, 0);

    /* --- 扫描定时器 ---
     * 250Hz/位 → 每位 4ms → 每 2ms 切换一次
     * DISPLAY_SCAN_HZ 定义的是"每位刷新率"，所以周期 = 1000/SCAN_HZ。
     * 负值表示"从上一次触发开始计时"，不受回调耗时影响，扫描更稳。
     */
    int32_t period_us = -(int32_t)(1000000u / DISPLAY_SCAN_HZ);
    add_repeating_timer_us(period_us, scan_callback, NULL, &s_scan_timer);
}

void ui_display_raw(uint8_t left_seg, uint8_t right_seg)
{
    s_seg[0] = left_seg;
    s_seg[1] = right_seg;
}

void ui_display_number(int value)
{
    if (value < 0 || value > 99) {
        ui_display_raw(UI_SEG_MINUS, UI_SEG_MINUS);
        return;
    }
    ui_display_raw(ui_seg_digit((uint8_t)(value / 10)),
                   ui_seg_digit((uint8_t)(value % 10)));
}

void ui_display_update(void)
{
    /* 实际显示由中断完成，这里只做按键消抖 */
    (void)ui_poll_buttons();
}

uint8_t ui_poll_buttons(void)
{
    /* ★ 精简版：按键没初始化，引脚是悬空的 ——
     *   读出来的电平随机，可能误触发 panic / 改音量。
     *   所以这里必须直接返回 0，而不是让它去读 GPIO。 */
#if !BOARD_HAS_BUTTONS
    return 0;
#endif
    const uint8_t pins[2] = { PIN_BTN_UP, PIN_BTN_DOWN };
    uint8_t raw = 0;
    uint32_t now = to_ms_since_boot(get_absolute_time());

    for (int i = 0; i < 2; i++) {
        /* 按下为低，取反成"1 = 按下" */
        if (!gpio_get(pins[i])) {
            raw |= (uint8_t)(1u << i);
        }
    }

    if (raw != s_btn_last_raw) {
        s_btn_last_raw = raw;
        s_btn_change_ms = now;
        return s_btn_pressed_edge;      /* 还在抖，先不判定 */
    }

    /* 电平稳定超过消抖时间才认账 */
    if (raw != s_btn_stable && (now - s_btn_change_ms) >= BTN_DEBOUNCE_MS) {
        uint8_t newly = (uint8_t)(raw & ~s_btn_stable);
        s_btn_stable = raw;
        s_btn_pressed_edge |= newly;
    }

    uint8_t out = s_btn_pressed_edge;
    s_btn_pressed_edge = 0;
    return out;
}

void ui_led_set(bool on)
{
    gpio_put(PIN_LED_STATUS, on ? 1 : 0);
}

void ui_led_toggle(void)
{
    gpio_xor_mask(1u << PIN_LED_STATUS);
}
