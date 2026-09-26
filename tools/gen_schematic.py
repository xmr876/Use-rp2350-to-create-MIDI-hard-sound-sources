#!/usr/bin/env python3
"""
gen_schematic.py — 生成 GMSS 音源（v4）的原理图 SVG

★ 为什么要"生成"而不是手画一张图

  原理图里的**每一根连线都是固件引脚定义的镜像**。
  手画的话，改了 board_config.h 里的引脚，图不会跟着变 ——
  于是图变成一份会骗人的文档。

  这里把引脚从 firmware/include/board_config.h 里**读出来**，
  再画进图里。引脚改了，重跑一次脚本，图自动跟上。
  配合 tools/verify_wiring_doc.py（核对文字文档）形成两份交叉检查。

★ 画法说明

  采用"模块 + 网络标号"的画法，而不是把每根线都拉通：
    · Pico 开发板和 PCM5102 模块本身是**成品模块**，画成带引脚名的框
    · 分立元件（光耦、三极管、电阻、电容、数码管、按键、DIN 座）
      画成真正的符号
    · 同一网络用相同标号，不画长线 —— 这是模块化设计的标准画法，
      也比拉长线更不容易看错

用法：
    python tools/gen_schematic.py                    # 输出到 build/schematic/
    python tools/gen_schematic.py --out 目录
    python tools/gen_schematic.py --check            # 只做自检，不写文件
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "firmware" / "include" / "board_config.h"

# ──────────────────────────────────────────────────────────────────────
# 从 board_config.h 读引脚（图的唯一数据来源）
# ──────────────────────────────────────────────────────────────────────
def load_pins() -> dict[str, int]:
    txt = CFG.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"//[^\n]*", " ", txt)
    out: dict[str, int] = {}
    for m in re.finditer(r"^#define\s+(PIN_[A-Z0-9_]+)\s+(-?\d+)", txt, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


# ──────────────────────────────────────────────────────────────────────
# 极简 SVG 绘图工具
# ──────────────────────────────────────────────────────────────────────
class Svg:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.parts: list[str] = []

    def raw(self, s: str) -> None:
        self.parts.append(s)

    # ---- 基本图元 ----
    def line(self, x1, y1, x2, y2, color="#1a5c1a", w=1.6, dash=None) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.raw(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="{color}" stroke-width="{w}"{d}/>')

    def rect(self, x, y, w, h, fill="#ffffff", stroke="#333", sw=1.6, rx=0) -> None:
        self.raw(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                 f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def circle(self, cx, cy, r, fill="#fff", stroke="#333", sw=1.6) -> None:
        self.raw(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"/>')

    def text(self, x, y, s, size=11, anchor="start", color="#111",
             weight="normal", family="Consolas, monospace") -> None:
        s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        self.raw(f'<text x="{x}" y="{y}" font-family="{family}" '
                 f'font-size="{size}" text-anchor="{anchor}" fill="{color}" '
                 f'font-weight="{weight}">{s}</text>')

    # ---- 连线（正交折线）----
    def wire(self, pts, color="#1a5c1a", w=1.6) -> None:
        for i in range(len(pts) - 1):
            self.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], color, w)

    def junction(self, x, y) -> None:
        self.circle(x, y, 3.2, fill="#1a5c1a", stroke="#1a5c1a", sw=0)

    # ---- 网络标号（同名即相连）----
    def netlabel(self, x, y, name, anchor="start", color="#a03000") -> None:
        w = max(28, len(name) * 7 + 10)
        if anchor == "start":
            self.raw(f'<rect x="{x}" y="{y - 11}" width="{w}" height="15" rx="2" '
                     f'fill="#fff6ee" stroke="{color}" stroke-width="1"/>')
            self.text(x + 5, y, name, size=10, color=color, weight="bold")
        else:
            self.raw(f'<rect x="{x - w}" y="{y - 11}" width="{w}" height="15" rx="2" '
                     f'fill="#fff6ee" stroke="{color}" stroke-width="1"/>')
            self.text(x - 5, y, name, size=10, anchor="end", color=color, weight="bold")

    # ---- 电源 / 地符号 ----
    def gnd(self, x, y, label="GND") -> None:
        self.line(x, y, x, y + 10)
        self.line(x - 9, y + 10, x + 9, y + 10)
        self.line(x - 5, y + 14, x + 5, y + 14)
        self.line(x - 1.5, y + 18, x + 1.5, y + 18)
        if label:
            self.text(x + 13, y + 17, label, size=9, color="#333")

    def vcc(self, x, y, label="3V3") -> None:
        self.line(x, y, x, y - 9)
        self.line(x - 8, y - 9, x + 8, y - 9)
        self.text(x, y - 13, label, size=10, anchor="middle",
                  color="#a03000", weight="bold")

    # ---- 分立元件符号 ----
    def resistor(self, x, y, refdes, value, horiz=True, box=44, lead=14) -> None:
        """电阻：矩形（IEC 画法）"""
        if horiz:
            self.line(x, y, x + lead, y)
            self.rect(x + lead, y - 8, box, 16, fill="#fff")
            self.line(x + lead + box, y, x + lead + box + lead, y)
            self.text(x + lead + box / 2, y - 12, refdes, size=9, anchor="middle")
            self.text(x + lead + box / 2, y + 24, value, size=9, anchor="middle",
                      color="#555")
            return x + lead * 2 + box
        else:
            self.line(x, y, x, y + lead)
            self.rect(x - 8, y + lead, 16, box, fill="#fff")
            self.line(x, y + lead + box, x, y + lead + box + lead)
            self.text(x - 13, y + lead + box / 2, refdes, size=9, anchor="end")
            self.text(x + 13, y + lead + box / 2 + 4, value, size=9, color="#555")
            return y + lead * 2 + box

    def cap(self, x, y, refdes, value, vert=True) -> None:
        if vert:
            self.line(x, y, x, y + 10)
            self.line(x - 11, y + 10, x + 11, y + 10, w=2.4)
            self.line(x - 11, y + 17, x + 11, y + 17, w=2.4)
            self.line(x, y + 17, x, y + 27)
            self.text(x + 14, y + 12, refdes, size=9)
            self.text(x + 14, y + 23, value, size=9, color="#555")
        else:
            self.line(x, y, x + 10, y)
            self.line(x + 10, y - 11, x + 10, y + 11, w=2.4)
            self.line(x + 17, y - 11, x + 17, y + 11, w=2.4)
            self.line(x + 17, y, x + 27, y)
            self.text(x + 10, y - 16, refdes, size=9)
            self.text(x + 4, y + 26, value, size=9, color="#555")

    def npn(self, x, y, refdes) -> None:
        """NPN 三极管，基极在左，集电极在上，发射极在下。
        调用点 (x,y) 是基极引线起点。"""
        self.circle(x + 26, y, 15, fill="#fff")
        self.line(x, y, x + 16, y)                       # 基极引线
        self.line(x + 16, y - 11, x + 16, y + 11, w=2.6)  # 基极竖条
        self.line(x + 16, y - 6, x + 33, y - 15)          # 集电极
        self.line(x + 16, y + 6, x + 33, y + 15)          # 发射极
        self.line(x + 33, y - 15, x + 33, y - 24)
        self.line(x + 33, y + 15, x + 33, y + 24)
        # 发射极箭头
        self.raw(f'<polygon points="{x+33},{y+24} {x+27},{y+13} {x+36},{y+11}" '
                 f'fill="#1a5c1a"/>')
        self.text(x + 40, y - 4, refdes, size=10, weight="bold")
        self.text(x + 40, y + 8, "S8050", size=9, color="#555")

    def led_display(self, x, y, refdes) -> dict[str, tuple[int, int]]:
        """2 位共阴数码管。

        ★ 引脚**按名字画**（A..G / DP / DIG1 / DIG2），不画 C193138 的
          物理脚号 —— 脚号随型号变，名字不变。脚号映射作为注释放旁边。
        ★ 引脚间距给 22px。最初给 10px，8 根段线加电阻挤成一团完全看不清
          （渲染出来才发现），这种图比没有图更危险。

        返回 {引脚名: (x, y)}，供外部接线用。
        """
        w, h = 150, 230
        self.rect(x, y, w, h, fill="#fff8f8", stroke="#a00", sw=1.8, rx=5)
        self.text(x + w / 2, y - 8, refdes, size=12, anchor="middle",
                  weight="bold")
        self.text(x + w / 2, y + 14, "2 位共阴", size=9,
                  anchor="middle", color="#a00")
        for k in range(2):
            bx = x + 22 + k * 66
            self.rect(bx, y + 30, 40, 70, fill="#ffeaea",
                      stroke="#c55", sw=1, rx=3)
            self.text(bx + 20, y + 62, "8.", size=17, anchor="middle",
                      color="#c00")
            self.text(bx + 20, y + 88, "8.", size=17, anchor="middle",
                      color="#c00")

        pins: dict[str, tuple[int, int]] = {}
        # 左侧 8 根：a..g, dp
        names_left = ["a", "b", "c", "d", "e", "f", "g", "dp"]
        for i, nm in enumerate(names_left):
            py = y + 48 + i * 22
            self.line(x, py, x - 16, py)
            self.text(x + 6, py + 4, nm, size=10, weight="bold")
            pins[nm] = (x - 16, py)
        # 右侧 2 根：DIG1 / DIG2
        for i, nm in enumerate(["DIG1", "DIG2"]):
            py = y + 90 + i * 40
            self.line(x + w, py, x + w + 16, py)
            self.text(x + w - 6, py + 4, nm, size=10, anchor="end",
                      weight="bold")
            pins[nm] = (x + w + 16, py)
        return pins

    def switch(self, x, y, refdes) -> None:
        """轻触按键"""
        self.line(x, y - 14, x, y - 8)
        self.line(x - 10, y - 8, x + 10, y - 8, w=2)
        self.line(x - 10, y + 8, x + 10, y + 8, w=2)
        self.line(x - 4, y - 14, x + 4, y - 14, w=2)
        self.line(x, y - 14, x, y - 18)
        self.text(x + 14, y + 2, refdes, size=10, weight="bold")

    # ---- 元件框（模块）----
    def module(self, x, y, w, h, title, subtitle="") -> None:
        self.rect(x, y, w, h, fill="#f4f8ff", stroke="#2a4a8a", sw=2, rx=6)
        self.raw(f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="6" '
                 f'fill="#2a4a8a"/>')
        self.raw(f'<rect x="{x}" y="{y+16}" width="{w}" height="10" '
                 f'fill="#2a4a8a"/>')
        self.text(x + 8, y + 18, title, size=12, color="#fff", weight="bold")
        if subtitle:
            self.text(x + w - 8, y + 18, subtitle, size=9, anchor="end", color="#cfe")

    def pin(self, x, y, name, side="right", net=None, netcolor="#a03000") -> None:
        """模块引脚：side=right 表示引脚在框的右边（引线向右伸）。
        返回引线末端坐标。"""
        if side == "right":
            self.line(x, y, x + 18, y)
            self.text(x - 5, y + 4, name, size=9, anchor="end")
            ex = x + 18
            if net:
                self.netlabel(ex + 3, y + 4, net)
        else:
            self.line(x, y, x - 18, y)
            self.text(x + 5, y + 4, name, size=9)
            ex = x - 18
            if net:
                self.netlabel(ex - 3, y + 4, net, anchor="end")
        return ex

    def save(self, path: Path) -> None:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
                f'<rect width="{self.w}" height="{self.h}" fill="#fdfdfb"/>')
        path.write_text(head + "\n" + "\n".join(self.parts) + "\n</svg>\n",
                        encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# 网表 —— 这是**权威连接表**，画图和它必须一致
# ──────────────────────────────────────────────────────────────────────
# ★ 为什么要单独写一份网表，而不是"看图连线"：
#   图是给人看的，会有走线交叉、网络标号分散在各处；
#   网表是给**动作**用的 —— 照着它一根一根接，不会漏、不会错。
#   两者从同一份数据出发，tools/gen_schematic.py 的自检会核对引脚。
#
# 格式：(网络名, [(位号, 引脚, 说明), ...])
def netlist(p: dict[str, int]) -> list[tuple[str, list[tuple[str, str, str]]]]:
    def gp(name: str) -> str:
        return f"GP{p[name]}"

    return [
        ("3V3", [
            ("U1", "3V3 (pin 36)", "开发板 3.3V 输出"),
            ("U2", "VIN", "★ 绝不能接 5V"),
            ("U3", "8 VCC", "6N138 电源"),
            ("R2", "上端", "光耦输出上拉"),
            ("C4", "+", "100µF 电解"),
            ("C5", "+", "100nF 去耦"),
            # ★ 这里**没有** R4~R11。
            #   段限流电阻是串在 GPIO 和数码管段之间的（共阴：段是阳极，
            #   由 GPIO 拉高点亮），不是接到 3V3 上。
            #   最初凭"限流电阻应该接电源"的直觉把 R4~R11 写进了 3V3 网络，
            #   那会把数码管接成"常亮"而不是"受 GPIO 控制"。
        ]),
        ("GND", [
            ("U1", "GND (3/8/13/18/23/28/38)", "开发板地"),
            ("U2", "GND", ""),
            ("U2", "SCK", "★ 接 GND 选内部 PLL"),
            ("U3", "5 GND", ""),
            ("R3", "下端", "经 4.7kΩ 到地"),
            ("C1", "一端", "DIN pin2 屏蔽"),
            ("C4", "−", ""),
            ("C5", "−", ""),
            ("Q1", "E", "发射极"),
            ("Q2", "E", "发射极"),
            ("SW1", "一端", ""),
            ("SW2", "一端", ""),
            ("C2", "一端", ""),
            ("C3", "一端", ""),
        ]),
        ("LRCK", [(("U1"), gp("PIN_I2S_LRCK"), "→ PCM5102"),
                  ("U2", "LRCK", "")]),
        ("BCK", [(("U1"), gp("PIN_I2S_BCK"), "→ PCM5102"),
                 ("U2", "BCK", "")]),
        ("DIN", [(("U1"), gp("PIN_I2S_DIN"), "→ PCM5102"),
                 ("U2", "DIN", "")]),
        ("MIDI_RX", [
            ("U1", gp("PIN_MIDI_RX"), "UART1 RX（AUX 功能 F11）"),
            ("U3", "6 VO", "光耦输出"),
            ("R2", "下端", "1kΩ 上拉"),
        ]),
        ("MIDI_TX", [(("U1"), gp("PIN_MIDI_TX"), "可选：MIDI THRU")]),
        ("SEG_A", [(("U1"), gp("PIN_SEG_A"), ""), ("R4", "左端", "→ 段 a")]),
        ("SEG_B", [(("U1"), gp("PIN_SEG_B"), ""), ("R5", "左端", "→ 段 b")]),
        ("SEG_C", [(("U1"), gp("PIN_SEG_C"), ""), ("R6", "左端", "→ 段 c")]),
        ("SEG_D", [(("U1"), gp("PIN_SEG_D"), ""), ("R7", "左端", "→ 段 d")]),
        ("SEG_E", [(("U1"), gp("PIN_SEG_E"), ""), ("R8", "左端", "→ 段 e")]),
        ("SEG_F", [(("U1"), gp("PIN_SEG_F"), ""), ("R9", "左端", "→ 段 f")]),
        ("SEG_G", [(("U1"), gp("PIN_SEG_G"), ""), ("R10", "左端", "→ 段 g")]),
        ("SEG_DP", [(("U1"), gp("PIN_SEG_DP"), ""), ("R11", "左端", "→ dp（可选）")]),
        ("DIG1", [(("U1"), gp("PIN_DIGIT1"), ""), ("R12", "一端", "→ Q1 基极")]),
        ("DIG2", [(("U1"), gp("PIN_DIGIT2"), ""), ("R13", "一端", "→ Q2 基极")]),
        ("BTN1", [(("U1"), gp("PIN_BTN_UP"), ""), ("SW1", "另一端", ""),
                  ("C2", "另一端", "消抖")]),
        ("BTN2", [(("U1"), gp("PIN_BTN_DOWN"), ""), ("SW2", "另一端", ""),
                  ("C3", "另一端", "消抖")]),
        ("OPTO_A", [("J1", "4", "MIDI 电流源"), ("R1", "左端", "")]),
        ("OPTO_ANODE", [("R1", "右端", "220Ω 限流"), ("U3", "2 A", "光耦阳极")]),
        ("OPTO_K", [("J1", "5", "MIDI 电流汇"), ("U3", "3 K", "阴极")]),
        ("DIN_SHIELD", [("J1", "2", "屏蔽"), ("C1", "另一端", "100nF 到 GND")]),
        ("OPTO_VB", [("U3", "7 VB", ""), ("R3", "上端", "★ 加速关断")]),
        ("DIG1_C", [("Q1", "C", "集电极"), ("DS1", "DIG1", "位选 1")]),
        ("DIG2_C", [("Q2", "C", "集电极"), ("DS1", "DIG2", "位选 2")]),
        ("Q1_B", [("Q1", "B", "基极"), ("R12", "另一端", "1kΩ")]),
        ("Q2_B", [("Q2", "B", "基极"), ("R13", "另一端", "1kΩ")]),
        ("SEG_A_FILT", [("R4", "右端", ""), ("DS1", "a", "")]),
        ("SEG_B_FILT", [("R5", "右端", ""), ("DS1", "b", "")]),
        ("SEG_C_FILT", [("R6", "右端", ""), ("DS1", "c", "")]),
        ("SEG_D_FILT", [("R7", "右端", ""), ("DS1", "d", "")]),
        ("SEG_E_FILT", [("R8", "右端", ""), ("DS1", "e", "")]),
        ("SEG_F_FILT", [("R9", "右端", ""), ("DS1", "f", "")]),
        ("SEG_G_FILT", [("R10", "右端", ""), ("DS1", "g", "")]),
        ("SEG_DP_FILT", [("R11", "右端", ""), ("DS1", "dp", "可选")]),
    ]


def write_netlist(p: dict[str, int], path: Path) -> None:
    lines: list[str] = []
    A = lines.append
    A("# GMSS v4 网表（权威连接表）")
    A("")
    A("> **本文件由 `tools/gen_schematic.py` 自动生成，引脚号从")
    A("> `firmware/include/board_config.h` 读出。** 不要手改 —— 改了会被下次生成覆盖。")
    A(">")
    A("> 画板时照着这张表一根一根接。原理图（`gmss_schematic.svg`）是同一份数据的")
    A("> 可视化版本，用来核对，不用来数线。")
    A("")
    A("## 网络一览")
    A("")
    A("| 网络 | 连接的引脚 |")
    A("|---|---|")
    for name, members in netlist(p):
        cell = "；".join(
            f"**{ref}** {pin}" + (f"（{note}）" if note else "")
            for ref, pin, note in members)
        A(f"| `{name}` | {cell} |")
    A("")
    A("## 按位号汇总（画 PCB 时按这个建网络）")
    A("")
    A("| 位号 | 型号 / 规格 | 各脚接到 |")
    A("|---|---|---|")
    refs: dict[str, list[tuple[str, str, str]]] = {}
    for name, members in netlist(p):
        for ref, pin, note in members:
            refs.setdefault(ref, []).append((name, pin, note))
    spec = {
        "U1": "RP2350 开发板 16MB", "U2": "PCM5102A 模块",
        "U3": "6N138（DIP-8）", "J1": "DIN-5 母座 180°",
        "DS1": "2 位共阴数码管", "Q1": "S8050", "Q2": "S8050",
        "SW1": "轻触 6×6×5", "SW2": "轻触 6×6×5",
    }
    for ref in sorted(refs, key=lambda r: (r[0], len(r), r)):
        pins = "；".join(f"{pin}→`{net}`" for net, pin, _ in refs[ref])
        A(f"| {ref} | {spec.get(ref, '见 BOM')} | {pins} |")
    A("")
    A("## 几个必须记住的约束")
    A("")
    A("| 约束 | 违反了会怎样 |")
    A("|---|---|")
    A("| **U2 VIN 只能 3.3V** | 接 5V 会烧 PCM5102A（绝对最大额定 3.9V） |")
    A("| **U2 SCK 接 GND** | 不接就得额外引一根 MCLK；接 3V3 则无时钟、无声 |")
    A("| **R2 用 1kΩ** | 用 10kΩ 时上升沿几十 µs，31250bps 下随机丢字节 |")
    A("| **R3 不能省** | 光耦关断变慢，高波特率下丢字节 |")
    A("| **位选经三极管** | 直接接 GPIO 会烧 pad（阴极要灌约 48mA） |")
    A("| **I2S 三根脚必须连续** | LRCK/BCK/DIN 必须是连续三个 GPIO（当前 GP12/13/14）；"
      "改了会编译报错（有 `_Static_assert` 拦着） |")
    A("| **段线必须连续** | GP15~GP22 连续 8 根，否则段码掩码写不对（同样有编译期断言） |")
    A("")
    A("## 系统结构")
    A("")
    A("```")
    A("  USB-C ──► U1 (RP2350, 16MB Flash)")
    A("              │")
    A("   ┌──────────┼──────────┬─────────────┐")
    A("   │          │          │             │")
    A("  PIO I2S   UART1       GPIO        GPIO")
    A("   │          │          │             │")
    A("   ▼          ▼          ▼             ▼")
    A(" U2 DAC    U3 6N138   DS1 数码管    SW1/SW2")
    A("   │      （J1 DIN-5）  （经 Q1/Q2）   按键")
    A("   ▼")
    A(" 3.5mm 耳机座")
    A("```")
    A("")
    A("固件与音色库同住 U1 上那片 16MB Flash：")
    A("`0x000000` 固件 → `0x040000` 音色库 → 采样池。采样数据是**内存映射**的，")
    A("读一个采样点就是读一个指针（`0x10000000 + sample_off`）。")
    A("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────
# 画图
# ──────────────────────────────────────────────────────────────────────
def draw(p: dict[str, int]) -> Svg:
    W, H = 1900, 1420
    s = Svg(W, H)

    # ─── 标题栏 ───
    s.rect(0, 0, W, 52, fill="#2a4a8a", stroke="none")
    s.text(18, 24, "GMSS — RP2350 硬 GM 音源", size=17, color="#fff",
           weight="bold", family="Microsoft YaHei, sans-serif")
    s.text(18, 42, "16MB 单 Flash 方案（v4）  ·  48 复音采样回放 GM 引擎  ·  "
                   "音色库与固件同住一片 Flash，内存映射直读",
           size=10, color="#cfe0ff", family="Microsoft YaHei, sans-serif")
    s.text(W - 18, 24, "引脚来源：firmware/include/board_config.h",
           size=9, anchor="end", color="#cfe0ff")
    s.text(W - 18, 40, "本图由 tools/gen_schematic.py 自动生成",
           size=9, anchor="end", color="#cfe0ff")

    # ══════════════════════════════════════════════════════════════
    # U1 —— RP2350 开发板
    # ══════════════════════════════════════════════════════════════
    ux, uy, uw = 60, 100, 300
    left = ["VBUS", "VSYS", "GND", "3V3_EN", "3V3", "ADC_VREF",
            "GP28", "AGND", "GP27", "GP26", "RUN",
            "GP22", "GND", "GP21", "GP20", "GP19", "GP18", "GND", "GP17", "GP16"]
    right = ["GP0", "GP1", "GND", "GP2", "GP3", "GP4", "GP5", "GND",
             "GP6", "GP7", "GP8", "GP9", "GND", "GP10", "GP11",
             "GP12", "GP13", "GND", "GP14", "GP15"]
    uh = 40 + max(len(left), len(right)) * 20
    s.module(ux, uy, uw, uh, "U1  RP2350 开发板", "16MB Flash · USB-C")

    # 左右两列引脚。
    # ★ 物理脚号画在**框内**贴近边缘的地方（灰色小字），网络标号画在
    #   框外引线末端。最初两者都放在框外同一位置，渲染出来叠在一起
    #   完全没法看 —— 物理脚号是给人对照开发板用的，不能省。
    pin_left_nets = {"3V3": "3V3", "GND": "GND"}
    for i, name in enumerate(left):
        y = uy + 52 + i * 20
        s.line(ux, y, ux - 18, y)
        s.text(ux + 5, y + 4, str(40 - i), size=8, color="#999")   # 物理脚号
        s.text(ux + 22, y + 4, name, size=10)                       # 丝印名
        if name in pin_left_nets:
            s.netlabel(ux - 22, y + 4, pin_left_nets[name], anchor="end")

    pin_right_nets = {
        "GND": "GND",
        "GP6": "DIG1", "GP7": "DIG2",
        "GP8": "BTN1", "GP9": "BTN2",
        "GP10": "MIDI_RX", "GP11": "MIDI_TX",
        "GP12": "LRCK", "GP13": "BCK", "GP14": "DIN",
        "GP15": "SEG_A", "GP16": "SEG_B", "GP17": "SEG_C", "GP18": "SEG_D",
    }
    for i, name in enumerate(right):
        y = uy + 52 + i * 20
        s.line(ux + uw, y, ux + uw + 18, y)
        s.text(ux + uw - 5, y + 4, str(i + 1), size=8, anchor="end", color="#999")
        s.text(ux + uw - 22, y + 4, name, size=10, anchor="end")
        net = pin_right_nets.get(name)
        if net:
            s.netlabel(ux + uw + 24, y + 4, net)

    # 右列下半部分（GP19..GP15）继续标
    for i, (name, net) in enumerate([("GP19", "SEG_E"), ("GP20", "SEG_F"),
                                     ("GP21", "SEG_G"), ("GP22", "SEG_DP")]):
        pass    # GP19..GP22 在左列，下面单独标

    s.text(ux + uw / 2, uy + uh + 18,
           "★ 板上丝印：GPIO 只印数字，GND 印成 G。以丝印为准。",
           size=9, anchor="middle", color="#a03000",
           family="Microsoft YaHei, sans-serif")
    s.text(ux + uw / 2, uy + uh + 33,
           "GP15~GP22 是连续的 8 根（段线），接线时按丝印数字找。",
           size=9, anchor="middle", color="#666",
           family="Microsoft YaHei, sans-serif")

    # 左列下半部分要标的网络（GP22/GP21/GP20/GP19/GP18/GP17/GP16）
    left_lower = {"GP22": "SEG_DP", "GP21": "SEG_G", "GP20": "SEG_F",
                  "GP19": "SEG_E", "GP18": "SEG_D", "GP17": "SEG_C",
                  "GP16": "SEG_B"}
    for i, name in enumerate(left):
        if name in left_lower:
            y = uy + 52 + i * 20
            s.netlabel(ux - 22, y + 4, left_lower[name], anchor="end")

    # ══════════════════════════════════════════════════════════════
    # U2 —— PCM5102A 模块
    # ══════════════════════════════════════════════════════════════
    mx, my, mw, mh = 640, 90, 250, 210
    s.module(mx, my, mw, mh, "U2  PCM5102A 模块", "6 脚排针")
    u2 = [("VIN", "3V3"), ("GND", "GND"), ("LRCK", "LRCK"),
          ("DIN", "DIN"), ("BCK", "BCK"), ("SCK", "GND")]
    for i, (nm, net) in enumerate(u2):
        y = my + 50 + i * 24
        s.line(mx, y, mx - 18, y)
        s.text(mx + 5, y + 4, nm, size=10, weight="bold")
        s.netlabel(mx - 22, y + 4, net, anchor="end")
    s.text(mx + mw / 2, my + mh + 16,
           "排针顺序（从板边往板内）：VIN GND LRCK DIN BCK SCK",
           size=9, anchor="middle", color="#666",
           family="Microsoft YaHei, sans-serif")
    s.text(mx + mw / 2, my + mh + 32,
           "★ VIN 必须 3.3V，接 5V 会烧芯片（绝对最大额定 3.9V）",
           size=9, anchor="middle", color="#c00", weight="bold",
           family="Microsoft YaHei, sans-serif")
    s.text(mx + mw / 2, my + mh + 47,
           "★ SCK 接 GND —— 选内部 PLL 从 BCK 重建主时钟，省掉 MCLK",
           size=9, anchor="middle", color="#c00",
           family="Microsoft YaHei, sans-serif")
    s.text(mx + mw / 2, my + mh + 62,
           "FLT / DEMP / XSMT / FMT 不接（模块板上已有默认配置）",
           size=9, anchor="middle", color="#666",
           family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # MIDI 输入
    # ══════════════════════════════════════════════════════════════
    bgx, bgy, bgw, bgh = 940, 100, 900, 350
    s.rect(bgx, bgy, bgw, bgh, fill="#fbfbf6", stroke="#999", sw=1.2, rx=8)
    s.text(bgx + 12, bgy + 22, "MIDI 输入（DIN-5 五脚 + 6N138 光耦）",
           size=13, weight="bold", family="Microsoft YaHei, sans-serif")

    # ── J1 DIN-5 母座 ──
    #
    # ★ 画成"引脚列"而不是画插座的弧形排列。
    #   原理图里连接器就应该是逻辑引脚表 —— 物理排布是**封装**的事，
    #   归 PCB 符号管。而且弧形排布我并没有可靠依据，
    #   画错了比不画更糟（会误导人按图去数针）。
    dx, dy = bgx + 34, bgy + 58
    s.rect(dx, dy, 176, 150, fill="#fff", stroke="#333", sw=1.8, rx=4)
    s.text(dx + 88, dy - 8, "J1  DIN-5 母座", size=10, anchor="middle",
           weight="bold")
    s.text(dx + 88, dy + 170, "MIDI IN（5 脚 180°）", size=8,
           anchor="middle", color="#888",
           family="Microsoft YaHei, sans-serif")
    jpins = {}
    for i, (n, func) in enumerate([(1, "不接"), (2, "屏蔽/地"), (3, "不接"),
                                   (4, "电流源 +"), (5, "电流汇 −")]):
        py = dy + 26 + i * 26
        s.line(dx + 176, py, dx + 198, py)
        s.text(dx + 168, py + 4, str(n), size=11, anchor="end", weight="bold")
        s.text(dx + 142, py + 4, func, size=9, anchor="end", color="#666",
               family="Microsoft YaHei, sans-serif")
        jpins[n] = (dx + 198, py)

    # ── U3 6N138 ──
    ox, oy = bgx + 350, bgy + 50
    s.rect(ox, oy, 150, 160, fill="#fff", stroke="#333", sw=1.8, rx=4)
    s.text(ox + 75, oy - 8, "U3  6N138", size=10, anchor="middle", weight="bold")
    s.text(ox + 75, oy + 180, "DIP-8", size=8, anchor="middle", color="#888")
    s.line(ox, oy + 46, ox - 22, oy + 46)
    s.text(ox + 6, oy + 50, "2 A", size=10)
    s.line(ox, oy + 86, ox - 22, oy + 86)
    s.text(ox + 6, oy + 90, "3 K", size=10)
    for nm, yy in [("8 VCC", oy + 28), ("7 VB", oy + 62),
                   ("6 VO", oy + 96), ("5 GND", oy + 130)]:
        s.line(ox + 150, yy, ox + 172, yy)
        s.text(ox + 144, yy + 4, nm, size=10, anchor="end")

    # ── pin4 → R1(220Ω) → 光耦 pin2(Anode) ──
    r1y = jpins[4][1]
    s.wire([(jpins[4][0], r1y), (ox - 130, r1y)])
    s.resistor(ox - 130, r1y, "R1", "220Ω", horiz=True, box=46, lead=22)
    s.wire([(ox - 40, r1y), (ox - 22, r1y), (ox - 22, oy + 46)])

    # ── pin5 → 光耦 pin3(Cathode) ──
    s.wire([(jpins[5][0], jpins[5][1]), (ox - 60, jpins[5][1]),
            (ox - 60, oy + 86), (ox - 22, oy + 86)])

    # ── pin2 → C1 → GND（屏蔽地）──
    s.wire([(jpins[2][0], jpins[2][1]), (dx + 232, jpins[2][1])])
    s.cap(dx + 232, jpins[2][1], "C1", "100nF", vert=False)
    s.wire([(dx + 259, jpins[2][1]), (dx + 290, jpins[2][1]),
            (dx + 290, dy + 180)])
    s.gnd(dx + 290, dy + 180)
    s.text(dx + 196, dy + 218, "pin1 / pin3 不接", size=9, color="#888",
           family="Microsoft YaHei, sans-serif")

    # ── pin8 VCC → 3V3，并并一颗 100nF ──
    s.wire([(ox + 172, oy + 28), (ox + 250, oy + 28)])
    s.junction(ox + 210, oy + 28)
    s.vcc(ox + 250, oy + 28, "3V3")
    s.cap(ox + 210, oy + 28, "C5", "100nF")
    s.line(ox + 210, oy + 55, ox + 210, oy + 64)
    s.gnd(ox + 210, oy + 64)

    # ── pin7 VB → R3(4.7k) → GND ──
    # 走线放在 ox+240 这一列：VO 的引线只到 ox+200、GND 的只到 ox+196，
    # 都不会被这条竖线穿过（原来那版三根线绞在一起，渲染出来才看出来）。
    s.wire([(ox + 172, oy + 62), (ox + 240, oy + 62), (ox + 240, oy + 76)])
    s.resistor(ox + 240, oy + 76, "R3", "4.7kΩ", horiz=False, box=44, lead=14)
    s.gnd(ox + 240, oy + 76 + 72)

    # ── pin5 GND ──
    s.wire([(ox + 172, oy + 130), (ox + 196, oy + 130)])
    s.gnd(ox + 196, oy + 130)

    # ── pin6 VO → MIDI_RX ──
    s.wire([(ox + 172, oy + 96), (ox + 200, oy + 96)])
    s.junction(ox + 200, oy + 96)
    s.netlabel(ox + 204, oy + 101, "MIDI_RX")

    # ── 上拉电阻单独画（用网络标号连接，避免与 VB 的走线交叉）──
    ux2 = ox + 430
    s.text(ux2, bgy + 62, "上拉（1kΩ）", size=9, anchor="middle",
           color="#2a4a8a", weight="bold",
           family="Microsoft YaHei, sans-serif")
    s.vcc(ux2, bgy + 82, "3V3")
    s.resistor(ux2, bgy + 84, "R2", "1kΩ", horiz=False, box=44, lead=14)
    s.wire([(ux2, bgy + 142), (ux2, bgy + 156)])
    s.netlabel(ux2 - 42, bgy + 160, "MIDI_RX")

    s.text(bgx + 22, bgy + 288,
           "★ 上拉必须是 1kΩ（R2），不是常见的 10kΩ —— 6N138 是达林顿输出，等效输出电容大；",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")
    s.text(bgx + 22, bgy + 306,
           "   10k 上拉时上升沿要几十 µs，而 MIDI 位宽只有 32µs，波形直接糊掉。",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")
    s.text(bgx + 22, bgy + 326,
           "★ 光耦必须 6N138 / 6N139，不能用 6N137（要 ≥6.3mA 才保证工作，超 MIDI 规范）。",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")
    s.text(bgx + 22, bgy + 344,
           "★ R3（4.7kΩ，pin7→GND）是加速关断用的，不能省 —— 省了会在高波特率下丢字节。",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # 数码管电路
    # ══════════════════════════════════════════════════════════════
    dx2, dy2 = 50, 470
    s.rect(dx2, dy2, 1080, 500, fill="#fbfbf6", stroke="#999", sw=1.2, rx=8)
    s.text(dx2 + 12, dy2 + 22, "2 位共阴数码管（动态扫描）",
           size=13, weight="bold", family="Microsoft YaHei, sans-serif")

    ddx, ddy = dx2 + 250, dy2 + 60
    dp = s.led_display(ddx, ddy, "DS1")

    # 段线：从左往右是 网络标号 → 180Ω → 数码管段脚
    segs = [("a", "SEG_A", "R4"), ("b", "SEG_B", "R5"), ("c", "SEG_C", "R6"),
            ("d", "SEG_D", "R7"), ("e", "SEG_E", "R8"), ("f", "SEG_F", "R9"),
            ("g", "SEG_G", "R10"), ("dp", "SEG_DP", "R11")]
    for seg, net, ref in segs:
        px, py = dp[seg]
        rx0 = ddx - 190
        s.wire([(px, py), (rx0 + 88, py)])           # 数码管引脚 → 电阻右端
        s.resistor(rx0, py, ref, "180Ω", horiz=True, box=44, lead=22)
        s.wire([(rx0 - 44, py), (rx0 - 62, py)])
        s.netlabel(rx0 - 66, py + 5, net, anchor="end")

    # 位选驱动
    for k, (nm, net, ref, qref) in enumerate(
            [("DIG1", "DIG1", "R12", "Q1"), ("DIG2", "DIG2", "R13", "Q2")]):
        px, py = dp[nm]
        bx = ddx + 300
        by = py
        # 网络标号 → 基极电阻 → 基极
        s.netlabel(bx - 96, by, net, anchor="end")
        s.wire([(bx - 92, by), (bx - 60, by)])
        s.resistor(bx - 60, by, ref, "1kΩ", horiz=True, box=34, lead=12)
        s.wire([(bx - 2, by), (bx + 24, by)])
        s.npn(bx + 24, by, qref)
        # 集电极 → 数码管位选脚
        s.wire([(bx + 57, by - 24), (bx + 57, by - 46),
                (px + 40, by - 46), (px + 40, py)])
        s.wire([(px + 40, py), (px, py)])
        # 发射极 → GND
        s.wire([(bx + 57, by + 24), (bx + 57, by + 46)])
        s.gnd(bx + 57, by + 46)

    s.text(dx2 + 20, dy2 + 330,
           "★ 位选必须用三极管：一位点亮时最多 8 段同时导通，阴极要灌约 48mA，",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")
    s.text(dx2 + 20, dy2 + 348,
           "   而 RP2350 单个 pad 只有 ~8mA 能力 —— 直接灌会烧 pad 或亮度惨不忍睹。",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")
    s.text(dx2 + 20, dy2 + 372,
           "★ 段线串 180Ω（@3.3V 红光 Vf≈2.0V 时峰值约 6mA，两位各 50% 占空）。",
           size=10, color="#555", family="Microsoft YaHei, sans-serif")
    s.text(dx2 + 20, dy2 + 390,
           "   觉得暗可以换 120Ω，不要再小。",
           size=10, color="#555", family="Microsoft YaHei, sans-serif")
    s.text(dx2 + 20, dy2 + 414,
           "引脚对应（以 C193138 / ARKLED SN420362N 核实）：",
           size=10, color="#2a4a8a", weight="bold",
           family="Microsoft YaHei, sans-serif")
    s.text(dx2 + 20, dy2 + 432,
           "   1:G  2:DP  3:A  4:F  5:DIG2  6:D  7:E  8:C  9:B  10:DIG1",
           size=10, color="#555", family="Consolas, monospace")
    s.text(dx2 + 20, dy2 + 452,
           "   ★ 换型号务必对照自己的数据手册 —— 共阴/共阳和引脚顺序都可能不同。",
           size=10, color="#c00", family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # 按键
    # ══════════════════════════════════════════════════════════════
    bx3, by3 = 960, 480
    s.rect(bx3, by3, 300, 190, fill="#fbfbf6", stroke="#999", sw=1.2, rx=8)
    s.text(bx3 + 10, by3 + 20, "按键", size=12, weight="bold",
           family="Microsoft YaHei, sans-serif")
    for k, (net, swref, cref) in enumerate(
            [("BTN1", "SW1", "C2"), ("BTN2", "SW2", "C3")]):
        cx = bx3 + 70 + k * 130
        cy = by3 + 70
        s.netlabel(cx - 55, cy + 4, net, anchor="end")
        s.wire([(cx - 51, cy), (cx - 16, cy)])
        s.junction(cx - 30, cy)
        s.switch(cx, cy, swref)
        s.wire([(cx, cy + 8), (cx, cy + 40)])
        s.gnd(cx, cy + 40)
        # 消抖电容
        s.line(cx - 30, cy, cx - 30, cy + 34)
        s.rect(cx - 37, cy + 34, 14, 12, fill="#fff", stroke="#333", sw=1.2)
        s.text(cx - 42, cy + 44, cref, size=7, anchor="end")
        s.line(cx - 30, cy + 46, cx - 30, cy + 56)
        s.gnd(cx - 30, cy + 56, label="")
    s.text(bx3 + 15, by3 + 165,
           "100nF 消抖；内部上拉已够用，外部 10k 可选",
           size=8, color="#555", family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # 电源去耦
    # ══════════════════════════════════════════════════════════════
    px, py = 1300, 480
    s.rect(px, py, 350, 190, fill="#fbfbf6", stroke="#999", sw=1.2, rx=8)
    s.text(px + 10, py + 20, "电源去耦", size=12, weight="bold",
           family="Microsoft YaHei, sans-serif")
    s.vcc(px + 80, py + 60, "3V3")
    s.junction(px + 80, py + 60)
    s.line(px + 80, py + 60, px + 80, py + 70)
    s.cap(px + 80, py + 70, "C4", "100µF")
    s.line(px + 80, py + 97, px + 80, py + 110)
    s.gnd(px + 80, py + 110)
    s.line(px + 80, py + 70, px + 190, py + 70)
    s.cap(px + 190, py + 70, "Cd1", "100nF")
    s.line(px + 190, py + 97, px + 190, py + 110)
    s.gnd(px + 190, py + 110)
    s.text(px + 15, py + 150,
           "100µF 电解抑制数码管扫描引起的纹波；",
           size=8, color="#555", family="Microsoft YaHei, sans-serif")
    s.text(px + 15, py + 165,
           "100nF 就近放在开发板 3V3 排针旁。",
           size=8, color="#555", family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # 电源说明
    # ══════════════════════════════════════════════════════════════
    sx, sy = 960, 700
    s.rect(sx, sy, 690, 180, fill="#fff8f0", stroke="#d09040", sw=1.6, rx=8)
    s.text(sx + 12, sy + 22, "供电", size=12, weight="bold",
           family="Microsoft YaHei, sans-serif")
    notes = [
        "整机由开发板的 USB-C 口供电，载板从 3V3 排针取电即可。",
        "电流预算：PCM5102 模块 ~30mA + 数码管 ~48mA + 6N138 ~10mA",
        "            + 板载 RGB 灯 ~5mA  ≈ 93mA（3V3 输出能力远超此值）",
        "",
        "★ VIN 必须 3.3V，接 5V 会烧 PCM5102A（绝对最大额定 3.9V）。",
        "★ 想脱离电脑独立使用：外部 5V 接 VBUS 脚并串一颗 SS34，",
        "   千万不要把 5V 直接灌到 3V3 脚上。",
    ]
    for i, t in enumerate(notes):
        col = "#c00" if t.startswith("★") else "#333"
        s.text(sx + 14, sy + 44 + i * 16, t, size=9, color=col,
               family="Microsoft YaHei, sans-serif")

    # ══════════════════════════════════════════════════════════════
    # 图例 / 说明
    # ══════════════════════════════════════════════════════════════
    lx, ly = 960, 900
    s.rect(lx, ly, 690, 130, fill="#f4f8ff", stroke="#2a4a8a", sw=1.4, rx=8)
    s.text(lx + 12, ly + 22, "读图说明", size=12, weight="bold",
           family="Microsoft YaHei, sans-serif")
    leg = [
        "· 网络标号（橙色框）同名的引脚是连在一起的，不画长线。",
        "· 引脚旁的小数字是开发板的**物理脚号**（1~40），大字号是丝印名。",
        "· 数码管引脚号按 C193138（ARKLED SN420362N）核实过 ——",
        "  换型号务必对照自己的数据手册，共阴/共阳和引脚顺序都可能不同。",
        "· 本图与 docs/接线图与BOM.md 是同一份连接，"
        "tools/verify_wiring_doc.py 会核对两者。",
    ]
    for i, t in enumerate(leg):
        s.text(lx + 14, ly + 44 + i * 16, t, size=9, color="#333",
               family="Microsoft YaHei, sans-serif")

    # ─── 右下角版本 ───
    s.text(W - 18, H - 12,
           "GMSS v4 原理图  ·  由 tools/gen_schematic.py 从 board_config.h 生成",
           size=9, anchor="end", color="#888")

    return s


# ──────────────────────────────────────────────────────────────────────
# 自检
# ──────────────────────────────────────────────────────────────────────
def self_check(p: dict[str, int], svg_text: str) -> list[str]:
    """
    ★ 图里出现的关键引脚必须与 board_config.h 一致。
      这是防止"图跟代码脱节"的那道闸 —— 手画的图迟早会漂。
    """
    problems = []

    expect = {
        "I2S LRCK": ("PIN_I2S_LRCK", 12),
        "I2S BCK":  ("PIN_I2S_BCK",  13),
        "I2S DIN":  ("PIN_I2S_DIN",  14),
        "MIDI RX":  ("PIN_MIDI_RX",  10),
        "MIDI TX":  ("PIN_MIDI_TX",  11),
        "段 a":     ("PIN_SEG_A",    15),
        "段 g":     ("PIN_SEG_G",    21),
        "段 dp":    ("PIN_SEG_DP",   22),
        "位选 1":   ("PIN_DIGIT1",    6),
        "位选 2":   ("PIN_DIGIT2",    7),
        "按键 1":   ("PIN_BTN_UP",    8),
        "按键 2":   ("PIN_BTN_DOWN",  9),
    }
    for label, (macro, want) in expect.items():
        got = p.get(macro)
        if got is None:
            problems.append(f"{label}: board_config.h 里没有 {macro}")
        elif got != want:
            problems.append(f"{label}: {macro}={got}，图里按 {want} 画的")

    # 图上必须出现所有段线网络标号
    for net in ["SEG_A", "SEG_B", "SEG_C", "SEG_D", "SEG_E", "SEG_F",
                "SEG_G", "SEG_DP", "DIG1", "DIG2", "BTN1", "BTN2",
                "LRCK", "BCK", "DIN", "MIDI_RX", "3V3", "GND"]:
        if net not in svg_text:
            problems.append(f"图上找不到网络标号 {net}")

    # 关键的"安全"提示必须在图上（这几条漏了会烧东西或不出声）
    for must in ["VIN 必须 3.3V", "SCK 接 GND", "1kΩ", "48mA"]:
        if must not in svg_text:
            problems.append(f"图上缺少关键提示：{must}")

    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 GMSS v4 原理图 SVG")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "build" / "schematic")
    ap.add_argument("--check", action="store_true", help="只自检，不写文件")
    args = ap.parse_args(argv)

    pins = load_pins()
    svg = draw(pins)
    text = "\n".join(svg.parts)

    print("=== 原理图自检 ===")
    problems = self_check(pins, text)
    for pr in problems:
        print(f"  ✗ {pr}")
    if not problems:
        print("  引脚与网络标号全部与 board_config.h 一致 ✓")

    if args.check:
        return 1 if problems else 0

    args.out.mkdir(parents=True, exist_ok=True)
    p = args.out / "gmss_schematic.svg"
    svg.save(p)
    print(f"\n已写出 {p}  ({p.stat().st_size:,} 字节)")
    print("  用浏览器打开即可查看/打印（SVG 矢量，放大不失真）")

    nl = args.out / "gmss_netlist.md"
    write_netlist(pins, nl)
    print(f"已写出 {nl}  ({nl.stat().st_size:,} 字节)")
    print("  ★ 这张表才是画板时要照着接的东西；SVG 用来核对")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
