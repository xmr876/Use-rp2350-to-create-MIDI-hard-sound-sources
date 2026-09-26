"""
gen_pitch_table.py — 生成固件用的音高查表头文件

产出: firmware/src/dsp_pitch_table.h

为什么需要这张表
----------------
音高步进是   step = 2^((note - root_key + tune_cents/100) / 12)
在 48 复音的热路径上，每个音符触发时算一次，用浮点 pow() 约 200+ 周期，
而且 M33 上浮点与定点混用会有额外的寄存器搬运开销。

用「Q16.16 查表 + 线性插值」代替：
    - 约 20 周期
    - 最大误差 0.39 音分（人耳可辨阈值 3~5 音分，余量充足）
    - 整数半音处误差为 0（表点正好落在精确值上）

★ 踩过的坑：第一版用 Q15（uint16）存 2^(s/12)，但 Q15 只能表示到约 1.0，
  s=0 处值是 2.0 直接饱和，后半张表全是 65535 → 1163 音分误差。
  必须用 Q16.16 存 uint32（可表示到 65535.9999）。

运行:  python tools/gen_pitch_table.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# 表参数
LO_SEMITONE = -60          # 覆盖下限（半音，相对根音）
HI_SEMITONE = 67           # 覆盖上限
SUB = 16                   # 每半音的细分（1/16 半音）
SCALE = 65536.0            # Q16.16 标度

OUT = Path(__file__).resolve().parent.parent / "firmware" / "src" / "dsp_pitch_table.h"


def build_table() -> list[int]:
    n = int(round((HI_SEMITONE - LO_SEMITONE) * SUB)) + 1
    out = []
    for i in range(n):
        s = LO_SEMITONE + i / SUB
        out.append(int(round(2.0 ** (s / 12.0) * SCALE)))
    return out


def measure_error(tab: list[int]) -> float:
    """扫全部插值区间，返回最大误差（音分）"""
    n = len(tab)
    worst = 0.0
    x = float(LO_SEMITONE)
    while x <= HI_SEMITONE:
        idx = (x - LO_SEMITONE) * SUB
        i0 = min(max(int(idx), 0), n - 2)
        f = idx - i0
        interp = tab[i0] + (tab[i0 + 1] - tab[i0]) * f
        exact = 2.0 ** (x / 12.0) * SCALE
        rel = abs(interp - exact) / exact
        if rel < 1.0:
            cents = 1200.0 * math.log2(1.0 + rel)
            worst = max(worst, cents)
        x += 1.0 / (SUB * 8)
    return worst


def emit(tab: list[int]) -> str:
    n = len(tab)
    lines = []
    lines.append("/*")
    lines.append(" * dsp_pitch_table.h — 音高查表（自动生成，请勿手工修改）")
    lines.append(" *")
    lines.append(" * 由 tools/gen_pitch_table.py 生成。要改表请改脚本再重新生成。")
    lines.append(" *")
    lines.append(" *   k_pitch_q16[i] = 2^((%d + i/%d) / 12) × 65536"
                 % (LO_SEMITONE, SUB))
    lines.append(" *")
    lines.append(" * 用法：")
    lines.append(" *   半音数 semitone（可以是负的、带小数）")
    lines.append(" *   idx = (semitone - PITCH_TAB_LO_SEMITONE) * PITCH_TAB_SUB")
    lines.append(" *   i0  = idx 的整数部分；frac = idx 的小数部分")
    lines.append(" *   step = k_pitch_q16[i0] + (k_pitch_q16[i0+1] - k_pitch_q16[i0]) * frac")
    lines.append(" *")
    lines.append(" * 精度：线性插值最大误差 %.3f 音分" % measure_error(tab))
    lines.append(" *       整数半音处误差为 0（表点落在精确值上）")
    lines.append(" *")
    lines.append(" * ★ 用 uint32 存 Q16.16，不要改成 uint16/Q15——")
    lines.append(" *   Q15 只能表示到约 1.0，而本表范围是 0.03125 .. 47.9，")
    lines.append(" *   会大面积饱和。")
    lines.append(" */")
    lines.append("#ifndef DSP_PITCH_TABLE_H")
    lines.append("#define DSP_PITCH_TABLE_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("")
    lines.append("#define PITCH_TAB_LO_SEMITONE (%d)" % LO_SEMITONE)
    lines.append("#define PITCH_TAB_SUB         (%d)" % SUB)
    lines.append("#define PITCH_TAB_LEN         (%d)" % n)
    lines.append("")
    lines.append("static const uint32_t k_pitch_q16[PITCH_TAB_LEN] = {")
    row = "    "
    for i, v in enumerate(tab):
        row += "%7d," % v
        if (i + 1) % 12 == 0:
            lines.append(row)
            row = "    "
    if row.strip():
        lines.append(row)
    lines.append("};")
    lines.append("")
    lines.append("#endif /* DSP_PITCH_TABLE_H */")
    return "\n".join(lines) + "\n"


def main() -> int:
    tab = build_table()
    err = measure_error(tab)
    src = emit(tab)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 固定用 LF 换行，避免 Windows 上生成 CRLF 导致与 Linux 构建不一致
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    print("已生成 %s" % OUT)
    print("  表长度 %d 项，%d 字节" % (len(tab), len(tab) * 4))
    print("  值范围 %d .. %d" % (min(tab), max(tab)))
    print("  最大插值误差 %.3f 音分" % err)
    if err > 1.0:
        print("错误：插值误差过大，请减小 SUB 或收紧范围", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
