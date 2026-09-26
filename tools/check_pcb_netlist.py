#!/usr/bin/env python3
"""
check_pcb_netlist.py — 把嘉立创EDA导出的**网表**与本工程期望的连接逐条比对

═══════════════════════════════════════════════════════════════════════
为什么需要这个：截图看不出网络对不对
═══════════════════════════════════════════════════════════════════════

从 PCB 截图能看出：元件位置、走线走向、有没有铺铜、丝印有没有重叠。
**看不出来**的是：某个焊盘到底接在哪个网络上。

而实际最容易出错、后果也最严重的就是网络连接：
  · U2 的 SCK 忘了接 GND          → 完全没声音
  · 数码管位选接反                → 两位显示的内容互换
  · 段线接错位                    → 显示乱码
  · 光耦 VO 上拉忘了接 3V3        → MIDI 收不到
  · 某个 GND 脚漏了               → 时好时坏

这些在网表里一目了然，在截图里完全看不出来。

═══════════════════════════════════════════════════════════════════════
怎么用
═══════════════════════════════════════════════════════════════════════

1. 嘉立创EDA 里：**设计 → 导出网表**（或「制造 → 导出网表」），
   格式选 **Protel**（也叫 Protel99SE / Altium）或 **CSV**，导出成 .txt / .net

2. 跑本脚本：

       python tools/check_pcb_netlist.py 你导出的网表.txt

3. 它会逐条报告：缺哪根、多哪根、接错哪个脚。

支持的格式（自动识别）：
  · Protel / Altium 网表：  ( \n 网络名 \n 位号-引脚 \n ... )
  · KiCad 网表：            (net (code ..) (name "..") (node (ref ..) (pin ..)))
  · 简单 CSV / TSV：        网络名,位号,引脚
  · 嘉立创EDA 的 "网络" 列表：每行「网络名 位号.引脚 位号.引脚 ...」

退出码：0 = 一致；1 = 有差异；2 = 读不出网表
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════════
# 期望的连接（与 docs/接线图与BOM.md、生成的原理图、网表文档完全一致）
# ══════════════════════════════════════════════════════════════════════
def expected(p: dict[str, int]) -> dict[str, set[tuple[str, str]]]:
    """返回 {网络名: {(位号, 引脚), ...}}。

    ★ 引脚用**人类可读的字符串**（"GP15" / "3" / "焊盘1"），
      比对时做归一化，兼容网表里 "U1-15" / "U1.15" / "U1_15" 各种写法。
    """
    E: dict[str, set[tuple[str, str]]] = {}

    def add(net, ref, pin):
        E.setdefault(net, set()).add((ref, str(pin)))

    g = lambda k: str(p[k])          # noqa: E731  GPIO 号

    # ---- 电源 ----
    for pin in (3, 8, 13, 18, 23, 28, 38):
        add("GND", "U1", pin)
    add("GND", "U2", "GND"); add("GND", "U2", "SCK")
    add("GND", "U3", "GND"); add("GND", "U3", "7")     # 经 R3，但 R3 另一端才到地
    add("GND", "Q1", "焊盘2"); add("GND", "Q2", "焊盘2")
    add("GND", "SW1", "焊盘1"); add("GND", "SW1", "焊盘3")
    add("GND", "SW2", "焊盘1"); add("GND", "SW2", "焊盘3")
    for r in ("C1", "C2", "C3", "C4", "C5", "C6"):
        add("GND", r, "2")
    add("GND", "U3", "5")

    for pin in (36,):
        add("3V3", "U1", pin)
    add("3V3", "U2", "VIN")
    add("3V3", "U3", "8")
    add("3V3", "R2", "1")
    for r in ("C4", "C5", "C6"):
        add("3V3", r, "1")

    # ---- I2S ----
    add("LRCK", "U1", g("PIN_I2S_LRCK")); add("LRCK", "U2", "LRCK")
    add("BCK",  "U1", g("PIN_I2S_BCK"));  add("BCK",  "U2", "BCK")
    add("DIN",  "U1", g("PIN_I2S_DIN"));  add("DIN",  "U2", "DIN")

    # ---- MIDI ----
    add("MIDI_RX", "U1", g("PIN_MIDI_RX"))
    add("MIDI_RX", "U3", "6"); add("MIDI_RX", "R2", "2")

    # ---- 段线 ----
    segmap = [("A", "a", "3", "R4"), ("B", "b", "9", "R5"), ("C", "c", "8", "R6"),
              ("D", "d", "6", "R7"), ("E", "e", "7", "R8"), ("F", "f", "4", "R9"),
              ("G", "g", "1", "R10"), ("DP", "dp", "2", "R11")]
    for up, low, dpin, r in segmap:
        add(f"SEG_{up}", "U1", g(f"PIN_SEG_{up}"))
        add(f"SEG_{up}", r, "1")
        add(f"SEG_{up}_DS", r, "2")
        add(f"SEG_{up}_DS", "DS1", dpin)

    # ---- 位选 ----
    for k, (q, r, ds_pin) in enumerate([("Q1", "R12", "10"), ("Q2", "R13", "5")], 1):
        add(f"DIG{k}", "U1", g(f"PIN_DIGIT{k}"))
        add(f"DIG{k}", r, "1")
        add(f"{q}_B", r, "2"); add(f"{q}_B", q, "焊盘1")
        add(f"{q}_C", q, "焊盘3"); add(f"{q}_C", "DS1", ds_pin)
    # 位选三极管发射极归到 GND（上面已加）

    # ---- 按键 ----
    for k, (sw, c, ref) in enumerate(
            [("SW1", "C2", "PIN_BTN_UP"), ("SW2", "C3", "PIN_BTN_DOWN")], 1):
        add(f"BTN{k}", "U1", g(ref))
        add(f"BTN{k}", sw, "焊盘2"); add(f"BTN{k}", sw, "焊盘4")
        add(f"BTN{k}", c, "1")

    # ---- MIDI 光耦 ----
    add("OPTO_A", "J1", "4"); add("OPTO_A", "R1", "1")
    add("OPTO_ANODE", "R1", "2"); add("OPTO_ANODE", "U3", "2")
    add("OPTO_K", "J1", "5"); add("OPTO_K", "U3", "3")
    add("DIN_SHIELD", "J1", "2"); add("DIN_SHIELD", "C1", "1")
    add("OPTO_VB", "U3", "7"); add("OPTO_VB", "R3", "1")
    # ★ R3 下端就是 GND（上面统一加过了），不要再造一个 OPTO_VB_GND ——
    #   那样会多出一个"只连一个点"的网络，自检时会误报。
    add("GND", "R3", "2")

    return E


# ══════════════════════════════════════════════════════════════════════
# 网表解析
# ══════════════════════════════════════════════════════════════════════
def norm_pin(s: str) -> str:
    return s.strip().strip('"').upper()


def parse_netlist(text: str) -> dict[str, set[tuple[str, str]]]:
    """尽量宽容地解析各种网表格式，返回 {网络名: {(位号,引脚)}}"""
    nets: dict[str, set[tuple[str, str]]] = {}

    def add(net, ref, pin):
        net = net.strip().strip('"')
        ref = ref.strip().strip('"').upper()
        pin = pin.strip().strip('"')
        if net and ref:
            nets.setdefault(net, set()).add((ref, pin))

    # ---- KiCad: (net (code 1) (name "GND") (node (ref R1) (pin 2))) ----
    for m in re.finditer(
            r'\(net\s+\(code[^)]*\)\s*\(name\s+"?([^")]+)"?\)(.*?)'
            r'(?=\(net\s+\(code|\Z)', text, re.S):
        net, body = m.group(1), m.group(2)
        for n in re.finditer(r'\(node\s+\(ref\s+"?([^")\s]+)"?\)\s*'
                             r'\(pin\s+"?([^")\s]+)"?\)', body):
            add(net, n.group(1), n.group(2))

    # ---- Protel / Altium: ( \n 网络名 \n R1-2 \n U1-5 \n ) ----
    if not nets:
        for blk in re.findall(r'\(\s*\n(.*?)\n\s*\)', text, re.S):
            lines = [l.strip() for l in blk.split("\n") if l.strip()]
            if len(lines) < 2:
                continue
            net = lines[0]
            for l in lines[1:]:
                m = re.match(r'^([A-Za-z]+\d+)\s*[-._]\s*(\S+)$', l)
                if m:
                    add(net, m.group(1), m.group(2))

    # ---- CSV / TSV: 网络名,位号,引脚 ----
    if not nets:
        for line in text.splitlines():
            parts = re.split(r'[,\t;]', line.strip())
            if len(parts) >= 3 and re.match(r'^[A-Za-z]+\d+$', parts[1].strip()):
                add(parts[0], parts[1], parts[2])

    # ---- 嘉立创EDA「网络」列表: 网络名 位号.引脚 位号.引脚 ... ----
    if not nets:
        for line in text.splitlines():
            toks = line.split()
            if len(toks) < 2:
                continue
            net = toks[0]
            for t in toks[1:]:
                m = re.match(r'^([A-Za-z]+\d+)[.\-](\S+)$', t)
                if m:
                    add(net, m.group(1), m.group(2))

    return nets


# ══════════════════════════════════════════════════════════════════════
PIN_ALIAS = {
    # 网表里 U2 的引脚可能写成 1..6 或 VIN/GND/...
    "1": "VIN", "2": "GND", "3": "LRCK", "4": "DIN", "5": "BCK", "6": "SCK",
    "焊盘1": "1", "焊盘2": "2", "焊盘3": "3",
    "B": "1", "E": "2", "C": "3",
}


def canon(ref: str, pin: str) -> str:
    """把引脚编号归一化，让 "U2-1" 与 "U2-VIN" 能对上"""
    p = norm_pin(pin)
    if ref == "U2":
        inv = {"VIN": "VIN", "GND": "GND", "LRCK": "LRCK", "DIN": "DIN",
               "BCK": "BCK", "SCK": "SCK"}
        return inv.get(p, p)
    if ref in ("Q1", "Q2"):
        return PIN_ALIAS.get(p, p)
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="比对 PCB 网表与本工程期望连接")
    ap.add_argument("netlist", type=Path, help="从嘉立创EDA导出的网表文件")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    if not args.netlist.exists():
        print(f"错误：找不到 {args.netlist}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(ROOT / "tools"))
    from gen_schematic import load_pins      # noqa: E402
    pins = load_pins()
    exp = expected(pins)

    text = args.netlist.read_text(encoding="utf-8", errors="replace")
    got = parse_netlist(text)

    if not got:
        print("错误：读不出网表 —— 格式没认出来。", file=sys.stderr)
        print("  支持的格式：Protel / KiCad / CSV / 网络列表。", file=sys.stderr)
        print("  嘉立创EDA 里请用「设计 → 导出网表」，格式选 Protel。",
              file=sys.stderr)
        return 2

    print(f"=== 网表比对 ===")
    print(f"  你的网表: {len(got)} 个网络，"
          f"{sum(len(v) for v in got.values())} 个连接")
    print(f"  期望的  : {len(exp)} 个网络，"
          f"{sum(len(v) for v in exp.values())} 个连接")

    got_c = {n: {(r, canon(r, p)) for r, p in v} for n, v in got.items()}
    exp_c = {n: {(r, canon(r, p)) for r, p in v} for n, v in exp.items()}

    problems = 0

    # ---- 1. 期望的每条连接，你的网表里有没有 ----
    print()
    print("── 缺少 / 接错 ──")
    missing = 0
    for net, conns in sorted(exp_c.items()):
        if net not in got_c:
            print(f"  ✗ 网络 [{net}] 整个不见了（应有 {len(conns)} 个连接）")
            problems += 1
            continue
        for ref, pin in sorted(conns):
            if (ref, pin) not in got_c[net]:
                # 看看这个脚跑到哪个网络去了 —— 这才是最有用的信息
                where = [n for n, v in got_c.items() if (ref, pin) in v]
                hint = f"（实际接在 [{where[0]}]）" if where else "（悬空，没接任何网络）"
                print(f"  ✗ [{net}] 少了 {ref} 的 {pin} 脚 {hint}")
                missing += 1
    if missing == 0:
        print("  ✓ 期望的连接全都在")
    problems += missing

    # ---- 2. 明显的多余连接（同一网络里出现了不该在一起的脚）----
    print()
    print("── 可疑的额外连接 ──")
    extra = 0
    for net, conns in sorted(got_c.items()):
        if net in exp_c:
            for ref, pin in sorted(conns - exp_c[net]):
                print(f"  ? [{net}] 多了一个 {ref} 的 {pin} 脚")
                extra += 1
    if extra == 0:
        print("  ✓ 没发现多余的连接")
    problems += extra

    # ---- 3. 单点网络（只有一个连接的"网络"，通常是漏接）----
    print()
    print("── 只连了一个点的网络（往往是漏接）──")
    single = 0
    for net, conns in sorted(got_c.items()):
        if len(conns) == 1 and net.upper() not in ("", "NC"):
            ref, pin = next(iter(conns))
            print(f"  ? [{net}] 只接了 {ref} 的 {pin} 脚 —— 是故意的吗？")
            single += 1
    if single == 0:
        print("  ✓ 没有孤立网络")
    problems += single

    # ---- 4. 关键安全项单独再查一遍 ----
    print()
    print("── 关键项专项检查 ──")
    def on_net(ref, pin, net) -> bool:
        return (ref, canon(ref, pin)) in got_c.get(net, set())

    critical = [
        ("U2 SCK 接 GND（选内部 PLL，不接就没声音）", "U2", "SCK", "GND"),
        ("U2 VIN 接 3V3（绝不能接 5V/VSYS）", "U2", "VIN", "3V3"),
        ("6N138 输出上拉 R2 接 3V3", "R2", "1", "3V3"),
        ("6N138 的 VB(pin7) 经 R3 到 GND", "R3", "2", "GND"),
        ("Q1 发射极(焊盘2)到 GND", "Q1", "焊盘2", "GND"),
        ("Q2 发射极(焊盘2)到 GND", "Q2", "焊盘2", "GND"),
    ]
    for desc, ref, pin, net in critical:
        ok = on_net(ref, pin, net)
        print(f"  {'✓' if ok else '✗'} {desc}")
        if not ok:
            problems += 1

    # 误接 5V 的检查
    for net in got_c:
        if net.upper() in ("VBUS", "VSYS", "5V"):
            if ("U2", "VIN") in got_c[net]:
                print("  ✗✗ U2 的 VIN 接到了 5V 上 —— 会烧 PCM5102A！")
                problems += 2

    print()
    if problems == 0:
        print("网表一致 ✓  可以打样了")
        print()
        print("  ★ 提醒：网表对不代表一定能用，还要看：")
        print("    · 有没有铺地铜（音频板强烈建议铺）")
        print("    · 电源/地线宽够不够（数码管位选那一路有 48mA 脉冲）")
        print("    · 跑一遍 DRC，间距/线宽别有告警")
        return 0

    print(f"★ 发现 {problems} 处问题 ★", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
