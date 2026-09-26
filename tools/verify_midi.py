"""
verify_midi.py — 转写 midi_in.c 的解析逻辑并测试

为什么这样做
------------
解析器有几处容易写错的地方，而且错了不一定立刻可见：
  - running status（连续同类型消息省略状态字节）
  - 力度 0 的 Note On 必须当 Note Off（不处理会"卡音"）
  - 系统消息必须**清除** running status（MIDI 规范）
  - 真时事件（0xF8..0xFF）可插在消息中间，且**不破坏** running status
  - 弯音的 14 位组装顺序（低 7 位在前）

本脚本按 midi_in.c 的状态机逐行转写并测试这些场景。
抓不住的：C 语法、UART 中断、环形缓冲并发。

运行:  python tools/verify_midi.py
"""
from __future__ import annotations

import sys

FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print("  OK   " + msg)
    else:
        FAILS += 1
        print("  FAIL " + msg)


# ==================================================================
# 转写 midi_in.c 的状态机
# ==================================================================
NOTE_OFF, NOTE_ON, POLY_PRESS, CTRL, PROG, CHAN_PRESS, BEND = (
    0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0)
SYSEX_START, SYSEX_END, RT_MIN = 0xF0, 0xF7, 0xF8

EV_NONE, EV_NOTE_ON, EV_NOTE_OFF, EV_PROGRAM, EV_CONTROL = (
    0, 1, 2, 3, 4)
EV_BEND, EV_CHAN_PRESS, EV_POLY_PRESS = 5, 6, 7


def expected_data_bytes(status: int) -> int:
    hi = status & 0xF0
    if hi in (NOTE_OFF, NOTE_ON, POLY_PRESS, CTRL, BEND):
        return 2
    if hi in (PROG, CHAN_PRESS):
        return 1
    return 0


class MidiParser:
    """与 midi_in.c 的 feed_byte() 逐行对应"""

    def __init__(self):
        self.status = 0
        self.data = [0, 0]
        self.ndata = 0
        self.expected = 0
        self.in_sysex = False

    def _make_event(self):
        st = self.status
        ch = st & 0x0F
        hi = st & 0xF0
        if hi == NOTE_ON:
            t = EV_NOTE_OFF if self.data[1] == 0 else EV_NOTE_ON
            return (t, ch, self.data[0], self.data[1], None)
        if hi == NOTE_OFF:
            return (EV_NOTE_OFF, ch, self.data[0], self.data[1], None)
        if hi == CTRL:
            return (EV_CONTROL, ch, self.data[0], self.data[1], None)
        if hi == PROG:
            return (EV_PROGRAM, ch, self.data[0], 0, None)
        if hi == BEND:
            return (EV_BEND, ch, 0, 0, (self.data[1] << 7) | self.data[0])
        if hi == CHAN_PRESS:
            return (EV_CHAN_PRESS, ch, self.data[0], 0, None)
        if hi == POLY_PRESS:
            return (EV_POLY_PRESS, ch, self.data[0], self.data[1], None)
        return None

    def feed(self, b: int):
        """返回事件元组或 None"""
        if b >= RT_MIN:
            return None                     # 真时事件，忽略且不改状态

        if b & 0x80:
            if b == SYSEX_START:
                self.in_sysex = True
                self.ndata = 0
                return None
            if b == SYSEX_END:
                self.in_sysex = False
                self.ndata = 0
                return None
            if b >= 0xF0:
                self.status = 0
                self.expected = 0
                self.ndata = 0
                self.in_sysex = False
                return None
            self.status = b
            self.expected = expected_data_bytes(b)
            self.ndata = 0
            return None

        if self.in_sysex or self.status == 0 or self.expected == 0:
            return None

        self.data[self.ndata] = b & 0x7F
        self.ndata += 1
        if self.ndata < self.expected:
            return None
        self.ndata = 0
        return self._make_event()


# ==================================================================
print("=== 1. 基本消息 ===")
p = MidiParser()
evs = [p.feed(b) for b in (0x90, 60, 100)]
check(evs[0] is None and evs[1] is None, "数据未收满时不产事件")
check(evs[2] == (EV_NOTE_ON, 0, 60, 100, None), "Note On 解析正确")

evs = [p.feed(b) for b in (0x80, 60, 0)]
check(evs[2] == (EV_NOTE_OFF, 0, 60, 0, None), "Note Off 解析正确")

p = MidiParser()
evs = [p.feed(b) for b in (0xC5, 42)]
check(evs[1] == (EV_PROGRAM, 5, 42, 0, None), "1 字节消息（Program Change）")

p = MidiParser()
evs = [p.feed(b) for b in (0xD3, 77)]
check(evs[1] == (EV_CHAN_PRESS, 3, 77, 0, None), "Channel Pressure")

# ==================================================================
print()
print("=== 2. ★ 力度 0 的 Note On 必须当 Note Off ===")
p = MidiParser()
evs = [p.feed(b) for b in (0x90, 60, 0)]
check(evs[2][0] == EV_NOTE_OFF,
      "Note On 力度 0 → Note Off（不处理会卡音）")
check(evs[2][2] == 60, "音符号传递正确")

# ==================================================================
print()
print("=== 3. ★ running status ===")
p = MidiParser()
# 0x90 60 100 | 62 100 | 64 100   —— 后两条省略状态字节
seq = [0x90, 60, 100, 62, 100, 64, 100]
evs = [p.feed(b) for b in seq]
got = [e for e in evs if e]
check(len(got) == 3, "running status 下产出 3 个事件（实际 %d）" % len(got))
check([e[2] for e in got] == [60, 62, 64], "音符号序列正确")

# ==================================================================
print()
print("=== 4. ★ 真时事件插在消息中间，不破坏 running status ===")
p = MidiParser()
# 0x90 60 | 0xF8（时钟）| 100
evs = [p.feed(b) for b in (0x90, 60, 0xF8, 100)]
got = [e for e in evs if e]
check(len(got) == 1 and got[0][2] == 60,
      "0xF8 插在数据字节之间不影响解析")
# running status 仍然有效
evs = [p.feed(b) for b in (62, 100)]
got = [e for e in evs if e]
check(len(got) == 1 and got[0][2] == 62,
      "真时事件后 running status 仍然有效")

# ==================================================================
print()
print("=== 5. ★ 系统消息必须清除 running status（MIDI 规范）===")
p = MidiParser()
p.feed(0x90)
p.feed(60)
p.feed(100)                      # 完成一条 Note On，running status = 0x90
p.feed(0xF1)                     # 系统消息（MTC 四分帧），不支持
# 之后孤立的 62 100 不应被当作 Note On
evs = [p.feed(b) for b in (62, 100)]
check(all(e is None for e in evs),
      "系统消息后孤立数据字节被丢弃（running status 已清除）")

# ==================================================================
print()
print("=== 6. SysEx 内容被忽略，且不影响后续解析 ===")
p = MidiParser()
seq = [0xF0, 0x7E, 0x00, 0x06, 0x01, 0xF7]
evs = [p.feed(b) for b in seq]
check(all(e is None for e in evs), "SysEx 不产出事件")
evs = [p.feed(b) for b in (0x90, 60, 100)]
got = [e for e in evs if e]
check(len(got) == 1 and got[0][0] == EV_NOTE_ON, "SysEx 后能正常解析")

# ==================================================================
print()
print("=== 7. ★ 弯音的 14 位组装（低 7 位在前）===")
p = MidiParser()
# 中心值 8192 = 0x2000 → LSB=0x00, MSB=0x40
evs = [p.feed(b) for b in (0xE0, 0x00, 0x40)]
check(evs[2][4] == 8192, "中心弯音应为 8192（实际 %s）" % evs[2][4])
# 最大值 16383 = 0x3FFF → LSB=0x7F, MSB=0x7F
p = MidiParser()
evs = [p.feed(b) for b in (0xE0, 0x7F, 0x7F)]
check(evs[2][4] == 16383, "最大弯音应为 16383（实际 %s）" % evs[2][4])
# 最小值 0
p = MidiParser()
evs = [p.feed(b) for b in (0xE0, 0x00, 0x00)]
check(evs[2][4] == 0, "最小弯音应为 0")

# ==================================================================
print()
print("=== 8. 通道号提取 ===")
for ch in (0, 5, 9, 15):
    p = MidiParser()
    evs = [p.feed(b) for b in (0x90 | ch, 60, 100)]
    check(evs[2][1] == ch, "通道 %d 提取正确" % ch)

# ==================================================================
print()
print("=== 9. 无 running status 时的孤立数据字节被丢弃 ===")
p = MidiParser()
evs = [p.feed(b) for b in (60, 100, 62, 100)]
check(all(e is None for e in evs), "缺少状态字节时全部丢弃")

# ==================================================================
print()
print("=== 10. 不完整消息后跟新状态字节，不产生垃圾事件 ===")
p = MidiParser()
# 0x90 60（未收满）后紧跟 0x80 状态字节
evs = [p.feed(b) for b in (0x90, 60, 0x80, 60, 0)]
got = [e for e in evs if e]
check(len(got) == 1 and got[0][0] == EV_NOTE_OFF,
      "未完成的消息被新状态字节打断后正确重启")

# ==================================================================
print()
if FAILS:
    print("%d 项未通过" % FAILS)
    sys.exit(1)
print("MIDI 解析逻辑验证通过 ✓")
print()
print("本脚本抓不住的（必须靠编译与硬件）：")
print("  - C 语法、UART 中断注册、环形缓冲的并发正确性")
print("  - UART1 的实际波特率误差")
print("  - 光耦边沿质量（用示波器看 RX 波形）")
