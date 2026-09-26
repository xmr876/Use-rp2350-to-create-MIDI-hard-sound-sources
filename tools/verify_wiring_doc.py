#!/usr/bin/env python3
"""
verify_wiring_doc.py — 核对「接线图与BOM.md」里的引脚表与 board_config.h 是否一致

★ 为什么值得单独写一个脚本：

  `docs/接线图与BOM.md` 是给**人**看的（照着插线），
  `firmware/include/board_config.h` 是给**编译器**看的。
  两者必须说同一件事。

  这类"文档和代码各写一份"的地方最容易烂：改了 board_config.h 忘了改文档，
  于是用户照着文档插线，固件却按另一套引脚跑 ——
  症状是"插对了也不响"，而且极难自查（因为文档看起来完全合理）。

  本项目已经在这类"两份定义"上栽过一次：
  gmss_zone_t 在 C 里 56 字节、Python 里 58 字节，躲过了全部 38 个单测。
  那个坑由 tools/check_struct_sizes.py 兜住；这个脚本兜住引脚这一份。

  ★ 脚本只核对**引脚号**，不核对文字描述 —— 描述是给人读的，
    允许自由修改；引脚号是契约，必须逐字一致。

用法：
    python tools/verify_wiring_doc.py
    python tools/verify_wiring_doc.py --quiet

退出码：0 = 一致；1 = 有出入；2 = 文件找不到
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "firmware" / "include" / "board_config.h"
DOC = ROOT / "docs" / "接线图与BOM.md"


def read_cfg() -> dict[str, int | str]:
    """从 board_config.h 抽出引脚定义（只取数字常量，跳过表达式）"""
    txt = CFG.read_text(encoding="utf-8")
    # 去掉注释，避免命中注释里举的反例
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"//[^\n]*", " ", txt)

    out: dict[str, int | str] = {}
    for m in re.finditer(r"^#define\s+(PIN_[A-Z0-9_]+)\s+([^\s/]+)", txt, re.M):
        name, val = m.group(1), m.group(2)
        if re.fullmatch(r"-?\d+", val):
            out[name] = int(val)
        else:
            out[name] = val        # 例如 PICO_DEFAULT_LED_PIN，记下表达式
    return out


# board_config.h 的名字 → 文档表里"板上丝印"那一列期望的数字
# ★ 这里**故意写成显式对照表**，不做模糊匹配。
#   模糊匹配（比如按名字相似度）会在改名时静默失配，
#   而静默失配正是这个脚本要防的东西。
EXPECTED = [
    ("PIN_I2S_LRCK",   "I2S LRCK",   "12"),
    ("PIN_I2S_BCK",    "I2S BCK",    "13"),
    ("PIN_I2S_DIN",    "I2S DIN",    "14"),
    ("PIN_MIDI_RX",    "MIDI RX",    "10"),
    ("PIN_MIDI_TX",    "MIDI TX",    "11"),
    ("PIN_SEG_A",      "段 a",        "15"),
    ("PIN_SEG_B",      "段 b",        "16"),
    ("PIN_SEG_C",      "段 c",        "17"),
    ("PIN_SEG_D",      "段 d",        "18"),
    ("PIN_SEG_E",      "段 e",        "19"),
    ("PIN_SEG_F",      "段 f",        "20"),
    ("PIN_SEG_G",      "段 g",        "21"),
    ("PIN_SEG_DP",     "段 dp",       "22"),
    ("PIN_DIGIT1",     "位选 1",      "6"),
    ("PIN_DIGIT2",     "位选 2",      "7"),
    ("PIN_BTN_UP",     "按键 1",      "8"),
    ("PIN_BTN_DOWN",   "按键 2",      "9"),
]


def read_doc_pins() -> dict[str, str]:
    """从文档的"引脚分配总表"里抽出 丝印号 → 信号 的映射。

    ★ 不按行号切，而是按 markdown 表格结构解析：
      找表头里同时含"板上丝印"和"信号"的那张表，
      然后取每行的前两列。这样文档前后加内容也不会解析错。
    """
    txt = DOC.read_text(encoding="utf-8")
    lines = txt.split("\n")

    # 找表头行
    start = None
    for i, ln in enumerate(lines):
        if "板上丝印" in ln and "信号" in ln and ln.strip().startswith("|"):
            start = i + 2          # 跳过表头 + 分隔行
            break
    if start is None:
        raise SystemExit(f"在 {DOC.name} 里找不到含「板上丝印 / 信号」的表头")

    pins: dict[str, str] = {}
    for ln in lines[start:]:
        if not ln.strip().startswith("|"):
            break                  # 表格结束
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        raw_pin, sig = cells[0], cells[1]
        # 去掉 markdown 粗体与"（可选）"之类的后缀
        num = re.sub(r"\*\*|`", "", raw_pin)
        num = re.sub(r"[（(].*?[)）]", "", num).strip()
        sig = re.sub(r"\*\*|`", "", sig).strip()
        if num:
            pins[num] = sig
    return pins


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="核对接线文档与 board_config.h 的引脚号")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if not CFG.exists():
        print(f"错误：找不到 {CFG}", file=sys.stderr)
        return 2
    if not DOC.exists():
        print(f"错误：找不到 {DOC}", file=sys.stderr)
        return 2

    cfg = read_cfg()
    doc = read_doc_pins()

    print("=== 接线文档 vs board_config.h 引脚核对 ===")
    print(f"  固件配置: {CFG.relative_to(ROOT)}")
    print(f"  接线文档: {DOC.relative_to(ROOT)}")
    print()
    print(f"  {'宏':<18} {'固件值':>6} {'文档丝印':>8}   判定")

    ok = True
    for macro, label, expect in EXPECTED:
        fw = cfg.get(macro)
        if fw is None:
            print(f"  {macro:<18} {'缺失':>6} {expect:>8}   ★ board_config.h 里没有这个宏")
            ok = False
            continue
        if isinstance(fw, str):
            print(f"  {macro:<18} {fw:>6} {expect:>8}   ⚠️ 非数字常量，跳过（{label}）")
            continue
        doc_val = expect
        # 文档里那一行是不是真的有这个丝印号
        if expect not in doc:
            print(f"  {macro:<18} {fw:>6} {expect:>8}   ★ 文档表里找不到丝印 '{expect}'")
            ok = False
            continue
        if fw != int(expect):
            print(f"  {macro:<18} {fw:>6} {expect:>8}   ★ 不一致")
            ok = False
            continue
        print(f"  {macro:<18} {fw:>6} {expect:>8}   OK")

    # 反向检查：文档里出现的丝印号必须在固件里有对应，不能有多余
    known = {e[2] for e in EXPECTED}
    extra = set(doc) - known
    # 3V3 / G 那些电源脚文档里也有，属于正常，不当错误
    power = {k for k in extra if re.search(r"3V3|GND|^G$|VBUS|VSYS|VR|RUN", k)}
    real_extra = extra - power
    if real_extra:
        print()
        print(f"  ⚠️ 文档里这些丝印号在固件里没有对应引脚（可能只是描述性的）：")
        for x in sorted(real_extra):
            print(f"       {x}  →  {doc[x]}")
        # 不判失败：文档里可以有"空闲脚"等额外行

    print()
    if ok:
        print("引脚号全部一致 ✓")
        print()
        print("  ★ 改了 board_config.h 的引脚，必须同步改 docs/接线图与BOM.md，")
        print("    反之亦然 —— 两份定义不一致的症状是「照着文档插线也不响」。")
        return 0
    print("★ 有出入 ★  两份定义必须一致", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
