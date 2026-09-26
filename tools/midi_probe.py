"""
发一个**明确知道数码管该显示什么**的测试信号。

用途：把"MIDI 到底通没通"和"显示对不对"一次问清楚。

  先发 Program Change 42  → 数码管**必须**变成 "42"
  再发 Program Change 7   → 数码管**必须**变成 "07"
  然后一直按住一个和弦

三个观察点：
  ① 数码管变 42 了吗？  变了 → MIDI 通的
  ② 数码管变 07 了吗？  变了 → 而且数字是对的，不是乱码
  ③ 有声音吗？          有   → 整条链路打通

每一轮都重新发一次 Program Change，这样即使板子中途重启过也能同步。
"""
import sys
import time
sys.path.insert(0, r'D:\音源\tools')
from midi_debug_stream import MidiOut, pick_device      # noqa: E402

dev, name = pick_device(1)
print(f'设备 [{dev}] {name}', flush=True)
m = MidiOut(dev)

try:
    for rnd in range(1, 999):
        # ---- 阶段 1：程序号 42 ----
        print(f'--- 第 {rnd} 轮 ---', flush=True)
        print('  → Program Change 42    数码管应显示 "42"', flush=True)
        m.program(0, 42)
        for _ in range(3):
            m.note_on(0, 60, 100); m.note_on(0, 64, 100); m.note_on(0, 67, 100)
            time.sleep(1.2)
            m.note_off(0, 60); m.note_off(0, 64); m.note_off(0, 67)
            time.sleep(0.2)

        # ---- 阶段 2：程序号 7 ----
        print('  → Program Change 7     数码管应显示 "07"', flush=True)
        m.program(0, 7)
        for _ in range(3):
            m.note_on(0, 60, 100); m.note_on(0, 64, 100); m.note_on(0, 67, 100)
            time.sleep(1.2)
            m.note_off(0, 60); m.note_off(0, 64); m.note_off(0, 67)
            time.sleep(0.2)

        # ---- 阶段 3：回到钢琴，长音 ----
        print('  → Program Change 0     数码管应显示 "00"，然后长按 8 秒', flush=True)
        m.program(0, 0)
        for n in (48, 55, 60, 64, 67, 72):
            m.note_on(0, n, 95)
        time.sleep(8.0)
        for n in (48, 55, 60, 64, 67, 72):
            m.note_off(0, n)
        time.sleep(0.1)
except KeyboardInterrupt:
    pass
finally:
    m.close()
    print('已关闭', flush=True)
