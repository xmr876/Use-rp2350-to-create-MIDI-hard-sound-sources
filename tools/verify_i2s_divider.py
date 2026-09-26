"""
verify_i2s_divider.py — 验证 PIO I2S 分频计算

为什么单独验证这个
------------------
可行性文档 §3.1 选 48kHz 而不是 44.1kHz 的唯一理由就是"分频可精确表示"。
如果实现里的整数运算写错（少乘一位、移位方向反了），抖动就回来了，
而且症状是"偶尔有杂音"，极难定位。

参数来源（必须与代码一致）：
  board_config.h  BOARD_SAMPLE_RATE        = 48000
                  BOARD_I2S_FRAME_BITS     = 64
                  BOARD_I2S_PIO_CYCLES_PER_BCLK = 4   ← i2s.pio 的循环体是 4 条指令
  i2s.c           分频 = clock_get_hz(clk_sys) / (采样率 × 64 × 4)

运行:  python tools/verify_i2s_divider.py
"""
from __future__ import annotations

import sys

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print("  OK   " + msg)
    else:
        FAILS += 1
        print("  FAIL " + msg)


def compute_divider(sys_hz: int, bclk_hz: int, cpb: int) -> tuple[int, int]:
    """复刻 i2s.c::i2s_compute_divider 的整数算法"""
    want = bclk_hz * cpb
    div = sys_hz // want
    rem = sys_hz % want
    frac = (rem << 8) // want
    return div, frac


def exact_divider(sys_hz: int, bclk_hz: int, cpb: int) -> float:
    return sys_hz / (bclk_hz * cpb)


# ==================================================================
SYS = 150_000_000
SR = 48_000
FRAME_BITS = 64
# i2s.pio 循环体 10 条指令、每条 1 周期 → 每 bit 10 个 SM 周期
CPB = 10
BCLK = SR * FRAME_BITS
SM_HZ = BCLK * CPB

print("=== 参数（必须与 board_config.h / i2s.pio 一致）===")
print("  系统时钟      %d Hz" % SYS)
print("  采样率        %d Hz" % SR)
print("  帧长          %d bit" % FRAME_BITS)
print("  每 BCLK 周期数 %d  (i2s.pio: out + 8×nop + jmp)" % CPB)
print("  BCLK          %d Hz" % BCLK)
print("  SM 频率       %d Hz" % SM_HZ)
print()

exact = exact_divider(SYS, BCLK, CPB)
print("=== 精确分频 ===")
print("  %d / %d = %.10f" % (SYS, SM_HZ, exact))
int_part = int(exact)
frac_part = exact - int_part
print("  = %d + %.10f" % (int_part, frac_part))
print("  小数部分 × 256 = %.10f" % (frac_part * 256))
check(abs(frac_part * 256 - round(frac_part * 256)) < 1e-9,
      "可被 8 位小数精确表示 → 零抖动")
check(int_part == 4 and round(frac_part * 256) == 226,
      "整数部分 4、小数部分 226（即 div=4 frac=226）")

print()
print("=== 各可选 C 值的对比（供选型参考）===")
NEED = SYS / (SR * FRAME_BITS)
print("  每 bit 可用总分频 = %d / (%d × %d) = %.6f" % (SYS, SR, FRAME_BITS, NEED))
print()
print("   ★ 关键认识：PIO 的分数分频器是**按 SM 周期**累加的。")
print("     每个 SM 周期消耗 D 个系统时钟，所以平均 SM 频率恒为 sys_clk/D，")
print("     与程序循环多长无关。只要 D 能被 8 位小数表示，**平均 BCLK 就精确**。")
print("     逐周期的相位抖动则取决于 delay 语义与硬件实现，")
print("     无法在没有硬件的情况下推导 —— 必须上示波器看。")
print()
print("   C    分频 D       frac×256  SM频率MHz  BCLK上升沿相对 bit 起点")
for c in (2, 4, 5, 10, 20, 25):
    d = NEED / c
    fp = d - int(d)
    x256 = fp * 256
    sm = SR * FRAME_BITS * c / 1e6
    # 若循环体里 BCK 上升在第 R 条 1 周期指令，则它落在 bit 的 R/C 处
    r = 5 if c == 10 else (2 if c == 4 else 1)
    print("   %-4d %-10.8f %-9.1f %-10.3f  %d/%d"
          % (c, d, x256, sm, r, c))
print()
check(abs((NEED / CPB) * 256 - round((NEED / CPB) * 256)) < 1e-9,
      "选定 C=%d 的分频可被 8 位小数精确表示 → 平均频率精确" % CPB)
check(SM_HZ == 30_720_000, "SM 频率 30.72 MHz")

print()
print("=== 整数算法 vs 精确值 ===")
d, f = compute_divider(SYS, BCLK, CPB)
got = d + f / 256.0
print("  compute_divider → div=%d frac=%d → %.10f" % (d, f, got))
check(abs(got - exact) < 1e-12, "整数算法与精确值完全一致（零误差）")

print()
print("=== 穷举：各种系统时钟下的量化误差 ===")
cases = []
for sysclk in (150_000_000, 133_000_000, 125_000_000, 100_000_000, 200_000_000):
    for sr in (48000, 44100, 96000, 32000):
        cases.append((sysclk, sr))

worst = 0.0
worst_case = None
zero_jitter = 0
total = 0
for (sysclk, sr) in cases:
    bclk = sr * FRAME_BITS
    if bclk * CPB > sysclk:
        continue
    total += 1
    d, f = compute_divider(sysclk, bclk, CPB)
    got = d + f / 256.0
    exp = exact_divider(sysclk, bclk, CPB)
    err = abs(got - exp)
    if err < 1e-12:
        zero_jitter += 1
    if err > worst:
        worst, worst_case = err, (sysclk, sr, got, exp)

print("  测试 %d 组参数" % total)
print("  最大量化误差 %.3e（8 位小数理论上限 %.3e）" % (worst, 1 / 256))
print("  其中 %d 组零量化误差" % zero_jitter)
check(worst <= 1 / 256 + 1e-12, "量化误差在 8 位小数精度内")
print()
print("  ★ 关键对比：为什么选 48kHz 而不是 44.1kHz")
for sr in (48000, 44100):
    bclk = sr * FRAME_BITS
    exp = exact_divider(SYS, bclk, CPB)
    fp = exp - int(exp)
    jitter = fp * 256
    tag = "精确" if abs(jitter - round(jitter)) < 1e-9 else "有舍入"
    print("    %6dHz: 分频 %.10f  小数×256 = %9.5f  → %s"
          % (sr, exp, jitter, tag))
check(abs((exact_divider(SYS, 44100 * FRAME_BITS, CPB) % 1) * 256
          - round((exact_divider(SYS, 44100 * FRAME_BITS, CPB) % 1) * 256)) > 1e-9,
      "44.1kHz 无法精确表示（正是选 48kHz 的理由）")

print()
print("=== 边界：不可行的配置必须报错 ===")
# SM 频率超过 sys_clk → div 会是 0
d, f = compute_divider(100_000_000, 2_000_000 * FRAME_BITS, CPB)
sm = 2_000_000 * FRAME_BITS * CPB
print("  2MHz 采样率 → SM 需 %d Hz，sys_clk 100MHz → div=%d" % (sm, d))
check(sm > 100_000_000 and d == 0, "div==0 即固件的报错判据（返回 -1）")

# 分频整数部分必须是 16 位
d, f = compute_divider(150_000_000, 48_000 * FRAME_BITS, CPB)
check(d <= 65535, "主场景 div=%d 在 16 位范围内" % d)

print()
print("=== 与 board_config.h 的一致性 ===")
from pathlib import Path
cfg = (Path(__file__).resolve().parent.parent
       / "firmware" / "include" / "board_config.h").read_text(encoding="utf-8")
import re
m = re.search(r"#define\s+BOARD_I2S_PIO_CYCLES_PER_BCLK\s+(\d+)", cfg)
check(m is not None, "在 board_config.h 里找到 BOARD_I2S_PIO_CYCLES_PER_BCLK")
if m:
    got_cpb = int(m.group(1))
    print("  board_config.h 里是 %d，本脚本用的是 %d" % (got_cpb, CPB))
    check(got_cpb == CPB, "两处一致（改了 .pio 必须同步改这里）")

m2 = re.search(r"#define\s+BOARD_SAMPLE_RATE\s+(\d+)", cfg)
check(m2 is not None and int(m2.group(1)) == SR, "采样率与 board_config.h 一致")

print()
if FAILS:
    print("%d 项未通过" % FAILS)
    sys.exit(1)
print("全部通过 ✓")
