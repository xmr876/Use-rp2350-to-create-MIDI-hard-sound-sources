"""
gen_dsp_tables.py — 生成固件用的数值表头文件

产出: firmware/src/dsp_tables.h
   k_vel_q15[]   MIDI 力度 → Q15 增益（0..127，128 项）
   k_db_q15[]    dB 衰减 → Q15 线性增益（0..96 dB，97 项）

★ 为什么用脚本生成而不是手写：
  第一版手写的力度表把最后一项写成了 68199 —— 越出 Q15 上限 32767。
  这种错误编译器不会报（只是值不对），听感上是"力度大了就破音"，
  极难定位。生成 + 自动校验才是可靠做法。

运行:  python tools/gen_dsp_tables.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "firmware" / "src" / "dsp_tables.h"

Q15 = 32767.0


def build_vel() -> list[int]:
    """力度 0..127 → 平方律增益

    gain = (vel/127)^2 * 32767

    vel=0 特殊处理为 0（MIDI 里 velocity 0 等同 note-off）。
    """
    out = [0] * 128
    for v in range(1, 128):
        out[v] = int(round((v / 127.0) ** 2 * Q15))
    out[127] = 32767          # 保证满幅精确
    return out


def build_db() -> list[int]:
    """0..96 dB 衰减 → Q15 线性增益

    gain = 10^(-db/20) * 32767
    """
    out = []
    for d in range(97):
        v = int(round((10.0 ** (-d / 20.0)) * Q15))
        out.append(max(1, min(32767, v)))    # 夹紧，且不出现 0（0 会让乘法永久静音）
    return out


def check(name: str, tab: list[int], expect_len: int,
          monotone: str) -> bool:
    """自检：长度、值域、单调性"""
    ok = True
    if len(tab) != expect_len:
        print("  !! %s 长度 %d，期望 %d" % (name, len(tab), expect_len))
        ok = False
    if min(tab) < 0 or max(tab) > 32767:
        print("  !! %s 越出 Q15 值域: %d .. %d" % (name, min(tab), max(tab)))
        ok = False
    if monotone == "up" and any(tab[i] > tab[i + 1] for i in range(len(tab) - 1)):
        print("  !! %s 非单调不减" % name)
        ok = False
    if monotone == "down" and any(tab[i] < tab[i + 1] for i in range(len(tab) - 1)):
        print("  !! %s 非单调不增" % name)
        ok = False
    if tab[-1] == 0 and monotone == "down":
        print("  !! %s 末项为 0（会让乘法永久静音）" % name)
        ok = False
    return ok


def emit_array(lines: list[str], name: str, ctype: str, tab: list[int],
               per_line: int = 12, comment: str = "") -> None:
    lines.append("/* %s */" % comment if comment else "")
    lines.append("#define %s_LEN (%d)" % (name.upper(), len(tab)))
    lines.append("static const %s %s[%s_LEN] = {" % (ctype, name, name.upper()))
    row = "    "
    for i, v in enumerate(tab):
        row += "%6d," % v
        if (i + 1) % per_line == 0:
            lines.append(row)
            row = "    "
    if row.strip():
        lines.append(row)
    lines.append("};")
    lines.append("")


def main() -> int:
    vel = build_vel()
    db = build_db()

    print("校验:")
    ok = check("k_vel_q15", vel, 128, "up")
    ok &= check("k_db_q15", db, 97, "down")
    print("  力度表: len=%d, [0]=%d [64]=%d [127]=%d"
          % (len(vel), vel[0], vel[64], vel[127]))
    print("  dB 表:  len=%d, [0]=%d [6]=%d [96]=%d"
          % (len(db), db[0], db[6], db[96]))
    if not ok:
        print("错误：自检未通过", file=sys.stderr)
        return 1

    lines: list[str] = []
    lines.append("/*")
    lines.append(" * dsp_tables.h — DSP 数值表（自动生成，请勿手工修改）")
    lines.append(" *")
    lines.append(" * 由 tools/gen_dsp_tables.py 生成。要改表请改脚本再重新生成。")
    lines.append(" *")
    lines.append(" * 全部为 Q15（32767 ≈ 1.0），值域 [0, 32767]。")
    lines.append(" *")
    lines.append(" * ★ 不要手工编辑这个文件。第一版手写的力度表把末项写成 68199，")
    lines.append(" *   越出 Q15 上限，编译器不报错，但力度大了就破音。")
    lines.append(" */")
    lines.append("#ifndef DSP_TABLES_H")
    lines.append("#define DSP_TABLES_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("")
    emit_array(lines, "k_vel_q15", "int16_t", vel, 12,
               "MIDI 力度 0..127 → Q15 增益（平方律 (vel/127)^2）")
    emit_array(lines, "k_db_q15", "int16_t", db, 12,
               "0..96 dB 衰减 → Q15 线性增益（10^(-db/20)）")
    lines.append("#endif /* DSP_TABLES_H */")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print()
    print("已生成 %s" % OUT)
    print("  %d 字节" % OUT.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
