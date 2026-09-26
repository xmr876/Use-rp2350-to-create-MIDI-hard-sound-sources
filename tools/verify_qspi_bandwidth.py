"""
verify_qspi_bandwidth.py — 数据盘访问方案的带宽与可行性核对

结论（第 3 版，最终）
--------------------
用 **QMI Direct Serial Mode**，不是 PIO，也不是内存映射窗口。

本脚本核对三件事：
  1. 需求侧：48 复音稳态需要多少带宽
  2. 供给侧：direct 模式在不同 CLKDIV 下能给多少
  3. **死机风险**：direct 模式期间不能碰 flash，这个约束有多严重

运行:  python tools/verify_qspi_bandwidth.py
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


SYS_CLK = 150_000_000
SAMPLE_RATE = 48_000
VOICES = 48

# ==================================================================
print("=== 1. 需求侧：最坏情况带宽 ===")
print()

BYTES_PCM16 = 2
BYTES_ADPCM4 = 0.5
AVG_PITCH_RATIO = 1.2

need_pcm16 = SAMPLE_RATE * BYTES_PCM16 * AVG_PITCH_RATIO
need_adpcm = SAMPLE_RATE * BYTES_ADPCM4 * AVG_PITCH_RATIO

print("  单复音（16bit PCM）: %.1f KB/s" % (need_pcm16 / 1024))
print("  单复音（4bit ADPCM）: %.1f KB/s" % (need_adpcm / 1024))
print()
print("  %d 复音全 PCM16 : %.2f MB/s" % (VOICES, need_pcm16 * VOICES / 1e6))
print("  %d 复音全 ADPCM : %.2f MB/s" % (VOICES, need_adpcm * VOICES / 1e6))

# 稳态：起音段只有前 10ms 是 PCM16，循环段是 ADPCM
attack_ms = 10
avg_note_ms = 800
af = attack_ms / avg_note_ms
realistic = (need_pcm16 * af + need_adpcm * (1 - af)) * VOICES
print("  稳态（起音占 %.1f%%）: %.2f MB/s" % (af * 100, realistic / 1e6))
print("  ★ 这是**峰值**需求（48 复音同时持续发声）")
print("    实际持续复音数通常 20~30，需求约为上面的 1/2")
print()

# ==================================================================
print("=== 2. 供给侧：QMI Direct Serial Mode ===")
print()
print("  SCK = sys_clk / (2 × CLKDIV)")
print("  吞吐 = SCK / 8（每个字节 8 个 SCK）")
print()
print("  CLKDIV   SCK(MHz)   理论吞吐(MB/s)   CPU 压力   评价")
for div in (1, 2, 4, 8, 16, 32):
    sck = SYS_CLK / (2 * div)
    if sck > 50e6:
        # W25Q128 的 SPI 模式上限约 50MHz（03h/0Bh 命令）
        tag = "超 W25Q128 SPI 上限"
    else:
        tag = ""
    tput_bytes = sck / 8                     # 字节/秒
    # CPU 压力：每字节要 push TX + pop RX，保守按每字节 8 个 CPU 周期估
    cpu_pct = tput_bytes * 8 / SYS_CLK * 100
    if cpu_pct > 30:
        judge = "CPU 吃紧"
    elif cpu_pct > 12:
        judge = "尚可"
    else:
        judge = "轻松"
    print("  %4d   %8.2f   %12.2f   %6.1f%%   %s %s"
          % (div, sck / 1e6, tput_bytes / 1e6, cpu_pct, judge, tag))

print()
div = 4
sck = SYS_CLK / (2 * div)
tput_bytes = sck / 8
cpu_pct = tput_bytes * 8 / SYS_CLK * 100
print("  选定 CLKDIV = %d" % div)
print("    SCK = %.2f MHz" % (sck / 1e6))
print("    吞吐 = %.2f MB/s" % (tput_bytes / 1e6))
print("    CPU 占用（按每字节 8 周期估）= %.1f%%" % cpu_pct)
margin = tput_bytes / realistic
print("    对峰值需求的余量 = %.2f 倍" % margin)
check(margin > 1.4, "峰值余量 > 1.4 倍")
check(sck <= 50e6, "SCK 未超 W25Q128 的 SPI 模式上限")

margin_real = tput_bytes / (realistic / 2)
print("    对典型需求（24 复音）的余量 = %.2f 倍" % margin_real)
check(margin_real > 2.5, "典型场景余量 > 2.5 倍")

print()
print("  ★★ 真正让这个方案成立的是**每复音 2KB 采样缓存**：")
cache_kb = 2
cache_ms = cache_kb * 1024 / need_adpcm * 1000
print("     2KB ≈ %.0f ms 的 ADPCM 播放量" % cache_ms)
print("     每 %.0f ms 才需要给一个复音补一次数据，" % cache_ms)
print("     所以读取是**突发式**的，可以用 DMA 排队，")
print("     不需要保证每一个采样的实时性。")
print("     ★ 而且 DMA 可以直接驱动 DIRECT_TX/DIRECT_RX FIFO，")
print("       把 CPU 占用从 %.1f%% 降到接近 0。" % cpu_pct)

# ==================================================================
print()
print("=== 3. ★ 死机风险核对（这是 direct 模式最大的坑）===")
print()
print("  QMI 文档原文：")
print("    'Memory-mapped accesses will generate bus errors")
print("     when direct serial mode is enabled.'")
print()
print("  我们的代码正从启动盘 XIP 执行 —— 所以进 direct 模式期间，")
print("  **任何取指或数据访问打到 flash 都会立刻总线错误**。")
print()
print("  必须遵守的三条：")
rules = [
    ("direct 模式的所有代码放 RAM",
     "__not_in_flash_func 标记；否则取指本身就崩"),
    ("期间屏蔽音频 DMA 之外的中断",
     "中断向量在 RAM，但处理函数默认在 flash，一跳就崩"),
    ("每次操作前轮询 BUSY==0",
     "文档：direct 模式无法中止已在进行的内存映射传输"),
]
for i, (what, why) in enumerate(rules, 1):
    print("    %d. %-32s —— %s" % (i, what, why))
print()
print("  ★ 对本项目的影响：")
print("    - core0（音频）完全不受影响：它只读内存、写 PIO FIFO")
print("    - core1 在 Flash 操作期间**不能执行任何 flash 里的代码**")
print("      → 所以 UI 扫描中断、MIDI 中断都必须用 __not_in_flash_func")
print("    - 实践上：把 flash 读取封装成短临界区（每次 4KB，约 1.8ms），")
print("      期间只屏蔽 core1 自己的中断，core0 照常跑音频")
crit_ms = 4096 / tput_bytes * 1000
print("    4KB 突发耗时 = %.2f ms（相当于 %.1f 个音频缓冲周期）"
      % (crit_ms, crit_ms * SAMPLE_RATE / 512))
check(crit_ms < 5.0, "单次突发 < 5ms（core0 的 10.7ms 缓冲不会被拖垮）")

print()
print("=== 4. 与其它方案的对比（为什么最终选 direct）===")
print()
rows = [
    ("PIO QSPI", "2.5", "只能读", "要抢 QSPI 引脚", "寄存器自包含"),
    ("内存映射窗口", "25", "只能读", "无", "M1 配置无法验证"),
    ("QMI direct", "%.2f" % (tput_bytes / 1e6), "能读能写", "无",
     "需代码放 RAM"),
]
print("  %-14s %-10s %-10s %-16s %s" % ("方案", "吞吐MB/s", "写能力", "引脚冲突", "可验证性"))
for r in rows:
    print("  %-14s %-10s %-10s %-16s %s" % r)
print()
print("  → direct 模式唯一代价是'代码要放 RAM'，而这个我们已经要做")
print("    （音频引擎本来就要 __not_in_flash_func 避免 XIP 抖动）。")
print("    换来的是：能写（USB 刷音色库必需）+ 无引脚冲突 + 寄存器有文档。")

print()
if FAILS:
    print("%d 项未通过" % FAILS)
    sys.exit(1)
print("带宽与可行性核对通过 ✓")
