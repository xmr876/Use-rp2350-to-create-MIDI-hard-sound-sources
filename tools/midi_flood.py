"""
让 MIDI 线**持续满负荷活动**，方便用万用表量出稳定的电压。

为什么要这样：
  Program Change 只有 2 个字节，发一次 0.6ms，然后隔 3 秒才发下一次 ——
  占空比不到 0.1%。万用表读平均值，几乎就是 0V，
  **分不清"线没在驱动"和"线在驱动但数据太稀疏"**。

  这个脚本不间断地发 Note On/Off，让线上几乎一直有电流在翻，
  万用表就能读到明确的中间值。

判据（线插在板子 MIDI IN 上、本脚本在跑）：
    DIN 座 pin4 对 pin5 ：应有明显电压（1V 以上，在跳）
    DIN 座 pin4 对 GND  ：应有 2~5V
    DIN 座 pin5 对 GND  ：接近 0V（是回线/地）
    光耦 pin2 对 pin3   ：约 +1.2V（LED 正向压降）
    光耦 pin6 对 GND    ：0~3.3V 之间的中间值（在翻）

全都是 0V 的话 → 线没接对（多半是把线的 MIDI IN 头插上去了），
                  或者线本身是坏的。
"""
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

sys.path.insert(0, r"D:\音源\tools")
from midi_debug_stream import MidiOut, list_devices      # noqa: E402

targets = []
print("=== MIDI 输出设备 ===")
for i, name in list_devices():
    if "Microsoft" in name or "GS Wavetable" in name:
        print(f"  [{i}] {name}   ← 跳过")
        continue
    try:
        targets.append((i, name, MidiOut(i)))
        print(f"  [{i}] {name}   ← 会发")
    except SystemExit as e:
        print(f"  [{i}] {name}   打不开：{e}")

if not targets:
    raise SystemExit("没有可用的 MIDI 输出设备")

print()
print(f"=== 向 {len(targets)} 个设备持续满负荷发送 ===")
print("    音色 0（钢琴），音符在 C3~C5 之间来回跑")
print("    数码管应显示 00；有声音的话会听到快速琶音")
print()

NOTES = [48, 52, 55, 60, 64, 67, 72]


def all_send(status, d1, d2):
    for _, _, m in targets:
        m.send(status, d1, d2)


try:
    # 先定好音色
    for _, _, m in targets:
        m.program(0, 0)
    time.sleep(0.2)

    rnd = 0
    while True:
        rnd += 1
        if rnd % 200 == 0:
            print(f"    …已发 {rnd} 组（数码管应显示 00）", flush=True)
        for n in NOTES:
            all_send(0x90, n, 100)      # Note On
        # 极短间隔 —— 让线上几乎一直有数据
        time.sleep(0.008)
        for n in NOTES:
            all_send(0x80, n, 0)        # Note Off
        time.sleep(0.002)

except KeyboardInterrupt:
    print("\n中断")
finally:
    for _, _, m in targets:
        try:
            m.all_notes_off()
        except Exception:
            pass
        m.close()
    print("已关闭")
