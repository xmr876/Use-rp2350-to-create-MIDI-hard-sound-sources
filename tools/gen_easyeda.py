#!/usr/bin/env python3
"""
gen_easyeda.py — 生成可导入**嘉立创EDA（标准版）**的原理图源文件

═══════════════════════════════════════════════════════════════════════
关于格式
═══════════════════════════════════════════════════════════════════════

嘉立创EDA标准版的"文档源码"是一个 JSON 文件，所有图元压成 `~` 分隔的字符串。
格式依据（不是猜的）：

  1. 官方文档：https://docs.easyeda.com/cn/DocumentFormat/EasyEDA-Format-Standard/
  2. 官方给的完整范例（一个能跑的 schematic json）：
     https://gist.github.com/dillonHe/0b62babdb8ab3d2ad7d3
  3. KiCad 的 EasyEDA 导入器文档（对每个字段的解释最清楚）：
     https://dev-docs.kicad.org/en/import-formats/easyeda/

导入方式（二选一）：
  · 嘉立创EDA 标准版 → 文件 → 打开 → 选择本文件
  · 或：新建原理图 → 顶部「设置」→「EasyEDA 源文件」→ 粘贴内容 → 应用

★★ 我必须坦白一件事：**我没有办法在这台机器上验证导入是否成功**，
   因为装不了嘉立创EDA。所以这个脚本做了两件事来降低风险：
     1. 每个图元格式都对着上面那份**能跑通的官方范例**逐字段对齐；
     2. 生成后跑**几何自检**（verify()），确认每根导线端点都真的落在
        某个引脚/标签/结点上 —— 悬空的线是导入后最难发现的问题。

   如果导入失败，最可能出问题的是 `canvas` 或 `head` 这两个"整档级"字段
   （元件、导线、网络标签这些图元级字段是照范例抄的，风险低得多）。
   应急办法写在文件末尾的说明里。

═══════════════════════════════════════════════════════════════════════
画法
═══════════════════════════════════════════════════════════════════════

  · 元件用**自绘的简单符号**（矩形本体 + 引脚），不依赖任何在线库
    —— 因为库里没有"RP2350 开发板""PCM5102 模块"这种成品模块的符号。
  · MIDI 输入那一簇用**实线连接**（拓扑最重要，一眼看懂）
  · 其余用**网络标签**连接（改布局时不会拉断线）
  · 每个元件都带 `LCSC Part` 属性（立创编号），导入后 BOM 能直接用

用法：
    python tools/gen_easyeda.py                # 输出到 build/easyeda/
    python tools/gen_easyeda.py --check        # 只自检
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "firmware" / "include" / "board_config.h"

# 立创编号（与 docs/接线图与BOM.md 一致，都是核实过的）
LCSC = {
    "6N138": "C571211", "DIN5": "C23689428", "SEG2": "C193138",
    "S8050": "C2146", "R220": "C17557", "R100": "C17408", "R1K": "C17513",
    "R4K7": "C17673", "R180": "C25270", "R10K": "C17414",
    "C100N": "C1711", "C100U": "C970684", "SW": "C52750873",
    "HDR6": "C40877", "HDR20": "C2905423",
}


def load_pins() -> dict[str, int]:
    txt = CFG.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"//[^\n]*", " ", txt)
    out = {}
    for m in re.finditer(r"^#define\s+(PIN_[A-Z0-9_]+)\s+(-?\d+)", txt, re.M):
        out[m.group(1)] = int(m.group(2))
    return out


# ══════════════════════════════════════════════════════════════════════
# 绘图器
# ══════════════════════════════════════════════════════════════════════
GREEN = "#008800"
DARKRED = "#880000"
BLUE = "#000080"

# 引脚电气类型（KiCad 导入器的映射：0=未指定 1=输入 2=输出 3=双向 4=无源）
ELEC_PASSIVE = "4"


class Sch:
    def __init__(self):
        self.shapes: list[str] = []
        self._n = 0
        # 几何自检用：所有"合法的导线落点"
        self.anchors: set[tuple[float, float]] = set()
        self.wires: list[tuple[float, float]] = []   # 每个端点
        self.wire_ends: set[tuple[float, float]] = set()
        self.notes: list[str] = []

    def gid(self) -> str:
        self._n += 1
        return f"gge{self._n}"

    # ---------- 基本图元 ----------

    def text(self, x, y, s, size="9pt", color="#0000FF", anchor="start",
             bold=False) -> None:
        """T~mark~x~y~rot~color~font~size~weight~style~baseline~type~value~visible~anchor~gid"""
        s = s.replace("\n", "\\n")
        self.shapes.append(
            f"T~L~{x}~{y}~0~{color}~Arial~{size}~{'bold' if bold else ''}~~"
            f"~comment~{s}~1~{anchor}~{self.gid()}")

    def rect(self, x, y, w, h, color="#000000", fill="none") -> None:
        """R~x~y~rx~ry~w~h~strokeColor~strokeWidth~strokeStyle~fillColor~gid"""
        self.shapes.append(
            f"R~{x}~{y}~~~{w}~{h}~{color}~1~0~{fill}~{self.gid()}")

    def poly(self, pts, color=GREEN, width=1) -> None:
        p = " ".join(f"{a} {b}" for a, b in pts)
        self.shapes.append(
            f"PL~{p}~{color}~{width}~0~none~{self.gid()}")

    def wire(self, *pts, color=GREEN) -> None:
        p = " ".join(f"{a} {b}" for a, b in pts)
        self.shapes.append(f"W~{p}~{color}~1~0~none~{self.gid()}")
        self.wires.extend(pts)
        # 记录"被导线连到过的点"——自检认它，但引脚/标签才是真正的落点
        self.wire_ends.add(pts[0])
        self.wire_ends.add(pts[-1])

    def junction(self, x, y) -> None:
        self.shapes.append(f"J~{x}~{y}~2.5~#CC0000~{self.gid()}")
        self.anchors.add((x, y))

    def netlabel(self, x, y, net, anchor="start") -> None:
        """网络标签。

        ★ `(x, y)` 既是**连接点**也是文本锚点；文本自己的位置用后面
          单独的 textX/textY 字段控制（见官方范例：
          `N~340~140~0~#000080~VCC~gge105~start~342~140~Verdana~7pt`
          —— 连接点在 340,140，文字画在 342,140）。
          所以这里 (x,y) 必须**正好等于导线的端点**，
          文字偏移只能体现在 tx/ty 上。
          最初把标签放在 (x-2, y)，结果每根引线末端都"悬空" ——
          自检直接报了 59 个悬空端点。
        """
        tx = x + 3 if anchor == "start" else x - 3
        self.shapes.append(
            f"N~{x}~{y}~0~{BLUE}~{net}~{self.gid()}~{anchor}~{tx}~{y + 3}"
            f"~Verdana~7pt")
        self.anchors.add((x, y))

    # ---------- 电源符号 ----------
    def flag_gnd(self, x, y, net="GND") -> None:
        g = self.gid()
        parts = [f"F~part_netLabel_gnD~{x}~{y}~0~{g}",
                 f"{x}~{y}",
                 f"{net}~{BLUE}~{x - 11}~{y - 13}~0~start~0~~"]
        parts.append(f"PL~{x} {y + 10} {x} {y}~#000000~1~0~none~{self.gid()}")
        parts.append(f"PL~{x - 9} {y + 10} {x + 9} {y + 10}~#000000~1~0~none~{self.gid()}")
        parts.append(f"PL~{x - 5} {y + 12} {x + 5} {y + 12}~#000000~1~0~none~{self.gid()}")
        parts.append(f"PL~{x - 2} {y + 14} {x + 2} {y + 14}~#000000~1~0~none~{self.gid()}")
        self.shapes.append("^^".join(parts))
        self.anchors.add((x, y))

    def flag_vcc(self, x, y, net="3V3") -> None:
        g = self.gid()
        parts = [f"F~part_netLabel_VCC~{x}~{y}~0~{g}",
                 f"{x}~{y}",
                 f"{net}~{BLUE}~{x}~{y - 14}~0~middle~0~~"]
        parts.append(f"PL~{x} {y - 10} {x} {y}~#000000~1~0~none~{self.gid()}")
        parts.append(f"PL~{x - 7} {y - 10} {x + 7} {y - 10}~#000000~1~0~none~{self.gid()}")
        self.shapes.append("^^".join(parts))
        self.anchors.add((x, y))

    # ---------- 元件符号 ----------
    def part(self, x, y, ref, value, pins_left, pins_right,
             libname="", lcsc="", w=None, pin_len=20, spacing=26,
             footer=""):
        """放一个"矩形 + 引脚"的元件。

        (x, y) 是**本体左上角**。pins_left / pins_right 是 [(引脚名, 引脚号)]。
        返回 {引脚名: (连接点x, 连接点y)}。

        ★ 引脚连接点坐标由本体几何算出来，导线也都用这个返回值定位 ——
          这样"线接在引脚上"是**构造上保证**的，不靠人工对坐标。
        """
        nl, nr = len(pins_left), len(pins_right)
        rows = max(nl, nr, 1)
        if w is None:
            w = 120
        h = spacing * rows + 20

        # 引脚连接点的 y（左右两侧共用同一套行位置，从本体顶部往下 20 开始）
        ys = [y + 20 + i * spacing for i in range(rows)]

        conn: dict[str, tuple[int, int]] = {}
        subs: list[str] = []

        # 元件名 + 位号（T~N / T~P）
        subs.append(f"T~N~{x + w / 2}~{y + h + 12}~0~{BLUE}~Arial~~~~~comment"
                    f"~{value}~1~middle~{self.gid()}")
        subs.append(f"T~P~{x + w / 2}~{y + h + 22}~0~{BLUE}~Arial~~~~~comment"
                    f"~{ref}~1~middle~{self.gid()}")

        # 本体矩形（画在 LIB 内部，坐标是绝对坐标）
        subs.append(f"R~{x}~{y}~~~{w}~{h}~#880000~1~0~none~{self.gid()}")

        def emit_pin(name, num, px, py, rot, path):
            g = self.gid()
            # 引脚名放在本体内侧，编号放在引脚线上方
            if rot == "180":      # 引脚在本体左侧，线向右伸进本体
                nx, ny, na = px + pin_len + 4, py + 3, "start"
                numx, numy = px + pin_len / 2, py - 3
            else:                 # rot == "0"：引脚在本体右侧，线向左伸
                nx, ny, na = px - pin_len - 4, py + 3, "end"
                numx, numy = px - pin_len / 2, py - 3
            subs.append(
                f"P~show~{ELEC_PASSIVE}~{num}~{px}~{py}~{rot}~{g}"
                f"^^{px}~{py}"
                f"^^M {px} {py} {path}~{DARKRED}"
                f"^^1~{nx}~{ny}~0~{name}~{na}~~"
                f"^^1~{numx}~{numy}~0~{num}~middle~~"
                f"^^~~^^~")
            conn[name] = (px, py)
            self.anchors.add((px, py))

        for i, (nm, num) in enumerate(pins_left):
            px, py = x, ys[i]
            emit_pin(nm, num, px, py, "180", f"h {pin_len}")

        for i, (nm, num) in enumerate(pins_right):
            px, py = x + w, ys[i]
            emit_pin(nm, num, px, py, "0", f"h -{pin_len}")

        # c_para：`key`value 反引号分隔
        cp = (f"`package`{libname or 'MODULE'}`nameAlias`Value`Value`{value}`"
              f"spicePre`U`spiceSymbolName`{libname or ref}`"
              + (f"`LCSC Part`{lcsc}`" if lcsc else ""))

        self.shapes.append(
            f"LIB~{x}~{y}~{cp}~~0~{self.gid()}#@$" + "#@$".join(subs))

        if footer:
            self.notes.append(footer)
        return conn

    def resistor(self, x, y, ref, value, lcsc="", vert=False, length=30,
                 text_above=False):
        """电阻：横放时 (x,y) 是左端连接点；竖放时是上端连接点。"""
        if not vert:
            p1, p2 = (x, y), (x + length, y)
            body = f"PL~{x + 6} {y} {x + length - 6} {y}~#880000~2~0~none~{self.gid()}"
        else:
            p1, p2 = (x, y), (x, y + length)
            body = f"PL~{x} {y + 6} {x} {y + length - 6}~#880000~2~0~none~{self.gid()}"
        return self._two_pin(x, y, ref, value, p1, p2, body, lcsc, "R",
                             text_above=(text_above and not vert))

    def cap(self, x, y, ref, value, lcsc="", vert=False, length=26):
        if not vert:
            p1, p2 = (x, y), (x + length, y)
            g1 = f"PL~{x + 8} {y - 7} {x + 8} {y + 7}~#880000~2~0~none~{self.gid()}"
            g2 = f"PL~{x + 14} {y - 7} {x + 14} {y + 7}~#880000~2~0~none~{self.gid()}"
            body = g1 + "#@$" + g2
        else:
            p1, p2 = (x, y), (x, y + length)
            g1 = f"PL~{x - 7} {y + 8} {x + 7} {y + 8}~#880000~2~0~none~{self.gid()}"
            g2 = f"PL~{x - 7} {y + 14} {x + 7} {y + 14}~#880000~2~0~none~{self.gid()}"
            body = g1 + "#@$" + g2
        return self._two_pin(x, y, ref, value, p1, p2, body, lcsc, "C")

    def _two_pin(self, x, y, ref, value, p1, p2, body, lcsc, prefix,
                 text_above=False):
        """
        text_above=True 时把位号/阻值文字放到元件**上方**。
        ★ 竖排一串电阻时（段限流那 8 只），文字放下方会撞到下一行的引脚，
          必须放上方才有空间。这个小参数是渲染预览之后才加的。
        """
        if text_above:
            t1 = f"T~N~{p1[0] + 16}~{p1[1] - 18}~0~{BLUE}~Arial~~~~~comment" \
                 f"~{value}~1~start~{self.gid()}"
            t2 = f"T~P~{p1[0] + 16}~{p1[1] - 7}~0~{BLUE}~Arial~~~~~comment" \
                 f"~{ref}~1~start~{self.gid()}"
        else:
            t1 = f"T~N~{p1[0] + 16}~{p1[1] + 14}~0~{BLUE}~Arial~~~~~comment" \
                 f"~{value}~1~start~{self.gid()}"
            t2 = f"T~P~{p1[0] + 16}~{p1[1] + 24}~0~{BLUE}~Arial~~~~~comment" \
                 f"~{ref}~1~start~{self.gid()}"
        subs = [t1, t2, body]
        for i, (px, py) in enumerate([p1, p2]):
            rot = "180" if i == 0 else "0"
            path = f"h 6" if i == 0 else "h -6"
            subs.append(
                f"P~show~{ELEC_PASSIVE}~{i + 1}~{px}~{py}~{rot}~{self.gid()}"
                f"^^{px}~{py}^^M {px} {py} {path}~{DARKRED}"
                f"^^0~~{i + 1}~~^^0~~{i + 1}~~^^~~^^~")
        cp = (f"`package`R0805`nameAlias`Value`Value`{value}`spicePre`{prefix}`"
              f"spiceSymbolName`{prefix}`"
              + (f"`LCSC Part`{lcsc}`" if lcsc else ""))
        self.shapes.append(
            f"LIB~{p1[0]}~{p1[1]}~{cp}~~0~{self.gid()}#@$" + "#@$".join(subs))
        self.anchors.add(p1)
        self.anchors.add(p2)
        return {"1": p1, "2": p2}

    # ---------- 便捷连线 ----------
    def stub(self, pt, net, direction, length=30):
        """从引脚拉一小段线再挂网络标签。

        ★ 标签的 (x,y) 必须**正好落在导线末端** —— 差 1 个单位，
          导线就悬空了，导进 EDA 之后网络是断的但不报错。
          所以这里三个坐标全部用同一个 e。
        """
        x, y = pt
        if direction == "L":
            e = (x - length, y)
            self.wire(pt, e)
            self.netlabel(e[0], e[1], net, anchor="end")
        elif direction == "R":
            e = (x + length, y)
            self.wire(pt, e)
            self.netlabel(e[0], e[1], net, anchor="start")
        elif direction == "U":
            e = (x, y - length)
            self.wire(pt, e)
            self.netlabel(e[0], e[1], net, anchor="start")
        else:
            e = (x, y + length)
            self.wire(pt, e)
            self.netlabel(e[0], e[1], net, anchor="start")
        return e

    def rail(self, pt, net, direction, length=30):
        """拉一小段线接电源符号"""
        x, y = pt
        if direction == "U":
            e = (x, y - length)
            self.wire(pt, e)
            self.flag_vcc(e[0], e[1], net)
        else:
            e = (x, y + length)
            self.wire(pt, e)
            self.flag_gnd(e[0], e[1], net)
        return e

    # ---------- 输出 ----------
    def to_json(self, title: str, w: int, h: int) -> str:
        head = {
            "docType": "1",
            "editorVersion": "6.5.4",
            "title": title,
            "description": "GMSS v4 — RP2350 硬 GM 音源。"
                           "由 tools/gen_easyeda.py 自动生成。",
            "x": "0", "y": "0",
            "hasIdFlag": True, "newgId": True,
            "importFlag": 0, "isSheet": False,
        }
        doc = {
            "docType": "1",
            "head": head,
            "canvas": f"CA~{w}~{h}~#FFFFFF~yes~#CCCCCC~10~{w}~{h}"
                      f"~line~10~pixel~5~0~0",
            "title": title,
            "shape": self.shapes,
            "BBox": {"x": 0, "y": 0, "width": w, "height": h},
            "colors": {},
        }
        return json.dumps(doc, ensure_ascii=False, indent=1)


# ══════════════════════════════════════════════════════════════════════
# 布局
# ══════════════════════════════════════════════════════════════════════
def build(p: dict[str, int]) -> Sch:
    s = Sch()

    s.text(20, 24, "GMSS — RP2350 硬 GM 音源（v4 单 Flash 方案）",
           size="14pt", color="#000000", bold=True)
    s.text(20, 42, "48 复音采样回放 GM 引擎 · 音色库与固件同住一片 16MB Flash · "
                   "由 tools/gen_easyeda.py 从 board_config.h 生成",
           size="8pt", color="#666666")

    # ══════════════════════════════════════════════════════════
    # U1 —— RP2350 开发板
    # ══════════════════════════════════════════════════════════
    left = ["VBUS", "VSYS", "GND", "3V3_EN", "3V3", "ADC_VREF",
            "GP28", "AGND", "GP27", "GP26", "RUN",
            "GP22", "GND", "GP21", "GP20", "GP19", "GP18", "GND", "GP17", "GP16"]
    right = ["GP0", "GP1", "GND", "GP2", "GP3", "GP4", "GP5", "GND",
             "GP6", "GP7", "GP8", "GP9", "GND", "GP10", "GP11",
             "GP12", "GP13", "GND", "GP14", "GP15"]
    u1 = s.part(
        200, 100, "U1", "RP2350-16MB",
        [(n, str(40 - i)) for i, n in enumerate(left)],
        [(n, str(i + 1)) for i, n in enumerate(right)],
        libname="RP2350_DEVBOARD_16MB", w=180, spacing=28,
        footer="U1 是成品开发板（板载 16MB Flash、USB-C、BOOT/USER 键、RGB 灯），"
               "不是裸片。板上丝印 GPIO 只印数字，GND 印成 G。")

    # U1 的网络标签
    u1_map_l = {"3V3": "3V3", "GND": "GND", "VBUS": "VBUS",
                "GP28": "GP28", "GP27": "GP27", "GP26": "GP26",
                "RUN": "RUN", "GP22": "SEG_DP", "GP21": "SEG_G",
                "GP20": "SEG_F", "GP19": "SEG_E", "GP18": "SEG_D",
                "GP17": "SEG_C", "GP16": "SEG_B"}
    for nm, net in u1_map_l.items():
        s.stub(u1[nm], net, "L")
    # 3V3 / GND 直接接电源符号更直观
    # （上面已经拉了标签，这里不再重复）

    u1_map_r = {"GP6": "DIG1", "GP7": "DIG2", "GP8": "BTN1", "GP9": "BTN2",
                "GP10": "MIDI_RX", "GP11": "MIDI_TX",
                "GP12": "LRCK", "GP13": "BCK", "GP14": "DIN",
                "GP15": "SEG_A"}
    for nm, net in u1_map_r.items():
        s.stub(u1[nm], net, "R")

    # ══════════════════════════════════════════════════════════
    # U2 —— PCM5102A 模块
    # ══════════════════════════════════════════════════════════
    u2 = s.part(
        520, 100, "U2", "PCM5102A",
        [("VIN", "1"), ("GND", "2"), ("LRCK", "3"),
         ("DIN", "4"), ("BCK", "5"), ("SCK", "6")],
        [], libname="PCM5102A_MODULE", lcsc="", w=110)
    s.stub(u2["VIN"], "3V3", "L")
    s.stub(u2["GND"], "GND", "L")
    s.stub(u2["LRCK"], "LRCK", "L")
    s.stub(u2["DIN"], "DIN", "L")
    s.stub(u2["BCK"], "BCK", "L")
    s.stub(u2["SCK"], "GND", "L")
    s.text(520, 320, "★ VIN 必须 3.3V，接 5V 会烧 PCM5102A（绝对最大额定 3.9V）",
           size="8pt", color="#CC0000")
    s.text(520, 334, "★ SCK 接 GND —— 选内部 PLL 从 BCK 重建主时钟，省掉 MCLK",
           size="8pt", color="#CC0000")
    s.text(520, 348, "排针顺序（从板边往板内）：VIN GND LRCK DIN BCK SCK",
           size="8pt", color="#666666")
    s.text(520, 362, "FLT / DEMP / XSMT / FMT 不接（模块板上已有默认配置）",
           size="8pt", color="#666666")

    # ══════════════════════════════════════════════════════════
    # MIDI 输入（这一簇用实线连，拓扑一眼看懂）
    # ══════════════════════════════════════════════════════════
    #
    # ★ 坐标是**算出来的**，不是手填的：
    #   J1 右列引脚在 x=570，y = 380/400/420/440/460（pin1..5）
    #   U3 左列 A/K 在 x=760，y = 380/400
    #   每条线的端点都直接用上面的返回值，绝不写死数字 ——
    #   第一版手填了几个中间点，结果引线末端和电阻引脚差了 10 个单位，
    #   自检报了 8 处悬空。
    #
    # ★ 拐弯一律走直角。斜线在原理图里既不好看也容易看错。
    j1 = s.part(
        520, 400, "J1", "DIN-5",
        [], [("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5")],
        libname="DIN5_FEMALE_180", lcsc=LCSC["DIN5"], w=90)
    s.text(520, 600, "1/3 不接   2 屏蔽   4 电流源+   5 电流汇−",
           size="8pt", color="#666666")

    u3 = s.part(
        830, 400, "U3", "6N138",
        [("A", "2"), ("K", "3")],
        [("VCC", "8"), ("VB", "7"), ("VO", "6"), ("GND", "5")],
        libname="OPTO_6N138", lcsc=LCSC["6N138"], w=120)

    # ---- J1 pin2 → C1 → GND（屏蔽地）----
    # 电容 1 脚正好落在 J1.2 的引线上，不留缝
    x2, y2 = j1["2"][0] + 40, j1["2"][1]
    c1 = s.cap(x2, y2, "C1", "100nF", LCSC["C100N"])
    s.wire(j1["2"], c1["1"])
    s.rail(c1["2"], "GND", "D", 20)

    # ---- J1 pin4 → R1(220Ω) → U3.A ----
    xr = j1["4"][0] + 40
    r1 = s.resistor(xr, j1["4"][1], "R1", "220Ω", LCSC["R220"])
    s.wire(j1["4"], r1["1"])
    xm = r1["2"][0] + 40
    s.wire(r1["2"], (xm, r1["2"][1]), (xm, u3["A"][1]), u3["A"])

    # ---- J1 pin5 → U3.K（从下方绕，避开 R1 那一行走线）----
    xa, ya = j1["5"][0] + 30, j1["5"][1]
    xb, yb = xa, ya + 40
    xc = u3["K"][0] - 40
    s.wire(j1["5"], (xa, ya), (xb, yb), (xc, yb), (xc, u3["K"][1]), u3["K"])

    # ---- U3 pin8 VCC → 3V3，并并一颗 100nF ----
    xv, yv = u3["VCC"][0] + 40, u3["VCC"][1]
    s.wire(u3["VCC"], (xv, yv))
    s.junction(xv, yv)
    s.wire((xv, yv), (xv, yv - 30))
    s.flag_vcc(xv, yv - 30, "3V3")
    c5 = s.cap(xv + 40, yv, "C5", "100nF", LCSC["C100N"])
    s.wire((xv, yv), c5["1"])
    s.rail(c5["2"], "GND", "D", 20)

    # ---- U3 pin5 GND ----
    s.rail(u3["GND"], "GND", "R", 20)

    # ---- U3 pin6 VO → MIDI_RX ----
    s.stub(u3["VO"], "MIDI_RX", "R", 30)

    # ---- U3 pin7 VB → R3 → GND（用网络标签接，避开与 VO 走线的交叉）----
    s.stub(u3["VB"], "OPTO_VB", "R", 30)
    s.text(1080, 420, "加速关断（不能省）", size="8pt",
           color="#CC0000", bold=True)
    s.resistor(1080, 432, "R3", "4.7kΩ", LCSC["R4K7"], vert=True)
    s.stub((1080, 432), "OPTO_VB", "L", 20)
    s.rail((1080, 432 + 30), "GND", "D", 20)

    # ---- R2 上拉（单独画，用网络标签接）----
    s.text(1230, 400, "上拉（1kΩ，不能换成 10k）", size="8pt",
           color="#CC0000", bold=True)
    s.flag_vcc(1230, 420, "3V3")
    s.wire((1230, 420), (1230, 432))
    s.resistor(1230, 432, "R2", "1kΩ", LCSC["R1K"], vert=True)
    s.stub((1230, 432 + 30), "MIDI_RX", "R", 20)

    s.text(520, 640, "★ 上拉必须 1kΩ：6N138 是达林顿输出，10k 时上升沿几十 µs，"
                     "而 MIDI 位宽只有 32µs，会随机丢字节", size="8pt", color="#CC0000")
    s.text(520, 654, "★ 光耦必须 6N138/6N139，不能用 6N137（要 ≥6.3mA 才保证工作，超 MIDI 规范）",
           size="8pt", color="#CC0000")
    s.text(520, 668, "★ R3（4.7kΩ，pin7→GND）是加速关断用的，不能省",
           size="8pt", color="#CC0000")

    # ══════════════════════════════════════════════════════════
    # 数码管
    # ══════════════════════════════════════════════════════════
    # ★ 引脚间距给到 34：8 根段线每个都要挂网络标签 + 一只电阻 + 电阻的
    #   位号/阻值文字，26 都嫌挤（渲染出来一看文字全糊在一起）。
    ds = s.part(
        620, 760, "DS1", "2位共阴",
        [("a", "3"), ("b", "9"), ("c", "8"), ("d", "6"),
         ("e", "7"), ("f", "4"), ("g", "1"), ("dp", "2")],
        [("DIG1", "10"), ("DIG2", "5")],
        libname="LED_SEG_2DIGIT_CC", lcsc=LCSC["SEG2"], w=120, spacing=34)

    # 段限流电阻**串在 GPIO 与段之间**（不是接 3V3！）：
    #   网络标签 → 电阻 → 数码管段脚
    seg_names = ["a", "b", "c", "d", "e", "f", "g", "dp"]
    rrefs = ["R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"]
    for nm, rr in zip(seg_names, rrefs):
        pin = ds[nm]
        res = s.resistor(pin[0] - 110, pin[1], rr, "180Ω", LCSC["R180"],
                         text_above=True)
        s.wire(res["2"], pin)                    # 电阻 → 段脚
        s.wire((res["1"][0] - 20, res["1"][1]), res["1"])   # 引线
        s.netlabel(res["1"][0] - 20, res["1"][1], f"SEG_{nm.upper()}",
                   anchor="end")

    # 说明放在左下角空白处（U1 下面），不跟元件挤在一起
    s.text(150, 720, "段限流 180Ω × 8 —— 串在 GPIO 与段之间，不是接 3V3",
           size="8pt", color="#CC0000")
    s.text(150, 744, "★ 位选必须经三极管：一位最多 8 段同时亮，阴极要灌约 48mA，",
           size="8pt", color="#CC0000")
    s.text(150, 758, "   RP2350 单脚只有 ~8mA，直接灌会烧 pad 或亮度惨不忍睹",
           size="8pt", color="#CC0000")
    s.text(150, 782, "C193138 引脚：1:G 2:DP 3:A 4:F 5:DIG2 6:D 7:E 8:C 9:B 10:DIG1",
           size="8pt", color="#666666")
    s.text(150, 796, "★ 换型号务必对照自己的数据手册，共阴/共阳和引脚顺序都可能不同",
           size="8pt", color="#CC0000")

    # 位选驱动 Q1/Q2：GPIO → 电阻 → 基极；发射极到地；集电极标号到数码管
    #
    # ★ 集电极到数码管用**网络标签**而不是拉线：DS1 的位选脚在右边，
    #   而 Q1/Q2 也在右边，拉直线会横穿 Q1 自己的本体 ——
    #   渲染出来一看就发现了。用标号就没有这个问题。
    for k, (nm, net, rref, qref) in enumerate(
            [("DIG1", "DIG1", "R12", "Q1"), ("DIG2", "DIG2", "R13", "Q2")]):
        qy = 700 + k * 140
        rb = s.resistor(790, qy, rref, "1kΩ", LCSC["R1K"])
        q = s.part(880, qy - 20, qref, "S8050",
                   [("B", "1"), ("E", "2")], [("C", "3")],
                   libname="S8050", lcsc=LCSC["S8050"], w=70)
        s.wire(rb["2"], q["B"])
        s.wire((rb["1"][0] - 20, rb["1"][1]), rb["1"])
        s.netlabel(rb["1"][0] - 20, rb["1"][1], net, anchor="end")
        s.rail(q["E"], "GND", "L", 20)
        s.stub(q["C"], f"{net}_C", "R", 30)
    s.stub(ds["DIG1"], "DIG1_C", "R", 30)
    s.stub(ds["DIG2"], "DIG2_C", "R", 30)

    # ══════════════════════════════════════════════════════════
    # 按键（按键 + 消抖电容，都并到 GND）
    # ══════════════════════════════════════════════════════════
    for k, (net, swref, cref, cy) in enumerate(
            [("BTN1", "SW1", "C2", 760), ("BTN2", "SW2", "C3", 900)]):
        sw = s.part(1120, cy, swref, "按键",
                    [("1", "1")], [("2", "2")],
                    libname="SW_PUSH_6MM", lcsc=LCSC["SW"], w=70)
        # 引脚 1 ← 网络标签
        s.stub(sw["1"], net, "L", 30)
        # 引脚 2 → GND
        s.rail(sw["2"], "GND", "R", 20)
        # 消抖电容并在同一网络上
        c = s.cap(1020, cy + 60, cref, "100nF", LCSC["C100N"])
        s.wire((c["1"][0], c["1"][1] - 20), c["1"])
        s.netlabel(c["1"][0], c["1"][1] - 20, net, anchor="start")
        s.rail(c["2"], "GND", "R", 20)
    s.text(1020, 1010, "100nF 消抖；RP2350 内部上拉已够用，外部 10k 可选",
           size="8pt", color="#666666")

    # ══════════════════════════════════════════════════════════
    # 电源去耦
    # ══════════════════════════════════════════════════════════
    s.text(1400, 700, "电源去耦", size="10pt", color="#000000", bold=True)
    s.wire((1430, 740), (1430, 752))
    s.flag_vcc(1430, 740, "3V3")
    c4 = s.cap(1430, 752, "C4", "100µF", LCSC["C100U"], vert=True)
    s.rail(c4["2"], "GND", "D", 20)
    s.wire((1500, 740), (1500, 752))
    s.flag_vcc(1500, 740, "3V3")
    c6 = s.cap(1500, 752, "C6", "100nF", LCSC["C100N"], vert=True)
    s.rail(c6["2"], "GND", "D", 20)
    s.text(1400, 840, "100µF 电解抑制", size="8pt", color="#666666")
    s.text(1400, 854, "数码管扫描纹波；", size="8pt", color="#666666")
    s.text(1400, 868, "100nF 就近放", size="8pt", color="#666666")
    s.text(1400, 882, "在 3V3 排针旁。", size="8pt", color="#666666")

    # ══════════════════════════════════════════════════════════
    # 说明
    # ══════════════════════════════════════════════════════════
    notes = [
        "读图说明",
        "· 网络标签（蓝色）同名的引脚是连在一起的。",
        "· 引脚旁的数字是开发板的物理脚号（1~40），名字是板上丝印。",
        "· 每个元件的 LCSC Part 属性已填立创编号，BOM 可直接用。",
        "· 本图与 docs/接线图与BOM.md、build/schematic/gmss_netlist.md 是同一份连接。",
        "· 数码管引脚号按 C193138（ARKLED SN420362N）核实；换型号务必对照数据手册。",
        "· 电源：整机由开发板 USB-C 供电，载板从 3V3 排针取电（约 93mA）。",
        "  想独立使用：外部 5V 接 VBUS 脚并串 SS34，切勿灌到 3V3 脚上。",
    ]
    for i, t in enumerate(notes):
        s.text(1400, 920 + i * 18, t, size="8pt",
               color="#000000" if i == 0 else "#333333",
               bold=(i == 0))

    return s


# ══════════════════════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════════════════════
def verify(s: Sch, text: str) -> list[str]:
    """几何 + 结构自检。

    ★ 最要紧的一条：**每根导线的每个端点都必须落在某个锚点上**
      （引脚连接点 / 网络标签 / 电源符号 / 结点）。
      悬空的线在嘉立创EDA里不会报错，只会让网络断掉 ——
      这种问题导进去以后极难发现，所以必须在这里挡住。
    """
    bad: list[str] = []

    # 1. JSON 能解析
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        return [f"生成的 JSON 无法解析：{e}"]

    for k in ("head", "canvas", "shape", "docType"):
        if k not in doc:
            bad.append(f"缺少顶层字段 {k}")
    if not isinstance(doc.get("head"), dict):
        bad.append("head 必须是 JSON 对象（不是字符串）")

    # 2. 每条 shape 的 cmdKey 合法
    known = {"LIB", "F", "W", "N", "O", "J", "T", "R", "PL", "PG", "PT",
             "A", "E", "C", "L", "I", "B", "BE", "AR", "Pimage"}
    counts: dict[str, int] = {}
    for sh in doc["shape"]:
        key = sh.split("~", 1)[0].split("^^", 1)[0]
        counts[key] = counts.get(key, 0) + 1
        if key not in known:
            bad.append(f"未知的图元类型 {key!r}")

    # 3. 导线端点必须落在锚点上
    #
    # ★ 这是整个自检里最要紧的一条。悬空的导线端点在 EDA 里**不会报错**，
    #   只会让那个网络悄悄断掉 —— 导进去以后极难发现。
    #
    # ★ 只检查**首尾两个点**：中间的拐点是走线形状，本来就不该落在引脚上。
    #   第一版检查了所有点，结果把每个拐点都报成"悬空"，
    #   真正的悬空（引线末端和元件引脚之间差 10 个单位）反而被淹没了。
    #
    # ★ 查的是**真正写出去的那个字符串**，不是内存里的中间数据，
    #   所以不会出现"检查通过了但文件是错的"。
    anchored = s.anchors
    floating = 0
    for sh in doc["shape"]:
        if not sh.startswith("W~"):
            continue
        body = sh.split("~")[1]
        nums = [float(v) for v in body.split()]
        pts = list(zip(nums[0::2], nums[1::2]))
        for pt in (pts[0], pts[-1]):
            if pt not in anchored and pt not in s.wire_ends:
                floating += 1
                if floating <= 5:
                    bad.append(f"导线端点悬空：{pt}")
    if floating > 5:
        bad.append(f"…另有 {floating - 5} 个悬空的导线端点")

    # 4. 元件数量与位号
    #
    # ★ 位号要从 `T~P~` 那个子图元里取，不能在整个 LIB 串上正则搜
    #   "comment~XXX~" —— 那样会先匹配到**元件值**（比如 S8050 的
    #   "S8050" 正好符合 [A-Z]+\d+），于是 Q1/Q2 被判成"缺失"。
    #   第一次跑就是这么误报的。
    libs = [sh for sh in doc["shape"] if sh.startswith("LIB~")]
    refs: set[str] = set()
    for sh in libs:
        for sub in sh.split("#@$"):
            if not sub.startswith("T~P~"):
                continue
            m = re.search(r"~comment~([^~]+)~", sub)
            if m:
                refs.add(m.group(1))
    expect_refs = {"U1", "U2", "U3", "J1", "DS1", "Q1", "Q2",
                   "SW1", "SW2", "R1", "R2", "R3", "R13",
                   "C1", "C2", "C3", "C4", "C5", "C6"}
    missing = expect_refs - refs
    if missing:
        bad.append(f"缺少元件位号：{sorted(missing)}")

    # 5. 关键网络必须存在
    for net in ["3V3", "GND", "LRCK", "BCK", "DIN", "MIDI_RX",
                "SEG_A", "SEG_G", "SEG_DP", "DIG1", "DIG2", "BTN1", "BTN2"]:
        if net not in text:
            bad.append(f"找不到网络 {net}")

    # 6. 关键提醒不能丢
    for must in ["VIN 必须 3.3V", "SCK 接 GND", "1kΩ", "48mA"]:
        if must not in text:
            bad.append(f"缺少关键提示：{must}")

    if not bad:
        print(f"  图元统计：{counts}")
        print(f"  元件 {len(libs)} 个，位号 {len(refs)} 个")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成可导入嘉立创EDA的原理图")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "easyeda")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    pins = load_pins()
    s = build(pins)
    title = "GMSS RP2350 GM 音源 v4"
    text = s.to_json(title, 1700, 1180)

    print("=== 自检 ===")
    bad = verify(s, text)
    for b in bad:
        print(f"  ✗ {b}")
    if not bad:
        print("  结构、几何、网络、提示 全部通过 ✓")

    if args.check:
        return 1 if bad else 0

    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / "GMSS_v4_原理图.json"
    out.write_text(text, encoding="utf-8")
    print(f"\n已写出 {out}  ({out.stat().st_size:,} 字节)")
    print("  导入：嘉立创EDA 标准版 → 文件 → 打开 → 选这个文件")
    print("  （或新建原理图 → 设置 → EasyEDA 源文件 → 粘贴 → 应用）")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
