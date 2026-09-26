# 固件工程

RP2350 硬 GM 音源固件（第三方 RP2350 开发板 16MB + PCM5102A 模块 + 6N138 MIDI 输入
+ 2 位共阴数码管 + 2 按键）。

---

## ✅ 当前状态

| | |
|---|---|
| **能编译出可烧录的 .uf2** | ✅ 零警告，32KB |
| **整条音频通路已实现** | ✅ 采样解码 → 48 复音 → 包络 → 混音 |
| **主机单元测试** | ✅ 29 项 + 与 Python 参考解码器逐样本对拍 64 条记录 |
| **硬件实测** | ❌ 板子还没做出来 |

```powershell
# 一键构建
powershell -ExecutionPolicy Bypass -File .\build.ps1
# 产物：D:\pico-build\gmss\gmss_synth.uf2
```

**烧录**：按住开发板 BOOTSEL 插 USB → 出现 `RP2350` 盘 → 把 `.uf2` 拖进去。

音色库单独烧（或用 `tools/merge_uf2.py` 合并成一个文件一次烧完）：

```powershell
cd D:\音源\tools\gmss
python -m gmss.build 你的音色库.sf2 --out ..\..\build\font
# 产出 library.uf2，拖进同一个盘
```

---

## 目录结构

```
firmware/
├── include/
│   ├── board_config.h        引脚 / 时钟 / 音频参数的**唯一真源**
│   └── gmss_format.h         板载音色库二进制格式（PC 端共享的契约）
├── src/
│   ├── main.c                双核调度：core0 音频 / core1 MIDI+UI
│   │
│   │  ── 纯逻辑层（不 include 任何 pico-sdk 头，可在 PC 上编译测试）──
│   ├── dsp_core.h/.c         定点 DSP：包络 / 插值 / 一阶滤波 / 总线 / 力度 / dB
│   ├── sampler.h/.c          音色库读取：PCM16 + IMA-ADPCM4 混合解码、关键帧定位
│   ├── voice.h/.c            48 复音管理、音高相位器、循环回绕、混音
│   ├── engine.h/.c           MIDI 语义 → 复音分配（踏板/弯音/CC/鼓通道）
│   │
│   │  ── 硬件层 ──
│   ├── i2s.pio / i2s.h/.c    PIO I2S + DMA 双缓冲
│   ├── midi_in.h/.c          UART1 收字节 + MIDI 解析（running status 等）
│   └── ui.h/.c               数码管扫描（定时器中断）+ 按键消抖
├── tools/hosttest/           主机测试程序（编成 x86-64 跑，不需要硬件）
├── uf2tool.py                裸二进制 → UF2（不依赖 picotool）
├── archive/                  已废弃的实现，保留作设计演进记录
└── build.ps1                 一键构建
```

**★ 分层的意义**：`dsp_core / sampler / voice / engine` 四个模块**一行 pico-sdk
都不 include**，所以整条音频通路能在 PC 上编成 x86-64 跑，并与
`tools/gmss` 的 Python 参考解码器逐样本对拍。这类代码的缺陷大多是
**不崩溃、不报错**的静默错误（解出噪声、循环点咔哒、包络哑音），
在硬件上极难定位 —— 见下面的验证脚本一节。

---

## 验证（不需要开发板）

```powershell
cd D:\音源
python tools\verify_sampler_host.py       # ★ 主机测试 + 逐样本对拍（最重要）
python -m pytest tools\gmss\tests -q      # PC 工具链 38 个单测
python tools\check_struct_sizes.py        # C/Python 结构体 sizeof 对拍
python tools\verify_wiring_doc.py         # 接线文档 vs 固件引脚
python tools\verify_firmware_structure.py # 固件结构/引脚交叉校验
python tools\verify_dsp.py                # DSP 定点算法
python tools\verify_i2s_timing.py         # PIO 时序模型
python tools\verify_i2s_divider.py        # 分频可精确表示性
python tools\verify_qspi_bandwidth.py     # 带宽可行性
python tools\verify_midi.py               # MIDI 解析逻辑
```

### `verify_sampler_host.py` 抓到过的真实缺陷

这个脚本值得单独说：它把 `sampler.c / voice.c / engine.c` 编到主机上跑，
再用 Python 参考实现逐样本比对 + 让编译器开着整数溢出检查。
**它已经抓到三个在硬件上极难定位的缺陷**：

| 缺陷 | 在硬件上的症状 |
|---|---|
| `gmss_stream_seek` 差一帧 | 听不出来，但循环每绕一圈就错开一个采样点，久了会漂 |
| `dsp_env_tick` 起音段整数溢出 | 起音时一声爆音，然后这个音就哑了 |
| `engine_reset` 没杀 voice | 复位/换音色后还有声音拖尾，音量拧到 0 也不停 |

这些都是"不崩溃、不报错"的类型 —— 靠听、靠示波器都很难归因。

---

## 音色库怎么读（v4 方案的核心简化）

音色库和固件住在**同一片 16MB Flash** 上，走 QMI CS0，音色库是
**内存映射**的：

```c
const uint8_t *p = (const uint8_t *)(0x10000000u + zone->sample_off);
```

XIP 缓存（16KB）自动处理。所以**没有** Flash 驱动、**没有** DMA 搬运、
**没有** QMI Direct 模式（也就没有"direct 期间不能执行 flash 代码"那类约束）。

演进过程（每一步放弃的原因都记在 `docs/16MB方案_可行性_BOM_烧录.md`）：

```
v1  裸片 RP2350A + 4 片 QSPI Flash     → QMI Direct 模式约束太多
v2  Pico 2 + 32MB Flash 挂 SPI          → Pico 2 的 QSPI 没引到排针，碰不到 QMI
v3  Pico 2 + 魔改 16MB                  → 要动热风枪
v4  第三方 RP2350 开发板（板载 16MB）    → 最省事，就是现在这个
```

---

## 编译环境（踩过的两个坑）

### 坑 1：工具链必须放在**纯 ASCII 路径**下

`arm-none-eabi-gcc` 用 ANSI 代码页去 stat 自己的库目录，路径里有中文时
找不到 `libgcc`/`libc`，链接报 `cannot find -lgcc`。
而且 `gcc -print-file-name=libgcc.a` 会只回显 `libgcc`（没找到）。

**用 junction/符号链接糊弄没用** —— Windows 会把路径 canonicalize 回中文原路径。

→ 所以工具链放在 `D:\pico-tc\`，**构建目录也必须是 ASCII 路径**。

### 坑 2：picotool 需要有**主机** C++ 编译器

本机只有 ARM 交叉编译器，没有 MSVC/MinGW/clang。所以 `CMakeLists.txt` 里开了
`PICO_NO_PICOTOOL=1`（且必须在 `pico_sdk_init()` **之前**设置，
否则 SDK 照样去找 picotool）。

代价：SDK 不再自动出 `.uf2`，改由 `uf2tool.py` 自己包（UF2 格式很简单：
512 字节块 + 两个魔数）。

主机编译器用 **zig**（`pip install ziglang`）—— 它能当 C/C++ 编译器用，
装起来比 MSVC Build Tools 省事得多。

---

## 关键数字（改代码前先看这张表）

| 项 | 值 | 说明 |
|---|---|---|
| 采样率 | 48000 Hz | 全库统一，固件不做采样率转换 |
| I2S | 32bit/声道，BCLK 3.072MHz | PCM5102A 取 MSB 对齐数据 |
| PIO | 每 bit **10** 个 SM 周期 | 改了 `i2s.pio` 必须同步改 `board_config.h` |
| PIO 分频 | 4 + 226/256（精确） | 48kHz/150MHz 下无抖动 |
| 音频块 | 512 帧 = 10.67ms | 双缓冲；引擎有一个整块的时间算下一块 |
| 复音数 | 48 | 150MHz 下的舒适点 |
| zone 结构体 | **56 字节** | 曾经错写成 58，见下 |
| 关键帧间隔 | 128 帧 | 与 `adpcm.py` 的 `KEYFRAME_INTERVAL` 一致 |
| 固件占用 | ~32KB / 512KB 预留 | 音色库从 flash 0x40000 开始 |

### ★ zone 结构体 58→56 那件事

`gmss_zone_t` 在 C 里是 **56 字节**，而 Python 工具链曾经按 **58 字节**打包
（格式串 `<Bbhh>` 多了一个 int16 填充）。后果是 zone 表**每一条都错位 2 字节**，
固件读到的 `key_lo / sample_off` 全是垃圾 —— 不崩溃、不报错，只是音频完全乱掉。

**它躲过了当时全部 38 个单元测试**，因为那些测试都是 Python↔Python 的自洽检查
（两边用同一个错常量，自然对得上），没有一处问过 C 编译器 `sizeof` 是多少。

现在有两重保险：

- `gmss_format.h` 末尾的 `_Static_assert`（4 个结构体尺寸 + 13 个字段偏移）
- `tools/check_struct_sizes.py`（用 ARM 编译器实测 `sizeof` 与 Python 常量对拍）

---

## 上板后要验证的三件事

1. **I2S 波形**：示波器看 BCK = 3.072MHz、LRCK = 48kHz。
   最可能的坑是 PIO 分频和 32bit 帧的数据对齐（必须 MSB 对齐，
   右对齐的症状是"几乎听不见"）。
2. **MIDI 输入**：31250bps 下光耦上升沿是否干净。
   最可能的坑是上拉用了 10kΩ 而不是 1kΩ。
3. **XIP 读音色库的实际带宽**：设计需求约 1.15MB/s（48 复音满负载），
   需要实测确认缓存不成为瓶颈。

`main.c` 每秒往串口打一行状态，其中 **`欠载` 应该恒为 0** ——
非 0 就说明 core0 被拖住了。
