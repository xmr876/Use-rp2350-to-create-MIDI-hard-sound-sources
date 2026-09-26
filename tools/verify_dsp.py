"""
verify_dsp.py — 把 dsp_core.c 的数值逻辑转写成 Python 做验证

为什么需要这个脚本
------------------
开发这个固件时本机没有 C 编译器，无法编译验证。但热路径上的数值错误
（查表越界、包络斜率算成 0、音分并入的取整方向错）肉眼看不出来，
必须在烧板前抓出来。

做法：从**生成的头文件里读真实表数据**，再用 Python 逐行复刻 dsp_core.c
的算法，与数学精确值对比。

能抓住：
  - 查表索引算错（越界 / 偏移一位）
  - 表值本身算错（曾经手写力度表把末项写成 68199，越出 Q15 上限）
  - 包络斜率被算成 0（包络卡死）或时长失真（曾经 5000ms 走成 8192ms）
  - 音分并入的取整方向错

抓不住（必须靠编译器和实测）：
  - C 语法错误、类型截断、隐式提升溢出、宏展开、头文件包含顺序
  - PIO/DMA/时钟等硬件交互

运行:  python tools/verify_dsp.py
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware" / "src"

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print("  OK   " + msg)
    else:
        FAILS += 1
        print("  FAIL " + msg)


def load_array(header: Path, name: str) -> list[int]:
    txt = header.read_text(encoding="utf-8")
    m = re.search(re.escape(name) + r"\[[^\]]*\]\s*=\s*\{(.*?)\};", txt, re.S)
    if not m:
        raise SystemExit("在 %s 里找不到数组 %s" % (header.name, name))
    return [int(x) for x in re.findall(r"-?\d+", m.group(1))]


def load_defines(header: Path) -> dict[str, int]:
    txt = header.read_text(encoding="utf-8")
    out = {}
    for m in re.finditer(r"#define\s+(\w+)\s+\(?(-?\d+)\)?", txt):
        out[m.group(1)] = int(m.group(2))
    return out


# ==================================================================
# 加载真实表数据
# ==================================================================
PITCH = load_array(SRC / "dsp_pitch_table.h", "k_pitch_q16")
PDEF = load_defines(SRC / "dsp_pitch_table.h")
VEL = load_array(SRC / "dsp_tables.h", "k_vel_q15")
DB = load_array(SRC / "dsp_tables.h", "k_db_q15")

LO = PDEF["PITCH_TAB_LO_SEMITONE"]
SUB = PDEF["PITCH_TAB_SUB"]
PLEN = PDEF["PITCH_TAB_LEN"]

print("=== 表加载 ===")
print("  k_pitch_q16: %d 项 (header 声明 %d), LO=%d SUB=%d" % (len(PITCH), PLEN, LO, SUB))
print("  k_vel_q15:   %d 项" % len(VEL))
print("  k_db_q15:    %d 项" % len(DB))
check(len(PITCH) == PLEN, "音高表长度与 #define 一致")
check(len(VEL) == 128, "力度表 128 项（0..127 直接索引）")
check(len(DB) == 97, "dB 表 97 项（0..96）")


# ==================================================================
# dsp_pitch_step
# ==================================================================
def pitch_step_c(note: int, root_key: int, tune_cents: int) -> int:
    """严格按 dsp_core.c 的整数运算复刻"""
    semitone_q4 = (note - root_key) * SUB + tune_cents * SUB // 100

    # C 里手工实现了"向下取整"的整除；Python 的 // 就是向下取整
    si = semitone_q4 // SUB
    idx = semitone_q4 - si * SUB

    i0 = (si - LO) * SUB + idx
    if i0 < 0:
        i0 = 0
    if i0 > PLEN - 2:
        i0 = PLEN - 2

    a = PITCH[i0]
    b = PITCH[i0 + 1]
    frac = 0                       # 表细分已覆盖输入精度
    step = a + (((b - a) * frac) >> 16)
    if step == 0:
        step = 0x00010000
    return step


print()
print("=== dsp_pitch_step 精度 ===")
worst = 0.0
worst_case = None
tested = 0
for note in range(128):
    for root in (0, 24, 36, 48, 60, 72, 84, 96, 108, 127):
        for tune in (-100, -50, -25, 0, 25, 50, 100):
            semis = (note - root) + tune / 100.0
            if not (-60 <= semis <= 66.9):
                continue           # 表范围外会被夹紧，属设计行为
            step = pitch_step_c(note, root, tune)
            exact = 2.0 ** (semis / 12.0) * 65536.0
            rel = abs(step - exact) / exact
            cents = 1200.0 * math.log2(1.0 + rel) if rel < 1.0 else 1e9
            tested += 1
            if cents > worst:
                worst, worst_case = cents, (note, root, tune, step, exact)

print("  测试组合 %d 个，最大误差 %.4f 音分" % (tested, worst))
if worst_case:
    n, r, t, got, exp = worst_case
    print("  最差点 note=%d root=%d tune=%d: 得到 %.1f 期望 %.1f" % (n, r, t, got, exp))
check(worst < 1.0, "音高误差 < 1 音分（人耳可辨约 3~5）")
check(tested > 3000, "测试覆盖充分")
check(pitch_step_c(60, 60, 0) == 65536, "note==root 步进恰为 1.0 (65536)")
check(pitch_step_c(72, 60, 0) == 131072, "高八度恰为 2.0 (131072)")
check(pitch_step_c(48, 60, 0) == 32768, "低八度恰为 0.5 (32768)")
check(pitch_step_c(0, 127, -100) > 0, "极端低音不返回 0")
check(pitch_step_c(127, 0, 100) > 0, "极端高音不返回 0")


# ==================================================================
# 力度表
# ==================================================================
print()
print("=== 力度表 ===")
bad = sum(1 for v in range(127)
          if VEL[v] != int(round((v / 127.0) ** 2 * 32767)))
check(bad == 0, "0..126 全部符合 (vel/127)^2")
check(VEL[127] == 32767, "vel=127 → 满幅 32767")
check(VEL[0] == 0, "vel=0 → 0（MIDI 里等同 note-off）")
check(max(VEL) <= 32767, "最大项不超 Q15 上限（曾经错成 68199）")
check(all(VEL[i] <= VEL[i + 1] for i in range(127)), "单调不减")


# ==================================================================
# dB 表
# ==================================================================
print()
print("=== dB 表 ===")
bad = sum(1 for d in range(97)
          if DB[d] != max(1, min(32767, int(round((10.0 ** (-d / 20.0)) * 32767)))))
check(bad == 0, "97 项全部符合 10^(-db/20)")
check(DB[0] == 32767, "0dB → 满幅")
check(DB[96] >= 1, "96dB 衰减仍 >= 1（为 0 会永久静音）")
check(all(DB[i] >= DB[i + 1] for i in range(96)), "单调不增")


# ==================================================================
# 包络（Q15.16）
# ==================================================================
ENV_INTERVAL = 16
UPD_PER_MS = 48000 // ENV_INTERVAL // 1000
ENV_ONE = 32767 << 16

print()
print("=== 包络（Q15.16 定点）===")
print("  DSP_ENV_UPDATE_INTERVAL=%d → 每毫秒 %d 次更新" % (ENV_INTERVAL, UPD_PER_MS))
check(UPD_PER_MS == 3, "整数除法后仍是 3（若为 0 会使所有包络卡死）")


def env_slope(ms: int, span_q16: int, min_step: int) -> int:
    """复刻 env_slope_q16"""
    if ms == 0:
        ms = 1
    n = ms * UPD_PER_MS
    step = abs(span_q16) // max(1, n)
    return max(step, min_step)


def env_steps(am, dm, sus, rm):
    a = env_slope(am, ENV_ONE, 1)
    span = ENV_ONE - (sus << 16)
    d = -env_slope(dm, span, 1) if span > 0 else 0
    r = -env_slope(rm, ENV_ONE, 1)
    return a, d, r


def env_timing(am, dm, sus, rm):
    """精确模拟三段耗时（毫秒）"""
    a, d, r = env_steps(am, dm, sus, rm)
    out = {}
    lvl, n = 0, 0
    while lvl < ENV_ONE and n < 20_000_000:
        n += 1
        if n % ENV_INTERVAL == 0:
            lvl += a
    out["attack"] = n / ENV_INTERVAL / UPD_PER_MS

    lvl, n, tgt = ENV_ONE, 0, (sus << 16)
    while lvl > tgt and n < 20_000_000:
        n += 1
        if n % ENV_INTERVAL == 0:
            lvl += d
    out["decay"] = n / ENV_INTERVAL / UPD_PER_MS

    lvl, n = ENV_ONE, 0
    while lvl > 0 and n < 20_000_000:
        n += 1
        if n % ENV_INTERVAL == 0:
            lvl += r
    out["release"] = n / ENV_INTERVAL / UPD_PER_MS
    return out


TOL = 1.0     # 容差：1 个更新周期 0.33ms + 取整余量
cases = [(1, 1, 32767, 5), (10, 200, 16384, 100), (500, 1000, 0, 2000),
         (2000, 5000, 8192, 5000), (250, 800, 0, 1500)]
for (am, dm, sus, rm) in cases:
    a, d, r = env_steps(am, dm, sus, rm)
    check(a > 0, "attack=%dms 斜率非 0（不会卡死）" % am)
    check(r < 0, "release=%dms 斜率为负" % rm)
    if sus < 32767:
        check(d < 0, "decay=%dms 斜率为负" % dm)

    tm = env_timing(am, dm, sus, rm)
    print("      A%-5d D%-5d S%-6d R%-5d → 实际 A%8.2f D%8.2f R%8.2f"
          % (am, dm, sus, rm, tm["attack"], tm["decay"], tm["release"]))
    check(abs(tm["attack"] - am) <= TOL, "  attack 误差 <= %.1fms" % TOL)
    if sus < 32767 and dm > 1:
        check(abs(tm["decay"] - dm) <= TOL, "  decay 误差 <= %.1fms" % TOL)
    check(abs(tm["release"] - rm) <= TOL, "  release 误差 <= %.1fms" % TOL)

a, d, r = env_steps(0, 0, 32000, 0)
check(a != 0 and r != 0, "极短段（0ms）夹紧后斜率非 0")


# ==================================================================
print()
if FAILS:
    print("%d 项未通过" % FAILS)
    sys.exit(1)
print("全部通过 ✓")
print()
print("本脚本只验证数值逻辑。以下必须靠编译器和实测：")
print("  - C 语法错误、漏分号、类型不匹配")
print("  - int / int16_t 截断，隐式提升导致的溢出")
print("  - 宏展开、头文件包含顺序")
print("  - PIO / DMA / 时钟 / QMI 等硬件交互")
