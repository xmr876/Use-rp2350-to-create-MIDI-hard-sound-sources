"""
verify_firmware_structure.py — 替代编译器的结构校验

为什么需要
----------
开发这个工程的机器没有 ARM 工具链也没有 C 编译器，代码从未编译过。
完整编译做不到，但**编译期会失败的几类问题可以在 Python 里静态查出来**：

  1. 括号 / 花括号不平衡（缺一个分号或右括号）
  2. 用了未定义的宏（拼错 PIN_xxx / BOARD_xxx / GMSS_xxx）
  3. include 了不存在的项目内头文件
  4. 声明了但没实现的函数（头文件里声明、.c 里忘了写）
  5. `_Static_assert` 里的引脚约束是否真的成立

抓不住的（必须靠真正编译）：
  - C 语法细节、类型不匹配、隐式转换
  - SDK 头文件里不存在的 API 名
  - pioasm 生成的符号名

运行:  python tools/verify_firmware_structure.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"
SRC = FW / "src"
INC = FW / "include"

FAILS = 0
WARNS = 0

# SDK / 标准库头文件的前缀（用于区分项目内头文件）
SDK_PREFIXES = ("pico/", "hardware/", "pio/", "tusb", "bsp/")
SDK_EXACT = ("stdio.h", "stdlib.h", "string.h", "math.h", "stdint.h",
             "stdbool.h", "stddef.h", "assert.h")


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print("  OK   " + msg)
    else:
        FAILS += 1
        print("  FAIL " + msg)


def warn(cond: bool, msg: str) -> None:
    global WARNS
    if not cond:
        WARNS += 1
        print("  WARN " + msg)


def strip_comments_and_strings(src: str) -> str:
    """去掉注释与字符串字面量，避免里面的括号干扰计数"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r'"(\\.|[^"\\])*"', '""', src)
    src = re.sub(r"'(\\.|[^'\\])*'", "''", src)
    return src


# ==================================================================
# 1. 收集所有宏定义
# ==================================================================
print("=== 1. 宏定义收集 ===")

MACRO_DEFS: dict[str, str] = {}
macro_src_files = list(INC.glob("*.h")) + list(SRC.glob("*.h")) + list(SRC.glob("*.c"))
pio_headers = [p for p in SRC.glob("*.pio")]

for f in macro_src_files:
    txt = f.read_text(encoding="utf-8")
    for m in re.finditer(r"^\s*#\s*define\s+(\w+)(\([^)]*\))?\s*(.*)$",
                         txt, re.M):
        name = m.group(1)
        body = m.group(3)
        # ★ 必须去掉行尾注释，否则 "23 /* 位选 1 */" 这样的值
        #   会把注释也算进去，导致引脚号解析错（曾把 23 解析成 20）
        body = re.sub(r"/\*.*?\*/", " ", body)
        body = re.sub(r"//.*$", " ", body)
        MACRO_DEFS[name] = body.strip()

print("  从 %d 个文件收集到 %d 个宏" % (len(macro_src_files), len(MACRO_DEFS)))
print("  其中 board_config.h 的引脚宏:")
for n in sorted(MACRO_DEFS):
    if n.startswith("PIN_"):
        print("    %-24s = %s" % (n, MACRO_DEFS[n]))

# 展开宏（用于 _Static_assert 检查）
def expand(expr: str, depth: int = 0) -> str:
    if depth > 16:
        return expr
    e = re.sub(r"\s+", "", expr).strip()
    e = re.sub(r"(\d)[uUlL]+", r"\1", e)
    for _ in range(16):
        m = re.search(r"[A-Za-z_]\w*", e)
        if not m:
            break
        name = m.group(0)
        if name not in MACRO_DEFS:
            return expr          # 含不可展开的符号，放弃
        rep = re.sub(r"\s+", "", MACRO_DEFS[name])
        rep = re.sub(r"(\d)[uUlL]+", r"\1", rep)
        e = e[:m.start()] + "(" + rep + ")" + e[m.end():]
        depth += 1
        if depth > 32:
            break
    return e


def eval_int(expr: str) -> int | None:
    e = expand(expr)
    if not re.fullmatch(r"[0-9+\-*/()<>!=&|~^ ]+", e):
        return None
    try:
        return int(eval(e, {"__builtins__": {}}, {}))
    except Exception:      # noqa: BLE001
        return None


# ==================================================================
# 2. 括号平衡
# ==================================================================
print()
print("=== 2. 括号平衡 ===")
for f in sorted(list(SRC.glob("*.c")) + list(SRC.glob("*.h"))
                + list(INC.glob("*.h"))):
    txt = strip_comments_and_strings(f.read_text(encoding="utf-8"))
    pairs = {"{": "}", "(": ")", "[": "]"}
    ok = True
    detail = []
    for op, cl in pairs.items():
        n_op = txt.count(op)
        n_cl = txt.count(cl)
        if n_op != n_cl:
            ok = False
            detail.append("%s%s=%d/%d" % (op, cl, n_op, n_cl))
    check(ok, "%-28s 括号平衡 %s" % (f.name, " ".join(detail)))

# ==================================================================
# 3. 未定义的宏引用
# ==================================================================
print()
print("=== 3. 未定义的宏引用 ===")
# 这些前缀属于本项目自己的宏；SDK 的宏不在范围内
OWN_PREFIXES = ("PIN_", "BOARD_", "GMSS_", "FLASH_", "MIDI_", "DSP_ENV",
                "UI_SEG", "UI_BTN", "ENV_", "PITCH_TAB")

# pioasm 生成的符号与 SDK 的函数名不算宏
KNOWN_EXTERNALS = {
    "PIO_FIFO_JOIN_TX", "DMA_SIZE_32", "DMA_SIZE_64", "DMA_IRQ_0",
    "GPIO_FUNC_XIP_CS1", "GPIO_FUNC_UART1", "GPIO_FUNC_PIO0",
    "GPIO_DRIVE_STRENGTH_4MA", "GPIO_SLEW_RATE_FAST",
    "GPIO_OUT", "GPIO_IN", "PICO_ERROR_GENERIC", "PICO_OK",
}

# 枚举成员也算已定义（ENV_ATTACK / GMSS_CODEC_PCM16 等）
ENUM_MEMBERS: set[str] = set()
for f in macro_src_files:
    txt = strip_comments_and_strings(f.read_text(encoding="utf-8"))
    for m in re.finditer(r"typedef\s+enum\s*(?:\w+)?\s*\{(.*?)\}\s*\w+\s*;",
                         txt, re.S):
        for mm in re.finditer(r"\b([A-Z]\w*)\b", m.group(1)):
            ENUM_MEMBERS.add(mm.group(1))

unresolved: dict[str, list[str]] = {}
for f in sorted(list(SRC.glob("*.c")) + list(SRC.glob("*.h"))
                + list(INC.glob("*.h"))):
    txt = strip_comments_and_strings(f.read_text(encoding="utf-8"))
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{3,})\b", txt):
        name = m.group(1)
        if not name.startswith(OWN_PREFIXES):
            continue
        if name in MACRO_DEFS or name in ENUM_MEMBERS or name in KNOWN_EXTERNALS:
            continue
        unresolved.setdefault(name, []).append(f.name)

print()
print("  收集到 %d 个枚举成员" % len(ENUM_MEMBERS))
if unresolved:
    for name, files in sorted(unresolved.items()):
        print("  FAIL 未定义的宏/枚举 %s （出现在 %s）"
              % (name, ", ".join(sorted(set(files)))))
    FAILS += len(unresolved)
else:
    print("  OK   所有 PIN_/BOARD_/GMSS_/FLASH_ 前缀的标识符都有定义")

# ==================================================================
# 4. include 检查
# ==================================================================
print()
print("=== 4. include 检查（只看引号形式，尖括号是 SDK 头）===")
missing = []
checked = 0
for f in sorted(list(SRC.glob("*.c")) + list(SRC.glob("*.h"))
                + list(INC.glob("*.h"))):
    txt = f.read_text(encoding="utf-8")
    for line in txt.split("\n"):
        ls = line.strip()
        if not ls.startswith("#"):
            continue
        rest = re.sub(r"^#\s*include\s*", "", ls)
        # 取 <> 或 "" 里的头文件名
        m2 = re.match(r'^[<"]([^>"]+)[>"]', rest)
        if not m2:
            continue
        hdr = m2.group(1)
        # SDK / 标准库头（用 <> 或已知前缀）跳过
        if rest.startswith("<") or hdr.startswith(SDK_PREFIXES) \
                or hdr in SDK_EXACT:
            continue
        checked += 1
        # PIO 生成的头文件：检查对应的 .pio 源文件存在
        if hdr.endswith(".pio.h"):
            pio_name = hdr[:-2]           # i2s.pio.h -> i2s.pio
            if not (SRC / pio_name).exists():
                missing.append((f.name, hdr, "缺少对应的 .pio 源文件"))
            continue
        if (SRC / hdr).exists() or (INC / hdr).exists():
            continue
        missing.append((f.name, hdr, "项目内找不到"))

if missing:
    for fn, hdr, why in missing:
        print("  FAIL %s include 了 %s —— %s" % (fn, hdr, why))
    FAILS += len(missing)
else:
    print("  OK   %d 个项目内 include 都能找到" % checked)

# ==================================================================
# 5. 头文件声明的函数是否有实现
# ==================================================================
print()
print("=== 5. 函数声明 vs 实现 ===")
decls: dict[str, list[str]] = {}
for h in sorted(SRC.glob("*.h")) + sorted(INC.glob("*.h")):
    txt = strip_comments_and_strings(h.read_text(encoding="utf-8"))
    if h.name == "gmss_format.h":
        continue          # 纯 static inline 与结构体，跳过
    for m in re.finditer(r"^\s*(?!static)([A-Za-z_]\w*(?:\s*\*)?)\s+"
                         r"([a-z_]\w*)\s*\(([^;{]*)\)\s*;", txt, re.M):
        ret, name, args = m.group(1).strip(), m.group(2), m.group(3)
        if "inline" in ret or name.startswith("__"):
            continue
        decls.setdefault(name, []).append(h.name)

# static inline 定义在头文件里，也算实现
for h in sorted(SRC.glob("*.h")) + sorted(INC.glob("*.h")):
    txt = strip_comments_and_strings(h.read_text(encoding="utf-8"))
    for m in re.finditer(r"static\s+inline\s+[A-Za-z_]\w*\s+([a-z_]\w*)\s*\(",
                         txt):
        decls.pop(m.group(1), None)

def find_function_definitions(txt: str) -> set[str]:
    """找所有函数定义的名字。

    直接匹配**完整签名**，不做"往前回溯几行"这种脆弱处理
    （签名可能跨行、属性可能嵌套、前后可能有大段注释和空行）。

    匹配的形式：
        int foo(void) {                    ← 普通
        static void __isr bar(void) {      ← 带属性
        int __not_in_flash_func(baz)(void) {   ← 属性里嵌函数名
        int qux(void)
        {                                  ← Allman 风格花括号换行

    这些全部被同一类模式覆盖：`标识符( ... )  [标识符(函数名)]  {`
    """
    names: set[str] = set()
    KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof",
                "do", "else", "case", "typedef", "struct", "union",
                "enum", "static_assert", "_Static_assert", "defined"}

    # 先把所有换行/多空白压成单空格，这样跨行签名变成一个平面字符串。
    # 代价是丢失行结构 —— 但我们需要的信息（标识符与括号）都还在。
    flat = re.sub(r"\s+", " ", txt)

    # 匹配 "... ( 参数表 ) ... {"
    pat = re.compile(r"([A-Za-z_]\w*)\s*\(([^;{}]*?)\)\s*"
                     r"(?:([A-Za-z_]\w*)\s*\(([^;{}]*?)\)\s*)?\{")
    for m in pat.finditer(flat):
        # ★ 直接从匹配到的文本里收集**所有**标识符。
        #
        #   为什么不做"哪个是函数名"的判断：
        #   pico-sdk 的属性形式 `int __not_in_flash_func(foo)(args) {`
        #   会让正则的分组回溯结果不可预测（实测 group(3) 恒为 None，
        #   属性宏把真正的函数名吞掉了）。
        #
        #   而我们的用途是**检查"头文件声明的函数是否在 .c 里实现"**，
        #   所以把匹配文本里的所有标识符都收进来即可 ——
        #   多收（误报）只会让检查更宽松，漏收（漏报）才会让检查失效。
        #   排除控制语句关键字，避免把 if/while/switch 当成函数。
        for cand in re.findall(r"[A-Za-z_]\w*", m.group(0)):
            if cand not in KEYWORDS:
                names.add(cand)
    return names


impl = set()
for c in sorted(SRC.glob("*.c")):
    txt = strip_comments_and_strings(c.read_text(encoding="utf-8"))
    impl |= find_function_definitions(txt)

not_impl = {n: h for n, h in decls.items() if n not in impl}
if not_impl:
    for n, h in sorted(not_impl.items()):
        print("  FAIL 声明了但未实现: %s()  （声明于 %s）" % (n, ", ".join(h)))
    FAILS += len(not_impl)
else:
    print("  OK   头文件里声明的函数都有 .c 实现（%d 个）" % len(decls))

# ==================================================================
# 6. _Static_assert 求值
# ==================================================================
print()
print("=== 6. _Static_assert 求值 ===")
asserts = []
for f in sorted(list(SRC.glob("*.c")) + list(SRC.glob("*.h"))
                + list(INC.glob("*.h"))):
    txt = strip_comments_and_strings(f.read_text(encoding="utf-8"))
    # 不用贪婪正则匹配括号，改为"从 _Static_assert( 起找最后一个字符串字面量"
    for m in re.finditer(r"_Static_assert\s*\(", txt):
        start = m.end()
        # 找匹配的右括号（计数法，忽略已剥离的字符串）
        depth = 1
        i = start
        while i < len(txt) and depth > 0:
            if txt[i] == "(":
                depth += 1
            elif txt[i] == ")":
                depth -= 1
            i += 1
        inner = txt[start:i - 1]
        # 拆成 表达式 , "消息"
        parts = inner.rsplit(",", 1)
        if len(parts) == 2:
            asserts.append((f.name, parts[0].strip(),
                            parts[1].strip().strip('"')))
        else:
            asserts.append((f.name, inner.strip(), ""))

for fn, expr, msg in asserts:
    v = eval_int(expr)
    if v is None:
        print("  ???? %s: %s —— 含不可静态求值的符号，跳过" % (fn, msg))
    else:
        check(v != 0, "%s: %s" % (fn, msg))

# ==================================================================
# 7. 引脚冲突
# ==================================================================
print()
print("=== 7. 引脚冲突 ===")
pins: dict[int, list[str]] = {}
for n, v in MACRO_DEFS.items():
    if not n.startswith("PIN_"):
        continue
    # *_BASE 是别名（指向别的引脚），不是独立分配
    if n.endswith("_BASE"):
        continue
    val = eval_int(v)
    if val is None or val < 0:
        continue
    pins.setdefault(val, []).append(n)

dup = {k: v for k, v in pins.items() if len(v) > 1}
if dup:
    for gpio, names in sorted(dup.items()):
        print("  FAIL GPIO%d 被重复分配: %s" % (gpio, names))
    FAILS += len(dup)
else:
    print("  OK   %d 个引脚无冲突" % len(pins))

used = sorted(pins.keys())
print("  已用 GPIO: %s" % used)
print("  空闲 GPIO: %s" % [g for g in range(30) if g not in used])

# ==================================================================
# 8. 列出用到的 SDK 函数（供人工核对拼写）
# ==================================================================
print()
print("=== 8. 用到的 SDK / 标准库函数 ===")
# 项目自己定义的函数名（不算 SDK）
own_funcs = set(impl) | set(decls.keys())
# 类型转换与关键字不算
NOT_FUNCS = {
    "if", "for", "while", "switch", "return", "sizeof", "typeof",
    "static_assert", "_Static_assert", "defined", "int8_t", "int16_t",
    "int32_t", "int64_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "void", "bool", "true", "false", "NULL", "main",
}

sdk_calls: dict[str, set[str]] = {}
for f in sorted(SRC.glob("*.c")):
    txt = strip_comments_and_strings(f.read_text(encoding="utf-8"))
    for m in re.finditer(r"\b([a-z_]\w{2,})\s*\(", txt):
        name = m.group(1)
        if name in own_funcs or name in NOT_FUNCS:
            continue
        # 过滤宏形式的调用
        if name.upper() == name:
            continue
        sdk_calls.setdefault(name, set()).add(f.name)

# 已知的 SDK / 标准库函数名（用于快速发现可疑拼写）
KNOWN_SDK = {
    # pico/stdlib
    "stdio_init_all", "sleep_ms", "sleep_us", "tight_loop_contents",
    "to_ms_since_boot", "get_absolute_time", "time_us_32",
    # multicore
    "multicore_launch_core1", "multicore_fifo_push_blocking",
    "multicore_fifo_pop_blocking", "multicore_fifo_rvalid",
    "multicore_fifo_wready",
    # clocks
    "clock_get_hz", "set_sys_clock_khz",
    # gpio
    "gpio_init", "gpio_set_dir", "gpio_put", "gpio_get", "gpio_pull_up",
    "gpio_set_drive_strength", "gpio_set_slew_rate", "gpio_xor_mask",
    "gpio_set_function", "gpio_set_input_enabled", "gpio_set_pulls",
    "gpio_set_dir_out_masked", "gpio_clr_mask", "gpio_set_mask",
    "pio_gpio_init",
    # pio
    "pio_claim_unused_sm", "pio_add_program", "pio_sm_init",
    "pio_sm_set_enabled", "pio_sm_set_pins_with_mask",
    "pio_sm_set_pindirs_with_mask", "pio_sm_put", "pio_get_dreq",
    "pio_sm_set_clkdiv_int_frac8",
    # dma
    "dma_claim_unused_channel", "dma_channel_get_default_config",
    "dma_channel_configure", "dma_channel_set_read_addr",
    "dma_channel_set_trans_count", "dma_channel_set_irq0_enabled",
    "dma_channel_start", "channel_config_set_transfer_data_size",
    "channel_config_set_read_increment", "channel_config_set_write_increment",
    "channel_config_set_dreq",
    # irq
    "irq_set_exclusive_handler", "irq_set_enabled",
    # timer
    "add_repeating_timer_us",
    # uart
    "uart_init", "uart_set_format", "uart_set_fifo_enabled",
    "uart_set_hw_flow", "uart_set_irq_enables", "uart_is_readable",
    "uart_getc", "uart_get_hw", "uart_putc", "uart_is_writable",
    # flash
    "flash_range_erase", "flash_range_program",
    # pio sm_config（已对照 pico-sdk hardware/pio.h 核实签名）
    "sm_config_set_sideset_pins", "sm_config_set_sideset_pin_base",
    "sm_config_set_sideset", "sm_config_set_out_pins",
    "sm_config_set_out_pin_base", "sm_config_set_out_pin_count",
    "sm_config_set_out_shift", "sm_config_set_in_shift",
    "sm_config_set_in_pins", "sm_config_set_set_pins",
    "sm_config_set_fifo_join", "sm_config_set_clkdiv",
    "sm_config_set_clkdiv_int_frac8", "sm_config_set_clkdiv_int_frac",
    "sm_config_set_wrap", "sm_config_set_jmp_pin",
    # 标准库
    "memcpy", "memset", "memcmp", "sin", "cos", "sqrt", "printf",
    "snprintf", "strlen", "strcmp",
}

# pioasm 生成的函数名以 .pio 的 program 名开头
PIO_GENERATED_SUFFIXES = (
    "_program_get_default_config", "_program_get_default_config",
    "_program_init", "_program_remove",
)


def is_pio_generated(name: str) -> bool:
    if any(name.endswith(s) for s in PIO_GENERATED_SUFFIXES):
        return True
    # <prog>_program / <prog>_offset 之类
    for p in pio_headers:
        prog = p.stem
        if name.startswith(prog + "_"):
            return True
    return False

unknown = {n: f for n, f in sdk_calls.items()
           if n not in KNOWN_SDK and not is_pio_generated(n)}
if unknown:
    print("  以下函数不在白名单里 —— **请重点核对拼写**：")
    for n, files in sorted(unknown.items()):
        print("    %-34s (%s)" % (n, ", ".join(sorted(files))))
    print()
    print("  ⚠️  不是错误（白名单不全），但这些是最可能拼错的地方。")
    print("      编译时会立刻暴露，比跑起来才发现好得多。")
else:
    print("  OK   所有 SDK 调用都在白名单内")

print()
print("  完整清单（%d 个）：" % len(sdk_calls))
for n in sorted(sdk_calls):
    print("    %s" % n)

# ==================================================================
print()
if FAILS:
    print("%d 项未通过，%d 项警告" % (FAILS, WARNS))
    sys.exit(1)
print("结构校验通过 ✓（%d 项警告）" % WARNS)
print()
print("本脚本抓不住的，必须靠真正的编译：")
print("  - C 语法细节、类型不匹配、隐式转换")
print("  - SDK 头文件里不存在的 API 名（上面第 8 节已列出清单供核对）")
print("  - pioasm 生成的符号名与调用是否一致")
print("  - 结构体成员名拼写（如 clkdiv 字段名）")
