"""
同时往**所有** MIDI 输出设备发信号。

为什么要这样：USB 转 MIDI 线在 Windows 上常常枚举出**两个**输出端口
（"USB2.0-MIDI" 和 "MIDIOUT2 (USB2.0-MIDI)"），到底哪个对应线上那个
丝印为 MIDI OUT 的 DIN 头，各家驱动不一样。只发一个的话，
发错端口 = 板子一个字节都收不到，而现象和"线接反了"一模一样。

这里把每个非微软自带的输出端口都打开、都发，排除掉"发错设备"这个变量。

同时发的内容是固定可预期的：
    Program Change 42 → 数码管应显示 "42"
    Program Change 7  → 数码管应显示 "07"
    Program Change 0  → 数码管应显示 "00"
然后长按一个和弦 8 秒。
"""
import sys
import time

# ★ 不要用 io.TextIOWrapper(sys.stdout.buffer, ...) 重新包一层 ——
#   那样原来的 sys.stdout 会被垃圾回收，底层 buffer 跟着关掉，
#   下一句 print 就报 "I/O operation on closed file"。
#   直接改现有流的编码即可。
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

sys.path.insert(0, r"D:\音源\tools")
from midi_debug_stream import MidiOut, list_devices      # noqa: E402

targets = []
print("=== 可用 MIDI 输出设备 ===")
for i, name in list_devices():
    skip = ("Microsoft" in name) or ("GS Wavetable" in name)
    print(f"  [{i}] {name}{'   ← 跳过（微软自带）' if skip else '   ← 会发'}")
    if skip:
        continue
    try:
        targets.append((i, name, MidiOut(i)))
    except SystemExit as e:
        print(f"        打不开：{e}")

if not targets:
    raise SystemExit("没有可用的 MIDI 输出设备")

print()
print(f"=== 同时向 {len(targets)} 个设备发送 ===")
for i, n, _ in targets:
    print(f"    [{i}] {n}")
print()

CHORD = (48, 55, 60, 64, 67, 72)


def send_all(status, d1, d2):
    for _, _, m in targets:
        m.send(status, d1, d2)


def program(p):
    print(f"  → Program Change {p:3d}   数码管应显示 {p:02d}", flush=True)
    send_all(0xC0, p, 0)


def chord_on(v=95):
    for n in CHORD:
        send_all(0x90, n, v)


def chord_off():
    for n in CHORD:
        send_all(0x80, n, 0)


try:
    rnd = 0
    while True:
        rnd += 1
        print(f"--- 第 {rnd} 轮 ---", flush=True)

        for p in (42, 7, 0):
            program(p)
            # 每个音色弹三下，方便听音色差异
            for _ in range(3):
                for n in (60, 64, 67):
                    send_all(0x90, n, 100)
                time.sleep(1.2)
                for n in (60, 64, 67):
                    send_all(0x80, n, 0)
                time.sleep(0.2)

        print("  → 长按低音和弦 8 秒（拿万用表量光耦用）", flush=True)
        chord_on()
        time.sleep(8.0)
        chord_off()
        time.sleep(0.1)

except KeyboardInterrupt:
    print("\n中断")
finally:
    for _, _, m in targets:
        m.close()
    print("已关闭全部设备")
