#!/usr/bin/env python3
"""
check_struct_sizes.py — 用 **ARM 编译器** 实测板载格式结构体的 sizeof，
                        与 PC 端工具链的常量对拍。

★ 为什么需要这个脚本（这是本项目代价最大的一个缺陷，值得写清楚）

  GMSS 的二进制格式是"一个 C 头文件 + 一个 Python 模块"共同定义的契约：
     firmware/include/gmss_format.h   ← 固件按这个读
     tools/gmss/gmss/layout.py        ← PC 端按这个写
     tools/gmss/gmss/player.py        ← 参考解码器按这个读

  两边各写各的，谁也没问过对方到底多大。

  实际发生的事：`gmss_zone_t` 在 C 里是 **56 字节**，
  而 Python 端写成了 **58 字节**（格式串 <Bbhh> 多了一个 int16 填充，
  C 那边 root_key/tune_cents/gain_db100 只占 4 字节）。

  后果：zone 表**每一条都错位 2 字节**。固件读到的
  key_lo / vel_hi / sample_off 全是垃圾 —— 不崩溃、不报错，
  只是"音频完全乱掉但程序照跑"。

  ★ 最要命的是：**它躲过了全部 38 个单元测试**。
    因为那些测试全是 Python↔Python 的自洽检查
    （pack 完再 unpack，两边都用同一个错常量，自然自洽）。
    **没有任何一处真正问过 C 编译器 sizeof 是多少。**

  这个脚本就是补上那一处。它做的事很朴素：
    写一个 .c 探针，让编译器把 sizeof 编进符号大小，再读出来对比。

用法：
    python tools/check_struct_sizes.py            # 自动找工具链
    python tools/check_struct_sizes.py --gcc <path>
    python tools/check_struct_sizes.py --quiet

退出码：0 = 一致；1 = 不一致（会打印每个字段的差异）；2 = 找不到工具链
（找不到工具链时返回 2 而不是 1 —— "没检查"和"检查失败"是两回事，
 不该混为一谈，否则 CI 上会误判。）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER_DIR = ROOT / "firmware" / "include"
GMSS_PY = ROOT / "tools" / "gmss"

# 工具链搜索顺序（第一个存在的就用）
GCC_CANDIDATES = [
    os.environ.get("PICO_TOOLCHAIN_PATH", "") and
    Path(os.environ["PICO_TOOLCHAIN_PATH"]) / "bin" / "arm-none-eabi-gcc",
    Path(r"D:\pico-tc\arm-gcc\bin\arm-none-eabi-gcc.exe"),
    Path(r"D:\pico-tc\arm-gcc\bin\arm-none-eabi-gcc"),
    Path("arm-none-eabi-gcc"),          # 走 PATH
]

# 探针：把 sizeof 变成数组长度，编译器会把它编进符号大小里。
# ★ 不用 static_assert 是因为断言失败时 GCC 不告诉你实际值是多少，
#   而 nm -S 能把真实尺寸读出来 —— 报错信息里带上"实际是 N"才有用。
PROBE_TEMPLATE = """\
#include <stdint.h>
#include <stddef.h>
#include "gmss_format.h"

const char probe_header[sizeof(gmss_header_t)];
const char probe_instr [sizeof(gmss_instrument_t)];
const char probe_zone  [sizeof(gmss_zone_t)];
const char probe_chip  [sizeof(gmss_chip_header_t)];

/* 字段偏移也一起量出来 —— 尺寸对但偏移错同样是灾难 */
const char off_zone_sample_off    [offsetof(gmss_zone_t, sample_off)    + 1];
const char off_zone_sample_len    [offsetof(gmss_zone_t, sample_len)    + 1];
const char off_zone_loop_start    [offsetof(gmss_zone_t, loop_start)    + 1];
const char off_zone_loop_end      [offsetof(gmss_zone_t, loop_end)      + 1];
const char off_zone_attack        [offsetof(gmss_zone_t, attack_samples)+ 1];
const char off_zone_keyframe_off  [offsetof(gmss_zone_t, keyframe_off)  + 1];
const char off_zone_keyframe_cnt  [offsetof(gmss_zone_t, keyframe_count)+ 1];
const char off_zone_root_key      [offsetof(gmss_zone_t, root_key)      + 1];
const char off_zone_env_attack    [offsetof(gmss_zone_t, env_attack_ms) + 1];
const char off_zone_loop_xfade    [offsetof(gmss_zone_t, loop_xfade)    + 1];
const char off_instr_zone_first   [offsetof(gmss_instrument_t, zone_first) + 1];
const char off_instr_bank         [offsetof(gmss_instrument_t, bank)       + 1];
const char off_instr_name         [offsetof(gmss_instrument_t, name)       + 1];
"""

# 符号名 → 从 Python 端读哪个常量
SIZE_CHECKS = [
    ("probe_header", "gmss_header_t",     "HEADER_SIZE"),
    ("probe_instr",  "gmss_instrument_t", "INSTR_SIZE"),
    ("probe_zone",   "gmss_zone_t",       "ZONE_SIZE"),
    ("probe_chip",   "gmss_chip_header_t", "CHIP_HDR_SIZE"),
]

# 偏移符号 → (结构体字段, 期望值来自 C 头文件的 _Static_assert)
OFFSET_CHECKS = [
    ("off_zone_sample_off",   "gmss_zone_t.sample_off",     8),
    ("off_zone_sample_len",   "gmss_zone_t.sample_len",     12),
    ("off_zone_loop_start",   "gmss_zone_t.loop_start",     16),
    ("off_zone_loop_end",     "gmss_zone_t.loop_end",       20),
    ("off_zone_attack",       "gmss_zone_t.attack_samples", 24),
    ("off_zone_keyframe_off", "gmss_zone_t.keyframe_off",   28),
    ("off_zone_keyframe_cnt", "gmss_zone_t.keyframe_count", 32),
    ("off_zone_root_key",     "gmss_zone_t.root_key",       36),
    ("off_zone_env_attack",   "gmss_zone_t.env_attack_ms",  40),
    ("off_zone_loop_xfade",   "gmss_zone_t.loop_xfade",     48),
    ("off_instr_zone_first",  "gmss_instrument_t.zone_first", 0),
    ("off_instr_bank",        "gmss_instrument_t.bank",       4),
    ("off_instr_name",        "gmss_instrument_t.name",       8),
]


def find_tool(name: str) -> str | None:
    """找一个 ARM 工具链可执行文件"""
    for c in GCC_CANDIDATES:
        if not c:
            continue
        s = str(c)
        if os.path.sep in s or "/" in s:
            if Path(s).exists():
                return s
        else:
            w = shutil.which(s)
            if w:
                return w
    return None


def measure(gcc: str, tmp: Path, quiet: bool) -> dict[str, int]:
    """编译探针并读出所有符号的大小"""
    src = tmp / "probe.c"
    obj = tmp / "probe.o"
    src.write_text(PROBE_TEMPLATE, encoding="utf-8")

    cmd = [gcc, "-c", str(src), "-o", str(obj),
           "-I", str(HEADER_DIR),
           "-mcpu=cortex-m33", "-mthumb", "-std=gnu11"]
    if not quiet:
        print("  编译探针:")
        print("    " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("错误：探针编译失败", file=sys.stderr)
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)

    # nm -S 输出: "<addr> <size> <type> <name>"（小写 = 局部，大写 = 全局）
    # ★ 必须从 **gcc 所在目录** 推 nm 的路径，不能走 find_tool()：
    #   find_tool 只认 GCC_CANDIDATES 里那几个 gcc 路径，
    #   拿 "arm-none-eabi-nm" 去问它只会返回 gcc 本身，
    #   于是"用 gcc 当 nm 使"，对象文件量不出任何符号，
    #   所有项都报"量不到"——校验形同虚设却还显示在跑。
    nm = str(Path(gcc).parent / Path(gcc).name.replace("gcc", "nm"))
    if not Path(nm).exists():
        nm = shutil.which("arm-none-eabi-nm") or nm
    if not Path(nm).exists():
        print(f"错误：找不到 arm-none-eabi-nm（试过 {nm}）", file=sys.stderr)
        return {}
    r = subprocess.run([nm, "-S", str(obj)], capture_output=True, text=True)
    if r.returncode != 0:
        print("错误：nm 读取失败", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)

    sizes: dict[str, int] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        # 形如: 00000000 00000040 R probe_header
        if len(parts) == 4 and re.fullmatch(r"[0-9a-fA-F]{8,16}", parts[1]):
            sizes[parts[3]] = int(parts[1], 16)
    return sizes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="用 ARM 编译器实测 gmss 格式结构体的 sizeof，与 Python 端常量对拍")
    ap.add_argument("--gcc", help="arm-none-eabi-gcc 路径（默认自动查找）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    gcc = args.gcc or find_tool("arm-none-eabi-gcc")
    if not gcc:
        print("跳过：找不到 arm-none-eabi-gcc。", file=sys.stderr)
        print("  设 PICO_TOOLCHAIN_PATH，或用 --gcc 指定路径。", file=sys.stderr)
        return 2

    sys.path.insert(0, str(GMSS_PY))
    try:
        from gmss import layout as L          # noqa: N812
    except Exception as e:                     # noqa: BLE001
        print(f"错误：import gmss.layout 失败：{e}", file=sys.stderr)
        return 1

    print("=== GMSS 结构体尺寸对拍（C 编译器实测 vs Python 常量）===")
    print(f"  工具链: {gcc}")
    print(f"  头文件: {HEADER_DIR / 'gmss_format.h'}")

    with tempfile.TemporaryDirectory() as td:
        sizes = measure(gcc, Path(td), args.quiet)

    ok = True

    print()
    print("  --- 结构体尺寸 ---")
    print(f"  {'结构体':<24} {'C sizeof':>10} {'Python':>10}   判定")
    for sym, cname, pyattr in SIZE_CHECKS:
        cval = sizes.get(sym)
        pyname = pyattr
        pyval = getattr(L, pyattr, None)
        if pyval is None and pyattr in ("CHIP_HDR_SIZE",):
            pyval = getattr(L, pyattr, None)
        if cval is None:
            print(f"  {cname:<24} {'?':>10} {pyval!s:>10}   ⚠️ 量不到")
            ok = False
            continue
        match = (cval == pyval)
        ok &= match
        mark = "OK" if match else "★ 不一致 ★"
        print(f"  {cname:<24} {cval:>10} {pyval!s:>10}   {mark}")
        if not match:
            print(f"      → Python 端 {pyattr} 应为 {cval}（当前 {pyval}）")

    print()
    print("  --- zone 字段偏移（尺寸对但偏移错同样是灾难）---")
    print(f"  {'字段':<34} {'C':>5} {'期望':>5}   判定")
    for sym, field, expect in OFFSET_CHECKS:
        got = sizes.get(sym)
        if got is None:
            print(f"  {field:<34} {'?':>5} {expect:>5}   ⚠️ 量不到")
            ok = False
            continue
        got -= 1        # 探针里是 offsetof(...)+1，减回来
        match = (got == expect)
        ok &= match
        mark = "OK" if match else "★ 不一致 ★"
        print(f"  {field:<34} {got:>5} {expect:>5}   {mark}")

    print()
    if ok:
        print("全部一致 ✓")
        print()
        print("  ★ 这个脚本存在的意义：它曾经抓到过一个真实缺陷 ——")
        print("    gmss_zone_t 在 C 里是 56 字节，而 Python 端写成了 58。")
        print("    那会让 zone 表每条错位 2 字节，音频全乱但不报错，")
        print("    而且**躲过了全部 38 个 Python 单元测试**")
        print("    （那些测试都是 Python 自洽检查，从没问过 C 编译器）。")
        print("    改了 gmss_format.h 或 layout.py 之后请务必重跑本脚本。")
        return 0
    else:
        print("★ 有项目不一致 ★", file=sys.stderr)
        print("  gmss_format.h 和 layout.py 是同一个二进制格式的两份定义，", file=sys.stderr)
        print("  任何一边改了都要同步另一边。不一致的后果是**静默数据错乱**，", file=sys.stderr)
        print("  不是编译错误。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
