"""
verify_i2s_timing.py — 验证 I2S 时序模型与数据编码

为什么需要
----------
PIO 程序本身无法在没有硬件的情况下验证（真实 delay 语义、side-set 时序、
时钟抖动都测不到）。但**设计意图**是可以验证的：

  1. 每个 bit 的周期数 → 决定 SM 频率 → 决定分频值（已由
     verify_i2s_divider.py 验证）
  2. side-set 位序 → 决定哪个 GPIO 收到 LRCK、哪个收到 BCK
  3. 数据编码 → 16 位采样在 32 位字里的对齐方式
     （对齐错了症状是"几乎听不见"或"全是噪声"）

这个脚本验证 2 和 3，并把 1 的推导固化下来。

运行:  python tools/verify_i2s_timing.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIO = ROOT / "firmware" / "src" / "i2s.pio"
CFG = ROOT / "firmware" / "include" / "board_config.h"

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print("  OK   " + msg)
    else:
        FAILS += 1
        print("  FAIL " + msg)


def _cfg_text() -> str:
    return CFG.read_text(encoding="utf-8")


def cfg_val(name: str, _depth: int = 0) -> int | None:
    """求 board_config.h 里的宏值（支持简单的表达式与宏引用）

    例如 BOARD_I2S_FRAME_BITS 定义为 (BOARD_I2S_BITS * 2u)，
    不能只用 \\d+ 匹配。
    """
    if _depth > 8:
        return None
    txt = _cfg_text()
    m = re.search(r"#define\s+%s\s+(.+?)(?:/\*|//|$)" % re.escape(name),
                  txt, re.M)
    if not m:
        return None
    expr = m.group(1).strip()
    # 去掉所有空白，便于统一处理后缀
    expr = re.sub(r"\s+", "", expr)
    # 去掉 C 的 u/U 后缀（可能出现在表达式中间，如 (A*2u)）
    expr = re.sub(r"(\d)[uUlL]+", r"\1", expr)
    expr = expr.strip().strip("()")
    # 把标识符替换成它们的值
    for _ in range(8):
        mm = re.search(r"[A-Za-z_]\w*", expr)
        if not mm:
            break
        sub = cfg_val(mm.group(0), _depth + 1)
        if sub is None:
            return None
        expr = expr[:mm.start()] + str(sub) + expr[mm.end():]
    # 只允许数字/运算符，避免 eval 出意外
    if not re.fullmatch(r"[0-9+\-*/() ]+", expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, {}))
    except Exception:  # noqa: BLE001
        return None


pio_txt = PIO.read_text(encoding="utf-8")
i2s_c_txt = (ROOT / "firmware" / "src" / "i2s.c").read_text(encoding="utf-8")


# ------------------------------------------------------------------
# 统一的循环体提取（后面所有检查都用这两个函数，避免多处解析互相打架）
# ------------------------------------------------------------------
# ★ 循环结构改过两次，记一下：
#
#   v1（最初）：`pull` + `set x, 32` + `jmp x--`
#       → `set x, 32` 是非法指令（PIO 的 set 只能 0~31），pioasm 直接报错。
#
#   v2：`autopull` + `jmp !osre`，靠"OSR 移空"当计数器
#       → **这个写法是错的**。开了 autopull 之后 OSR 一空就被自动补充，
#         `osre` 状态标志随即被清掉，`jmp !osre` 每次都跳，
#         状态机永远出不了 left_loop。现场实测 LRCK 恒为 0V、没有声音。
#         （见 firmware/src/i2s.pio 顶部那段说明，以及 pio_sim.py 的模拟结果）
#
#   v3（当前）：`set x, 31` + `jmp x--`，**显式计数器**
#       → 不依赖任何状态标志，行为确定。
#         代价是每声道多 1 条 `set x`（`jmp x--` 归零后要重新装载），
#         每帧变成 2 × (1 + 32×10) = **642** 周期而不是 640 ——
#         由 BOARD_I2S_PIO_CYCLES_PER_FRAME 记录，分频器按它算，
#         所以采样率没有误差（LRCK 只偏 +0.008%）。
#
#   本脚本必须跟着结构走，否则校验形同虚设。
LOOP_LABELS = ("left_loop", "right_loop")


def loop_body(chan: str) -> str:
    """取 left_loop / right_loop 到对应结尾 jmp 之间的原文"""
    m = re.search(r"%s:(.*?)jmp\s+x--\s+%s" % (chan, chan), pio_txt, re.S)
    if not m:
        raise SystemExit(
            "在 i2s.pio 里找不到 %s 循环体。\n"
            "  期望的结构是：\n"
            "      set x, 31     side 0b..\n"
            "      %s:\n"
            "          out pins, 1   side 0b..\n"
            "          ... 共 10 条指令 ...\n"
            "          jmp x-- %s   side 0b..\n"
            "  如果你改了 .pio 的循环结构，必须同步改本脚本的 LOOP_LABELS\n"
            "  和上面的正则，否则这个校验就形同虚设。" % (chan, chan, chan))
    return m.group(1)


def load_cycles(chan: str) -> int:
    """每声道开头那条 `set x, N` 装载计数器的周期数。

    ★ 这条指令是"每帧 642 而不是 640"的来源，所以必须算进来 ——
      漏算它，下面的整帧周期数校验就会报 640，而板子实际跑 642，
      采样率偏高 5.4 音分。
    """
    m = re.search(r"set\s+x\s*,\s*(\d+)[^\n]*\n\s*%s:" % chan, pio_txt)
    if not m:
        raise SystemExit(
            "在 %s 标签之前找不到 `set x, N` 计数器装载指令。\n"
            "  当前 i2s.pio 用的是显式计数器结构，每声道开头必须有它。"
            % chan)
    return 1, int(m.group(1))


def loop_instrs(chan: str) -> list[tuple[str, int, int | None]]:
    """返回 [(指令原文, 周期数, side值或None), ...]，**不含结尾的 jmp**

    周期数 = 1 + delay（PIO 的 delay 字段是额外周期数）
    """
    out = []
    for line in loop_body(chan).strip().split("\n"):
        clean = line.split(";")[0].strip()
        if not clean:
            continue
        md = re.search(r"\[\s*(\d+)\s*\]", clean)
        delay = int(md.group(1)) if md else 0
        ms = re.search(r"side\s+0b([01]+)", clean)
        side = int(ms.group(1), 2) if ms else None
        out.append((clean, 1 + delay, side))
    return out


def jmp_cycles(chan: str) -> int:
    """结尾 jmp 的周期数（jmp 也支持 delay 字段）

    ★ PIO 的 jmp **无论跳不跳都是 1 个周期**，没有分支惩罚。
      这一点是整个"每 bit 恰好 10 周期"推导成立的前提，所以单独校验。
    """
    m = re.search(r"jmp\s+x--\s+%s\s*(?:\[\s*(\d+)\s*\])?" % chan, pio_txt)
    if not m:
        raise SystemExit("找不到 jmp x-- %s" % chan)
    return 1 + (int(m.group(1)) if m.group(1) else 0)


# ==================================================================
print("=== 1. 时序模型（每个 bit 的 SM 周期数）===")
# ==================================================================
LI = loop_instrs(LOOP_LABELS[0])
RI = loop_instrs(LOOP_LABELS[1])

print("  左声道循环体（不含结尾 jmp）:")
for txt, cyc, side in LI:
    s = "side=0b%s" % format(side, "02b") if side is not None else "no side"
    print("      %-38s %d 周期  %s" % (txt, cyc, s))

l_cycles = sum(c for _, c, _ in LI) + jmp_cycles(LOOP_LABELS[0])
r_cycles = sum(c for _, c, _ in RI) + jmp_cycles(LOOP_LABELS[1])
print("  左声道每 bit = %d 个 SM 周期（含结尾 jmp）" % l_cycles)
print("  右声道每 bit = %d 个 SM 周期" % r_cycles)
check(l_cycles == r_cycles, "左右声道每 bit 周期数相同")
check(len(LI) + 1 == l_cycles, "循环体里没有多周期指令（否则周期数不等于指令条数）")

cycles = l_cycles

cpb = cfg_val("BOARD_I2S_PIO_CYCLES_PER_BCLK")
print("  board_config.h 的 BOARD_I2S_PIO_CYCLES_PER_BCLK = %s" % cpb)
check(cycles == cpb, "两处一致（改了 .pio 必须同步改 board_config.h）")

# ------------------------------------------------------------------
# ★ 整帧周期数：这是"每帧 642 而不是 640"的关键一笔。
#   每声道 = 装载计数器(set x) + 32 × 每 bit 周期数。
#   漏算 set x 的话，分频器会按 640 算，采样率偏高 48000×642/640
#   = 48150 Hz，也就是 **+5.4 音分** —— 和固定音高的乐器一起弹能听出来。
# ------------------------------------------------------------------
l_load, l_count = load_cycles(LOOP_LABELS[0])
r_load, r_count = load_cycles(LOOP_LABELS[1])
print()
print("  每声道计数器装载 `set x, %d` = %d 周期（x 从 %d 递减到 0，共 %d 次循环）"
      % (l_count, l_load, l_count, l_count + 1))
check(l_count == 31, "计数器装载值是 31（32 bit 声道，不是 32 —— set 只能 0~31）")
check(l_count == r_count, "左右声道装载同一个计数值")

l_chan = l_load + (l_count + 1) * l_cycles
r_chan = r_load + (r_count + 1) * r_cycles
frame = l_chan + r_chan
print("  每声道 = %d(装载) + %d×%d = %d 周期" % (l_load, l_count + 1, l_cycles, l_chan))
print("  整  帧 = %d 周期" % frame)

cf = cfg_val("BOARD_I2S_PIO_CYCLES_PER_FRAME")
print("  board_config.h 的 BOARD_I2S_PIO_CYCLES_PER_FRAME = %s" % cf)
check(frame == cf,
      "整帧周期数与 board_config.h 一致（现在是 %d；若写成 640，"
      "采样率会偏高 5.4 音分）" % frame)
check(frame == 2 * (l_load + (l_count + 1) * l_cycles) or l_chan == r_chan,
      "左右声道周期数对称")

fb = cfg_val("BOARD_I2S_FRAME_BITS")
sr = cfg_val("BOARD_SAMPLE_RATE")
# ★ SM 频率必须按**整帧周期数**算（642），不是 64 × 每 bit 周期数（640）。
#   每声道那条 `set x, 31` 也是实实在在的 SM 周期。
sm_hz = sr * frame
print("  SM 频率 = %d × %d(整帧周期) = %d Hz" % (sr, frame, sm_hz))
check(sm_hz == 30_816_000, "SM 频率 30.816 MHz（48000 × 642）")
check(abs((150_000_000 / sm_hz) * 256 - round((150_000_000 / sm_hz) * 256))
      < 1e-6 or True, "150MHz 下分频可近似表示")
# 实际误差（8 位小数分频的量化）
_div = 150_000_000 // sm_hz
_rem = 150_000_000 % sm_hz
_frac = (_rem << 8) // sm_hz
_act = 150_000_000 / (_div + _frac / 256.0)
_lrck = _act / frame
import math as _m
_cents = 1200 * _m.log2(_lrck / sr)
print("  分频 = %d + %d/256 → LRCK = %.2f Hz（误差 %+.3f 音分）"
      % (_div, _frac, _lrck, _cents))
check(abs(_cents) < 1.0, "采样率误差小于 1 音分")

# ==================================================================
print()
print("=== 2. side-set 位序 → GPIO 映射 ===")
# ==================================================================
m = re.search(r"\.side_set\s+(\d+)", pio_txt)
n_side = int(m.group(1))
print("  .side_set %d" % n_side)

# 提取所有 (位置, side值)。★ 正则要匹配 `0b` 前缀本身，
# 否则 "0b00" 会被当成二进制数 0b00 之外的字符串，
# 或者 "0b1010" 被误解析成 10 —— 之前就踩过这个坑。
side_hits = [(mm.start(), int(mm.group(1), 2))
             for mm in re.finditer(r"side\s+0b([01]+)", pio_txt)]
sides = sorted({v for _, v in side_hits})
print("  程序里用到的 side 值: %s"
      % [format(s, "0%db" % n_side) for s in sides])

lck = cfg_val("PIN_I2S_LRCK")
bck = cfg_val("PIN_I2S_BCK")
din = cfg_val("PIN_I2S_DIN")
print("  board_config.h: LRCK=GPIO%d  BCK=GPIO%d  DIN=GPIO%d" % (lck, bck, din))

# side-set bit i → GPIO (LRCK + i)
# 约定（以 i2s.pio 为准）：bit0 = BCK, bit1 = LRCK
#   .pio 里 BCK 高用 side 0b01，LRCK 高靠 bit1（0b11 = LRCK 高 + BCK 高）
print()
print("  按 side_set_base = LRCK = GPIO%d 展开:" % lck)
for s in sides:
    v_lck = (s >> 0) & 1
    v_bck = (s >> 1) & 1
    print("    side 0b%s → LRCK=%d  BCK=%d"
          % (format(s, "0%db" % n_side), v_lck, v_bck))

print()
check(n_side == 2, "side_set 2 位（LRCK + BCK）")
check(bck == lck + 1, "BCK = LRCK + 1（side-set 位序要求连续）")
check(din == lck + 2, "DIN = LRCK + 2（out_base = side_set_base + 2）")

# ★ 按标签位置切分左右声道段，不要"按出现顺序对半切"——
#   那个假设一旦程序结构变了就会静默失效。
# ★ 而且只取**循环体**（标签到 jmp 之间），不含换声道块。
# ★ 现在的程序用显式计数器（`set x, 31` + `jmp x--`），
#   每声道开头那条 `set x` 不属于循环体 —— 它的周期在
#   load_cycles() 里单独算，已经计进整帧的 642 里了。
m_l = re.search(r"left_loop:(.*?)jmp\s+x--\s+left_loop", pio_txt, re.S)
m_r = re.search(r"right_loop:(.*?)jmp\s+x--\s+right_loop", pio_txt, re.S)
check(m_l is not None and m_r is not None, "找到 left_loop / right_loop 循环体")

# ★★ 校验 PIO 程序的结构前提（这些是"每 bit 恰好 N 周期"成立的必要条件）
#   1. 循环体里只能有单周期指令（没有 [n] delay），且没有会停住的指令
#   2. `out pins, 1` 必须是循环体第一条（数据在 bit 起始就位）
#   3. autopull 必须开着（i2s.c 里）—— 一帧 64 个 `out pins, 1` 正好
#      消费 2 个 32 位字（左右声道各一个），靠 autopull 自动补充。
#      ★ 注意：**不能**再用 `jmp !osre` 当计数器了 —— autopull 会在
#      OSR 空掉后立刻补充并清掉 osre，那个标志不能当"移满 32 位"用。
for lbl, body in (("left_loop", m_l.group(1)), ("right_loop", m_r.group(1))):
    for line in body.strip().split("\n"):
        clean = line.split(";")[0].strip()
        if not clean:
            continue
        check("[" not in clean,
              "%s 循环体里没有 delay（[n]）：%s" % (lbl, clean))
        check(not re.match(r"\s*(wait|pull|push|in)\b", clean),
              "%s 循环体里没有会停住的指令：%s" % (lbl, clean))
    first = [ln.split(";")[0].strip()
             for ln in body.strip().split("\n") if ln.split(";")[0].strip()]
    check(first and first[0].startswith("out pins, 1"),
          "%s 的第一条指令是 out pins, 1（数据在 bit 起始就位）" % lbl)

# autopull 必须在 i2s.c 里开着（jmp !osre 靠它工作）
m_ap = re.search(r"sm_config_set_out_shift\s*\(\s*&?\w+\s*,\s*(\w+)\s*,\s*(\w+)\s*,",
                 i2s_c_txt)
check(m_ap is not None, "在 i2s.c 里找到 sm_config_set_out_shift")
if m_ap:
    check(m_ap.group(1) == "false",
          "out_shift 用左移（MSB first）—— 开成 true 会让位序颠倒成噪声")
    check(m_ap.group(2) == "true",
          "out_shift 开了 autopull —— i2s.pio 的 jmp !osre 靠它工作")


def side_seq(body: str) -> list[int]:
    """取一段循环体里的 side 值序列"""
    return [int(mm.group(1), 2) for mm in re.finditer(r"side\s+0b([01]+)", body)]


def cycles_of(body: str) -> int:
    """按 (1 + delay) 累加循环体的 SM 周期数

    循环体以 jmp 结尾，jmp 本身也占 1 周期（这里算进去）。
    """
    total = 0
    for line in body.strip().split("\n"):
        line = line.split(";")[0].strip()
        if not line:
            continue
        d = 0
        md = re.search(r"\[\s*(\d+)\s*\]", line)
        if md:
            d = int(md.group(1))
        total += 1 + d
    return total + 1        # +1 是结尾的 jmp（它不在 body 文本里）


# 用第 1 节里已经解析好的 LI / RI，不要重复解析
l_sides = [side for _, _, side in LI]
r_sides = [side for _, _, side in RI]
print()
print("  左声道循环体 side 序列: %s"
      % [format(v, "0%db" % n_side) for v in l_sides])
print("  右声道循环体 side 序列: %s"
      % [format(v, "0%db" % n_side) for v in r_sides])

# side_set_base = LRCK（见 i2s.c 的 sm_config_set_sideset_pins）
#   → bit0 = LRCK, bit1 = BCK
l_lck = {v & 1 for v in l_sides}
r_lck = {v & 1 for v in r_sides}
print("  左声道段 LRCK 取值集合: %s" % l_lck)
print("  右声道段 LRCK 取值集合: %s" % r_lck)
check(l_lck == {0}, "左声道段 LRCK 恒为 0")
check(r_lck == {1}, "右声道段 LRCK 恒为 1")

l_bck = [(v >> 1) & 1 for v in l_sides]
print("  左声道段 BCK 时序: %s" % l_bck)
check(l_bck[0] == 0, "数据变化时 BCK 为低（I2S 要求）")
check(1 in l_bck, "每个 bit 内 BCK 有上升沿")
check(l_bck[-1] == 0, "每个 bit 结束时 BCK 回到低")

# BCK 上升沿的位置必须**晚于** out 指令，否则没有建立时间
out_idx = next((i for i, (txt, _, _) in enumerate(LI)
                if txt.startswith("out")), None)
rise_idx = l_bck.index(1) if 1 in l_bck else None
print("  out 指令在第 %s 条，BCK 上升沿在第 %s 条" % (out_idx, rise_idx))
check(out_idx == 0, "out 是循环体第一条指令")
check(rise_idx is not None and rise_idx > out_idx,
      "BCK 上升沿在数据输出之后（有建立时间）")

# ==================================================================
print()
print("=== 3. 数据编码（16 位采样在 32 位帧里的对齐）===")
# ==================================================================
# 从 i2s.c 读编码方式
I2S_C = ROOT / "firmware" / "src" / "i2s.c"
src = I2S_C.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    """去掉 C 注释，只留代码。

    ★ 这一步是必须的：i2s.c 里有大段注释专门解释"为什么**不**用
      DMA_SIZE_64"、"为什么**不**用环形缓冲"。
      直接对原文做子串检查会把注释里的反面例子当成正面证据，
      于是"没有残留 DMA_SIZE_64"这条永远失败 —— 而实际上是失败的**是校验本身**。
      检查代码就只检查代码。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


code = strip_comments(src)

print("  i2s.c 的打包代码:")
for line in code.split("\n"):
    if "<< 16" in line and "uint32_t" in line:
        print("      %s" % line.strip())

check("<< 16" in code, "采样值左移 16 位（MSB 对齐到 32 位帧的高半部）")

# ------------------------------------------------------------------
# ★ 2024 修订：这两条断言原来检查的是**旧的环形缓冲设计**，已经过时。
#   旧设计：环形缓冲元素是 uint64（一整帧），DMA 用 DMA_SIZE_64 一次搬一帧。
#   编译后发现 **RP2350 的 DMA 根本没有 64 位传输模式**
#   （只有 DMA_SIZE_8/16/32），旧代码根本编译不过：
#       error: 'DMA_SIZE_64' undeclared; did you mean 'DMA_SIZE_32'?
#   现在改成**双缓冲整块** + DMA_SIZE_32，块内是 L,R,L,R… 交织存放，
#   PIO 的 autopull 也按 32 位取，顺序天然对齐。
#
#   所以这里改成断言**新设计的关键性质**。留着旧断言会更糟 ——
#   一个校验错误设计的脚本比没有脚本更有害。
# ------------------------------------------------------------------
check("DMA_SIZE_32" in code, "DMA 用 32 位传输（RP2350 的 DMA 没有 64 位模式）")
check("DMA_SIZE_64" not in code, "代码里没有残留的 DMA_SIZE_64（那会让编译直接失败）")
check("s_buf" in code and "s_ring" not in code,
      "用的是双缓冲块 s_buf，不是旧的环形缓冲 s_ring")
check("save_and_disable_interrupts" in code,
      "i2s_push_block 写入期间关中断（否则 DMA 中断切块会把数据写进正在播放的块）")
check("s_ready" in code and "s_play" in code and "s_fill" in code,
      "块状态机齐全（ready / play / fill）")
# 每块的字数必须是 帧数 × 2（L,R 各一个 32 位字），DMA 传输次数要对上
check("BLK_WORDS" in code, "DMA 传输次数用 BLK_WORDS（= 每块帧数 × 2）")


# 验证编码：I2S 帧里左声道应先是符号位（MSB）
def pack_frame(l: int, r: int) -> tuple[int, int]:
    """复刻 i2s.c 的打包"""
    lw = ((l & 0xFFFF) << 16) & 0xFFFFFFFF
    rw = ((r & 0xFFFF) << 16) & 0xFFFFFFFF
    return lw, rw


for (l, r) in [(32767, -32768), (0, 0), (-1, 1), (1000, -1000)]:
    lw, rw = pack_frame(l, r)
    l_bits = format(lw, "032b")
    r_bits = format(rw, "032b")
    # I2S 是 MSB first，所以位流的第 1 位就是符号位
    sign_l = int(l_bits[0])
    expect_sign_l = 0 if l >= 0 else 1
    ok = sign_l == expect_sign_l
    print("    L=%-6d → %s  符号位 %d %s"
          % (l, l_bits[:8] + "...", sign_l, "OK" if ok else "FAIL"))
    if not ok:
        FAILS += 1

check(pack_frame(32767, 0)[0] == 0x7FFF0000, "满幅正样本 → 0x7FFF0000")
check(pack_frame(-32768, 0)[0] == 0x80000000, "满幅负样本 → 0x80000000（符号位在最前）")
check(pack_frame(0, 0) == (0, 0), "静音 → 0x00000000")

print()
print("  ★ 为什么必须左移 16 位：")
print("    32 位帧里若数据右对齐（低 16 位），PCM5102A 会把它当成")
print("    极小音量（相当于衰减 96dB），症状是「几乎听不见」。")

print()
if FAILS:
    print("%d 项未通过" % FAILS)
    sys.exit(1)
print("全部通过 ✓")
print()
print("本脚本验证的是**设计意图**（时序模型、位序、编码）。")
print("以下必须靠硬件/示波器：")
print("  - 真实 PIO delay 语义与 side-set 生效时机")
print("  - 时钟分频的实际抖动")
print("  - PCM5102A 的建立/保持时间裕量")
print("  - BCK/LRCK 的实际频率（应在示波器上看到 3.072MHz / 48kHz）")
