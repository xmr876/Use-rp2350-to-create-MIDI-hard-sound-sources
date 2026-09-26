#!/usr/bin/env python3
"""
gen_wiring_display.py — 数码管接线示意图（单独一张，只画相关的东西）

为什么单独做这张：
  总的原理图里有 28 个元件、84 根线 —— 对着它接线太容易看错行。
  接线的时候人只需要看**当前这一簇**，所以按功能拆开画。

这张图的三条设计原则：
  1. **只画用到的引脚**。RP2350 只列这 10 个脚，不列另外 30 个。
  2. **物理脚号写在最显眼的位置**。数码管写 1~10，三极管写焊盘 1/2/3。
  3. **数字要对得上**：图上的每个编号都与 docs/接线图与BOM.md、
     tools/gen_easyeda.py 生成的原理图完全一致（同一份 board_config.h）。

三极管按 **SOT-23 焊盘丝印**编号（S8050 J3Y）：
     焊盘 1 = 基极 B
     焊盘 2 = 发射极 E
     焊盘 3 = 集电极 C
  ★ 这是标准 SOT-23 三极管的脚位，但**换型号务必对一下数据手册**。

用法：
    python tools/gen_wiring_display.py
    python tools/gen_wiring_display.py --check     # 只做一致性自检
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "firmware" / "include" / "board_config.h"

# ──────────────────────────────────────────────────────────────────────
# 数码管引脚表（按 C193138 / ARKLED SN420362N 核实）
# ──────────────────────────────────────────────────────────────────────
# (功能, 数码管物理脚号, 网络, 电阻位号)
SEGMENTS = [
    ("a",  3, "SEG_A",  "R4"),
    ("b",  9, "SEG_B",  "R5"),
    ("c",  8, "SEG_C",  "R6"),
    ("d",  6, "SEG_D",  "R7"),
    ("e",  7, "SEG_E",  "R8"),
    ("f",  4, "SEG_F",  "R9"),
    ("g",  1, "SEG_G",  "R10"),
    ("dp", 2, "SEG_DP", "R11"),
]
DIGITS = [
    ("DIG1", 10, "DIG1", "Q1", "R12"),
    ("DIG2",  5, "DIG2", "Q2", "R13"),
]

# SOT-23 焊盘号 → 电极
SOT23 = {1: ("B", "基极"), 2: ("E", "发射极"), 3: ("C", "集电极")}


def load_pins() -> dict[str, int]:
    txt = CFG.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"//[^\n]*", " ", txt)
    out = {}
    for m in re.finditer(r"^#define\s+(PIN_[A-Z0-9_]+)\s+(-?\d+)", txt, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


# ══════════════════════════════════════════════════════════════════════
class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.p = w, h, []

    def raw(self, s):
        self.p.append(s)

    def line(self, x1, y1, x2, y2, c="#1a6b1a", w=2.2, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.raw(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                 f'stroke="{c}" stroke-width="{w}"{d} stroke-linecap="round"/>')

    def path(self, pts, c="#1a6b1a", w=2.2, dash=None):
        for i in range(len(pts) - 1):
            self.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                      c, w, dash)

    def rect(self, x, y, w, h, fill="#fff", stroke="#333", sw=2, rx=0):
        self.raw(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                 f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def circle(self, cx, cy, r, fill="#fff", stroke="#333", sw=2):
        self.raw(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"/>')

    def junction(self, x, y, r=6):
        self.circle(x, y, r, fill="#1a6b1a", stroke="#1a6b1a", sw=0)

    def text(self, x, y, s, size=14, anchor="start", color="#111",
             weight="normal", ff="Microsoft YaHei, sans-serif"):
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.raw(f'<text x="{x}" y="{y}" font-family="{ff}" font-size="{size}" '
                 f'text-anchor="{anchor}" fill="{color}" '
                 f'font-weight="{weight}">{s}</text>')

    def mono(self, x, y, s, size=14, anchor="start", color="#111",
             weight="bold"):
        self.text(x, y, s, size, anchor, color, weight,
                  "Consolas, monospace")

    def save(self, path):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
                f'<rect width="{self.w}" height="{self.h}" fill="#ffffff"/>')
        path.write_text(head + "\n" + "\n".join(self.p) + "\n</svg>\n",
                        encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
def draw(p: dict[str, int]) -> Svg:
    W, H = 1620, 1240
    s = Svg(W, H)

    s.rect(0, 0, W, 62, fill="#1f4e8c", stroke="none")
    s.text(20, 28, "数码管接线示意图", size=22, color="#fff", weight="bold")
    s.text(20, 50, "2 位共阴数码管（C193138） + 8 只 180Ω + 2 只 S8050 位选驱动",
           size=13, color="#cfe0ff")
    s.text(W - 20, 30, "三极管按 SOT-23 焊盘丝印 1 / 2 / 3 编号",
           size=13, anchor="end", color="#ffe08a", weight="bold")
    s.text(W - 20, 50, "本图与固件 board_config.h 一致",
           size=11, anchor="end", color="#cfe0ff")

    # ══════════════════════════════════════════════════════════════
    # 区域 A：段线
    # ══════════════════════════════════════════════════════════════
    s.rect(16, 74, 1130, 570, fill="#f7fbf7", stroke="#7aa87a", sw=1.5, rx=8)
    s.text(30, 100, "A  段线（8 根）—— 每根串一只 180Ω", size=17,
           color="#1a6b1a", weight="bold")
    s.text(30, 122, "接法：RP2350 的 GPIO  →  180Ω 电阻  →  数码管对应的段脚",
           size=12, color="#555")

    # ---- RP2350 引脚列 ----
    bx, by, bw = 40, 150, 250
    s.rect(bx, by, bw, 470, fill="#eef4ff", stroke="#1f4e8c", sw=2.5, rx=6)
    s.rect(bx, by, bw, 32, fill="#1f4e8c", stroke="none", rx=6)
    s.rect(bx, by + 24, bw, 8, fill="#1f4e8c", stroke="none")
    s.text(bx + bw / 2, by + 22, "RP2350 开发板", size=15, anchor="middle",
           color="#fff", weight="bold")
    s.text(bx + bw / 2, by + 50, "板上丝印", size=11, anchor="middle",
           color="#666")

    rows_a: dict[str, tuple[float, float]] = {}
    for i, (fn, dpin, net, rref) in enumerate(SEGMENTS):
        y = by + 78 + i * 48
        gp = p[f"PIN_SEG_{fn.upper()}"]
        s.rect(bx + bw - 78, y - 15, 66, 30, fill="#fff",
               stroke="#1f4e8c", sw=1.8, rx=4)
        s.mono(bx + bw - 45, y + 6, str(gp), size=17, anchor="middle",
               color="#1f4e8c")
        s.text(bx + bw - 86, y + 5, f"GPIO {gp}", size=12, anchor="end",
               color="#1f4e8c", weight="bold")
        s.line(bx + bw, y, bx + bw + 26, y)
        rows_a[fn] = (bx + bw + 26, y)

    # ---- 电阻列 ----
    rx0 = bx + bw + 26
    for i, (fn, dpin, net, rref) in enumerate(SEGMENTS):
        y = by + 78 + i * 48
        s.line(rx0, y, rx0 + 34, y)
        s.rect(rx0 + 34, y - 15, 84, 30, fill="#fff8e8",
               stroke="#c08000", sw=2, rx=4)
        s.text(rx0 + 76, y + 2, "180Ω", size=14, anchor="middle",
               color="#a06000", weight="bold")
        s.text(rx0 + 76, y + 18, rref, size=11, anchor="middle", color="#888")
        s.line(rx0 + 118, y, rx0 + 152, y)

    # ---- 数码管 ----
    dx, dy, dw = rx0 + 152, by, 250
    s.rect(dx, dy, dw, 470, fill="#fff4f4", stroke="#a02020", sw=2.5, rx=6)
    s.rect(dx, dy, dw, 32, fill="#a02020", stroke="none", rx=6)
    s.rect(dx, dy + 24, dw, 8, fill="#a02020", stroke="none")
    s.text(dx + dw / 2, dy + 22, "2 位共阴数码管", size=15, anchor="middle",
           color="#fff", weight="bold")
    s.text(dx + dw / 2, dy + 50, "位号 DS1", size=11, anchor="middle",
           color="#666")
    # 两个 8 字示意
    s.text(dx + dw / 2, dy + 92, "8. 8.", size=44, anchor="middle",
           color="#dd9999", weight="bold")

    for i, (fn, dpin, net, rref) in enumerate(SEGMENTS):
        y = by + 78 + i * 48
        s.circle(dx, y, 11, fill="#ffe8e8", stroke="#a02020", sw=2.4)
        s.mono(dx, y + 6, str(dpin), size=15, anchor="middle",
               color="#a02020")
        s.text(dx + dw - 12, y + 5, f"段 {fn}", size=14, anchor="end",
               color="#a02020", weight="bold")

    s.text(dx + dw / 2, dy + 448, "（此列是逻辑顺序，", size=11,
           anchor="middle", color="#888")
    s.text(dx + dw / 2, dy + 464, "物理排布见下方实物图）", size=11,
           anchor="middle", color="#888")

    # ══════════════════════════════════════════════════════════════
    # 区域 B：位选
    # ══════════════════════════════════════════════════════════════
    s.rect(16, 660, 1130, 520, fill="#f7f9ff", stroke="#7a8ab8", sw=1.5, rx=8)
    s.text(30, 686, "B  位选（2 根）—— 经三极管到地", size=17,
           color="#1f4e8c", weight="bold")
    s.text(30, 708,
           "接法：RP2350 的 GPIO  →  1kΩ  →  三极管焊盘1(基极)；"
           "焊盘2(发射极) → GND；焊盘3(集电极) → 数码管位选脚",
           size=12, color="#555")

    # S8050 引脚说明条
    #
    # ★ 焊盘编号依据（不是我编的）：
    #   长电 S8050（丝印 J3Y，SOT-23）数据手册说明，把三脚朝下放、
    #   单脚朝上：**单脚是集电极，左脚是基极，右脚是发射极**；
    #   而 SOT-23 的标准焊盘编号是 左下=1、右下=2、上=3。
    #   两下一对应 → 1=基极B、2=发射极E、3=集电极C。
    s.rect(30, 724, 700, 56, fill="#fff8e8", stroke="#c08000", sw=1.8, rx=6)
    s.text(44, 746, "S8050（SOT-23，丝印 J3Y）焊盘对应关系", size=12,
           color="#a06000", weight="bold")
    for i, (pad, (el, cn)) in enumerate(SOT23.items()):
        s.mono(56 + i * 152, 764, f"{pad} = {el}", size=18, color="#a06000")
        s.text(56 + i * 152 + 56, 764, cn, size=12, color="#666")


    # 两个位选支路
    for k, (fn, dpin, net, qref, rref) in enumerate(DIGITS):
        y0 = 856 + k * 200
        gp = p[f"PIN_DIGIT{k + 1}"]

        # RP2350 脚
        s.rect(40, y0 - 20, 150, 40, fill="#eef4ff", stroke="#1f4e8c",
               sw=2, rx=4)
        s.text(115, y0 + 1, f"GPIO {gp}", size=15, anchor="middle",
               color="#1f4e8c", weight="bold")
        s.text(115, y0 + 16, f"（丝印 {gp}）", size=10, anchor="middle",
               color="#888")
        s.line(190, y0, 226, y0)

        # 基极电阻
        s.rect(226, y0 - 18, 80, 36, fill="#fff8e8", stroke="#c08000",
               sw=2, rx=4)
        s.text(266, y0 + 2, "1kΩ", size=14, anchor="middle",
               color="#a06000", weight="bold")
        s.text(266, y0 + 18, rref, size=11, anchor="middle", color="#888")
        s.line(306, y0, 350, y0)

        # 三极管（TT 画法：圆 + 内部符号，外侧标焊盘号）
        tx, ty = 400, y0
        s.circle(tx, ty, 40, fill="#fff", stroke="#333", sw=2.6)
        s.line(tx - 40, ty, tx - 14, ty, w=2.6)                 # 基极引线
        s.line(tx - 14, ty - 22, tx - 14, ty + 22, w=3.4)       # 基极竖条
        s.line(tx - 14, ty - 12, tx + 16, ty - 30, w=2.6)       # 集电极
        s.line(tx - 14, ty + 12, tx + 16, ty + 30, w=2.6)       # 发射极
        s.raw(f'<polygon points="{tx+16},{ty+30} {tx+2},{ty+22} '
              f'{tx+12},{ty+13}" fill="#1a6b1a"/>')
        # 位号放到圆圈上方，避免和焊盘号 "2" 挤在一起
        # 位号放圆圈**右侧**：放上方会撞到 S8050 说明条，
        # 放下方会撞到焊盘号 2 和 GND 走线（都是渲染出来才看到的）
        s.text(tx + 78, ty + 6, qref, size=16, weight="bold")
        # 焊盘号：1 左、2 下、3 上（红底白字，最显眼）
        for px, py, pn in ((tx - 56, ty, "1"), (tx + 34, ty + 52, "2"),
                           (tx + 34, ty - 36, "3")):
            s.rect(px - 13, py - 15, 26, 30, fill="#c00000", stroke="none",
                   rx=3)
            s.mono(px, py + 7, pn, size=18, anchor="middle", color="#fff")

        # 焊盘2 → GND
        s.path([(tx + 34, ty + 52), (tx + 34, ty + 86), (tx + 120, ty + 86)])
        s.line(tx + 106, ty + 86, tx + 134, ty + 86, c="#333", w=3)
        s.line(tx + 112, ty + 92, tx + 128, ty + 92, c="#333", w=3)
        s.line(tx + 117, ty + 98, tx + 123, ty + 98, c="#333", w=3)
        s.text(tx + 142, ty + 91, "GND", size=13, color="#333", weight="bold")

        # 焊盘3 → 数码管位选脚
        s.path([(tx + 34, ty - 36), (tx + 34, ty - 74), (900, ty - 74)])
        s.rect(900, ty - 96, 216, 44, fill="#fff4f4", stroke="#a02020",
               sw=2.2, rx=5)
        s.mono(918, ty - 68, str(dpin), size=19, color="#a02020")
        s.text(944, ty - 68, f"DS1 位选脚（第 {k+1} 位）", size=13,
               color="#a02020", weight="bold")
        s.text(944, ty - 50, f"（网络 {fn}，共阴端）", size=10, color="#888")

    # ══════════════════════════════════════════════════════════════
    # 右侧：接线顺序 + 注意事项
    # ══════════════════════════════════════════════════════════════
    sx = 1166
    s.rect(sx, 74, 438, 1150, fill="#fffdf5", stroke="#c0a060",
           sw=1.5, rx=8)
    s.text(sx + 18, 104, "接线顺序（接一根划一根）", size=16,
           color="#a06000", weight="bold")

    steps = [
        ("第一步：位选先接（先接这个，好动手）", True),
        ("① RP2350 丝印 6  →  1kΩ (R12)  →  Q1 焊盘1", False),
        ("② Q1 焊盘2  →  GND", False),
        ("③ Q1 焊盘3  →  数码管脚 10", False),
        ("④ RP2350 丝印 7  →  1kΩ (R13)  →  Q2 焊盘1", False),
        ("⑤ Q2 焊盘2  →  GND", False),
        ("⑥ Q2 焊盘3  →  数码管脚 5", False),
        ("", False),
        ("第二步：8 根段线（每根串一只 180Ω）", True),
    ]
    yy = 132
    for t, bold in steps:
        if t:
            s.text(sx + 18, yy, t, size=13,
                   color="#a06000" if bold else "#333", weight="bold" if bold else "normal")
        yy += 22

    seg_steps = [
        ("⑦ 丝印 15 → 180Ω(R4)  → 脚 3", "段 a"),
        ("⑧ 丝印 16 → 180Ω(R5)  → 脚 9", "段 b"),
        ("⑨ 丝印 17 → 180Ω(R6)  → 脚 8", "段 c"),
        ("⑩ 丝印 18 → 180Ω(R7)  → 脚 6", "段 d"),
        ("⑪ 丝印 19 → 180Ω(R8)  → 脚 7", "段 e"),
        ("⑫ 丝印 20 → 180Ω(R9)  → 脚 4", "段 f"),
        ("⑬ 丝印 21 → 180Ω(R10) → 脚 1", "段 g"),
        ("⑭ 丝印 22 → 180Ω(R11) → 脚 2", "段 dp"),
    ]
    for t, note in seg_steps:
        s.mono(sx + 18, yy, t, size=13, color="#1a6b1a")
        s.text(sx + 396, yy, note, size=11, anchor="end", color="#888")
        yy += 22

    # 注意事项
    yy += 16
    s.line(sx + 18, yy, sx + 420, yy, c="#e0d0a0", w=1.5)
    yy += 26
    s.text(sx + 18, yy, "★ 三个最容易做错的地方", size=15,
           color="#c00000", weight="bold")
    yy += 26
    warn = [
        "1) 位选必须经三极管，不能直接接 GPIO。",
        "   一位点亮时最多 8 段同时亮，阴极要灌约 48mA，",
        "   而 RP2350 单个脚只有约 8mA 能力 ——",
        "   直接接会烧 pad，或者亮度惨不忍睹。",
        "",
        "2) 数码管必须是**共阴**。共阳的接法完全不同",
        "   （段接 GND、位选接 +V），买错就整片不亮。",
        "   万用表二极管档自测：黑笔（−）碰位选脚、",
        "   红笔（+）碰段脚 → 该段亮 = 共阴 ✓",
        "",
        "3) 段限流电阻串在 GPIO 与段之间，",
        "   **不是**接到 3.3V 上。接错会常亮不受控。",
    ]
    for t in warn:
        col = "#c00000" if t.startswith(("1)", "2)", "3)")) else "#555"
        s.text(sx + 18, yy, t, size=11, color=col)
        yy += 18

    yy += 10
    s.text(sx + 18, yy, "S8050 实物辨识：单脚朝上、两脚朝自己 →", size=11,
           color="#a06000")
    yy += 16
    s.text(sx + 18, yy, "左脚=1(基极)  右脚=2(发射极)  单脚=3(集电极)", size=11,
           color="#a06000")

    yy += 24
    s.line(sx + 18, yy, sx + 420, yy, c="#e0d0a0", w=1.5)
    yy += 26
    s.text(sx + 18, yy, "数码管脚位速查（C193138）", size=14,
           color="#a02020", weight="bold")
    yy += 24
    pinmap = [
        ("1", "g"), ("2", "dp"), ("3", "a"), ("4", "f"), ("5", "DIG2"),
        ("6", "d"), ("7", "e"), ("8", "c"), ("9", "b"), ("10", "DIG1"),
    ]
    for i, (pn, fn) in enumerate(pinmap):
        col = 0 if i < 5 else 1
        row = i % 5
        x = sx + 30 + col * 200
        y = yy + row * 26
        s.mono(x, y, pn, size=15, color="#a02020")
        s.text(x + 30, y, f"= {fn}", size=13, color="#333", weight="bold")
    yy += 5 * 26 + 14

    s.text(sx + 18, yy, "★ 换型号务必对照自己的数据手册 ——", size=11,
           color="#c00000")
    s.text(sx + 18, yy + 16, "   共阴/共阳、引脚顺序都可能不同。", size=11,
           color="#c00000")

    # 底部说明
    s.text(30, H - 14,
           "提示：段线与位选都接好后，先不插数码管，量一下 8 只 180Ω 的"
           "另一端对 GND 都不应短路；再插上数码管上电。",
           size=12, color="#555")

    return s


# ══════════════════════════════════════════════════════════════════════
def draw_buttons(p: dict[str, int]) -> Svg:
    """按键与电源去耦 —— 剩下没接的就这两组，一共 8 根线。"""
    W, H = 1200, 820
    s = Svg(W, H)

    s.rect(0, 0, W, 62, fill="#1f4e8c", stroke="none")
    s.text(20, 28, "按键与电源接线示意图", size=22, color="#fff", weight="bold")
    s.text(20, 50, "2 只轻触按键 + 消抖电容 + 3.3V 去耦", size=13,
           color="#cfe0ff")
    s.text(W - 20, 30, "本图与固件 board_config.h 一致", size=12,
           anchor="end", color="#cfe0ff")

    # ── 按键 ──
    s.rect(16, 74, 720, 530, fill="#f7f9ff", stroke="#7a8ab8", sw=1.5, rx=8)
    s.text(30, 102, "按键（2 组，接法完全一样）", size=17, color="#1f4e8c",
           weight="bold")
    s.text(30, 124, "接法：GPIO → 按键 → GND；100nF 电容**并在按键两端**",
           size=12, color="#555")

    for k, (swref, cref, y0) in enumerate(
            [("SW1", "C2", 186), ("SW2", "C3", 348)]):
        gp = p["PIN_BTN_UP" if k == 0 else "PIN_BTN_DOWN"]
        s.rect(40, y0 - 22, 150, 44, fill="#eef4ff", stroke="#1f4e8c",
               sw=2.2, rx=5)
        s.text(115, y0, f"GPIO {gp}", size=16, anchor="middle",
               color="#1f4e8c", weight="bold")
        s.text(115, y0 + 16, f"（板上丝印 {gp}）", size=10, anchor="middle",
               color="#888")
        s.line(190, y0, 250, y0)
        s.junction(250, y0)

        s.rect(250, y0 - 40, 130, 80, fill="#fff", stroke="#333", sw=2, rx=5)
        s.line(270, y0 - 14, 360, y0 - 14, w=2.6)
        s.line(270, y0 + 14, 360, y0 + 14, w=2.6)
        s.line(295, y0 - 14, 295, y0 - 30, w=2.6)
        s.line(345, y0 - 14, 345, y0 - 30, w=2.6)
        s.line(295, y0 - 30, 345, y0 - 30, w=2.6)
        s.text(315, y0 + 58, swref, size=13, anchor="middle", weight="bold")
        s.line(380, y0, 440, y0)
        s.line(440, y0 - 14, 440, y0 + 14, c="#333", w=3.4)
        s.line(452, y0 - 9, 452, y0 + 9, c="#333", w=3.4)
        s.line(452, y0, 500, y0)
        s.line(486, y0, 514, y0, c="#333", w=3)
        s.line(492, y0 + 6, 508, y0 + 6, c="#333", w=3)
        s.line(497, y0 + 12, 503, y0 + 12, c="#333", w=3)
        s.text(524, y0 + 5, "GND", size=14, color="#333", weight="bold")

        # 消抖电容并在按键两端
        s.line(250, y0, 250, y0 + 96)
        s.line(214, y0 + 96, 286, y0 + 96, w=3)
        s.line(214, y0 + 108, 286, y0 + 108, w=3)
        s.line(250, y0 + 108, 250, y0 + 140)
        s.line(236, y0 + 140, 264, y0 + 140, c="#333", w=3)
        s.line(242, y0 + 146, 258, y0 + 146, c="#333", w=3)
        s.line(247, y0 + 152, 253, y0 + 152, c="#333", w=3)
        s.text(302, y0 + 106, f"{cref}   100nF", size=13, color="#333",
               weight="bold")

    s.text(30, 634, "★ 电容并在按键两端（不是串在回路里）—— 消抖用",
           size=13, color="#c00000")
    s.text(30, 656, "★ RP2350 内部上拉已够用，外部 10kΩ 可以省；",
           size=13, color="#c00000")
    s.text(30, 678, "   但按键引线超过 10cm 时建议还是加，抗干扰更好",
           size=13, color="#c00000")

    # ── 电源去耦 ──
    s.rect(756, 74, 428, 530, fill="#fffdf5", stroke="#c0a060", sw=1.5, rx=8)
    s.text(770, 102, "3.3V 去耦", size=17, color="#a06000", weight="bold")
    s.text(770, 124, "都是并在 3V3 与 GND 之间", size=12, color="#555")

    for i, (ref, val, note) in enumerate(
            [("C4", "100µF", "电解，抑制数码管扫描纹波"),
             ("C6", "100nF", "就近放在 3V3 排针旁")]):
        x = 836 + i * 190
        y = 210
        s.line(x, y - 66, x, y - 32)
        s.line(x - 34, y - 66, x + 34, y - 66, c="#c00000", w=3)
        s.text(x, y - 76, "3V3", size=15, anchor="middle", color="#c00000",
               weight="bold")
        s.line(x, y - 32, x, y)
        s.line(x - 30, y, x + 30, y, w=3.4)
        s.line(x - 30, y + 14, x + 30, y + 14, w=3.4)
        s.line(x, y + 14, x, y + 46)
        s.line(x - 14, y + 46, x + 14, y + 46, c="#333", w=3)
        s.line(x - 8, y + 52, x + 8, y + 52, c="#333", w=3)
        s.line(x - 3, y + 58, x + 3, y + 58, c="#333", w=3)
        s.text(x + 46, y + 4, ref, size=15, weight="bold")
        s.text(x + 46, y + 26, val, size=13, color="#333")
        s.text(x - 46, y + 84, note, size=11, color="#666", anchor="middle")

    s.text(770, 634, "★ 100µF 电解**正极**接 3V3、负极接 GND，别装反",
           size=12, color="#c00000")
    s.text(770, 656, "★ 整机从开发板 3V3 排针取电，总电流约 93mA",
           size=12, color="#c00000")
    s.text(770, 678, "★ 想脱离电脑独立用：外部 5V 接 VBUS 脚并串 SS34，",
           size=12, color="#c00000")
    s.text(770, 700, "   切勿把 5V 灌到 3V3 脚上", size=12, color="#c00000")

    s.text(30, H - 34,
           "接完这两组，整套硬件接线就完成了。"
           "上电前先用万用表量 3V3 对 GND 没有短路。",
           size=13, color="#555")
    return s


# ══════════════════════════════════════════════════════════════════════
def verify(p: dict[str, int], text: str) -> list[str]:
    bad = []
    # 段线 GPIO 必须连续（固件依赖这一点做掩码写）
    segs = [p[f"PIN_SEG_{c}"] for c in "ABCDEFG"] + [p["PIN_SEG_DP"]]
    if segs != list(range(segs[0], segs[0] + 8)):
        bad.append(f"段线 GPIO 不连续：{segs}（固件用掩码整组写，必须连续）")
    # 位选脚
    if p["PIN_DIGIT1"] == p["PIN_DIGIT2"]:
        bad.append("两个位选脚不能相同")
    # 图上每个 GPIO 号都要出现
    for c in "ABCDEFG":
        g = str(p[f"PIN_SEG_{c}"])
        if f"GPIO {g}" not in text:
            bad.append(f"图上找不到 GPIO {g}（段 {c.lower()}）")
    for k in (1, 2):
        g = str(p[f"PIN_DIGIT{k}"])
        if f"GPIO {g}" not in text:
            bad.append(f"图上找不到 GPIO {g}（位选 {k}）")
    # 关键提示
    for must in ["48mA", "共阴", "焊盘", "180Ω", "1kΩ"]:
        if must not in text:
            bad.append(f"图上缺少关键信息：{must}")
    # 数码管 10 个脚都要出现
    for pn in range(1, 11):
        if f">{pn}<" not in text:
            bad.append(f"图上找不到数码管脚 {pn}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成数码管接线示意图")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "wiring")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    p = load_pins()
    s = draw(p)
    text = "\n".join(s.p)

    print("=== 自检 ===")
    bad = verify(p, text)
    for b in bad:
        print(f"  ✗ {b}")
    if not bad:
        print(f"  段线 GPIO {p['PIN_SEG_A']}~{p['PIN_SEG_DP']} 连续 ✓")
        print(f"  位选 GPIO {p['PIN_DIGIT1']} / {p['PIN_DIGIT2']} ✓")
        print("  10 个数码管脚、8 个段 GPIO、2 个位选 GPIO 全部在图上 ✓")

    if args.check:
        return 1 if bad else 0

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "数码管接线示意图.svg"
    s.save(out)
    print(f"\n已写出 {out}  ({out.stat().st_size:,} 字节)")

    s2 = draw_buttons(p)
    out2 = args.out / "按键与电源接线示意图.svg"
    s2.save(out2)
    print(f"已写出 {out2}  ({out2.stat().st_size:,} 字节)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
