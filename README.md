# GMSS — RP2350 通用 MIDI 硬音源

> **一块 RP2350 开发板 + 一小片载板，做成一台 128 音色的 General MIDI 硬音源。**
> 插上 MIDI 键盘就能弹，不依赖电脑、不需要驱动。

---

## 这是什么

一台**离线运行的 GM 音源**：

- **128 个旋律音色 + 8 套鼓组**（General MIDI Level 1 全覆盖）
- **6.42 MB 采样库固化在板载 16MB flash** —— 不用 SD 卡、不用电脑
- **48 kHz / 16 bit 输出**，I2S + PCM5102A，线路电平
- **6N138 光耦隔离的 MIDI 输入**，与电脑彻底电气隔离
- **16 复音**
- 开机自动播放测试和弦，板载 LED 指示运行状态

注：根据目前测试，rp2350性能极限大概就在16复音，目前的效果大概就是极限了，新版设计精简了数码管和按键，大大减少了复杂程度

---
## 主要硬件清单

1.一块魔改16m的类pico2 rp2350开发板
2.6n138光耦
3.pcm5102音频模块（tb上紫色的那种）
其他元件详见嘉立创eda工程的bom表

## 快速上手

### 1. 烧录

下载 [`build/release/GMSS_TimGM6mb_一次烧录.uf2`](build/release/)（12.31 MB，**固件 + 音色库**）

1. 按住开发板 **BOOTSEL** 键，插上 USB-C
2. 电脑出现 `RP2350` 盘
3. 把 UF2 拖进去，**等十几秒**（盘自动消失即完成）

### 2. 接线

```
MIDI 键盘/电脑 ──[MIDI 线]──> MIDI IN
                                 │
                          音频线 3.5mm
                                 ↓
                       有源音箱 / 功放 / 调音台
供电：USB-C 5V
```

> ⚠️ 输出是**线路电平**，推不动耳机（32Ω）。要接耳机需外加耳放（如 TDA1308）。

### 3. 开机自检

| 现象 | 含义 |
|---|---|
| **LED 闪烁** | 系统启动，音频核心运行中 |
| **听到 C 大调和弦** | 音色库加载成功、音频通路正常 |

**没有和弦 = 音色库或音频通路有问题。**

---

## ⚠️ 五条硬件要点

> 任一条做错都会导致「完全没声音」或「MIDI 无响应」。
> **都是实际装配调试中踩过的坑。**

### 1. PCM5102A 模块的 `XSMT` 必须接 3.3 V

软静音控制脚。**模块上没有上拉电阻，悬空时电平不定，可能被读成「静音」。**

症状：I2S 时钟全部正常（LRCK 1.6V、BCK 1.0V）、数据也在跑，**但输出一点声音都没有**。

直接短接到 3.3 V 即可（CMOS 输入脚，安全）。

### 3. PCM5102A 的 `SCK` 接 GND（pcb中已设计）

模块内部 PLL 从 BCK 重建主时钟。**SCK 悬空 = 没有主时钟 = 完全不出声。**

---

## 项目结构

```
firmware/              固件（RP2350，C11 + PIO 汇编）
├── include/board_config.h   ★ 单一配置来源（引脚、采样率、复音数…）
├── src/
│   ├── main.c           双核主流程
│   ├── i2s.c/.pio       I2S 输出（PIO + DMA 双缓冲）
│   ├── midi_in.c        MIDI 接收（硬件 UART + 光耦）
│   ├── engine.c         合成引擎（声部分配、CC 处理）
│   ├── voice.c          声部渲染（ADPCM 解码、包络、混音）
│   ├── sampler.c        采样库读取
│   └── ui.c             数码管扫描 + 按键消抖
└── tests/               主机端单元测试

tools/                 构建与验证工具（Python）
├── gmss/                音色库编译工具（SF2 → ADPCM 打包）
├── verify_*.py          7 个固件校验脚本
├── gen_*.py             原理图 / EasyEDA / 接线图生成
├── merge_uf2.py         固件 + 音色库合并成单文件
└── midi_*.py            MIDI 测试工具（定速推送、加速扫频）

docs/                  文档
├── 产品介绍.md
├── 设计文档.md          ★ 从最终 PCB 工程提取
├── 接线图与BOM.md
└── …

build/                 构建输出（大多被 .gitignore 排除）
├── release/            ★ 可烧录固件
└── sf2/                音色库源文件
```

---

## 从源码构建

### 依赖

- `arm-none-eabi-gcc`（或 zig cc 交叉编译）
- `pico-sdk` 2.x
- `cmake` + `ninja`
- Python 3.9+

### 固件

```bash
export PICO_SDK_PATH=/path/to/pico-sdk
export PICO_TOOLCHAIN_PATH=/path/to/arm-gcc

cmake -S firmware -B build -G Ninja \
      -DPICO_BOARD=pico2 \
      -DPICO_FLASH_SIZE_BYTES=16777216 \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

> `PICO_FLASH_SIZE_BYTES=16777216` **必须写**（16MB flash），否则地址检查会误判越界。

### 音色库

```bash
cd tools/gmss
PYTHONPATH=. python -m gmss.build ../../build/sf2/TimGM6mb.sf2 --out ../../build/font_adpcm
```

### 合并成单文件

```bash
python tools/merge_uf2.py build/release/gmss_synth.uf2 \
                           build/font_adpcm/library.uf2 \
                           -o build/release/GMSS_一次烧录.uf2
```

### 验证

```bash
python -m pytest tools/gmss/tests -q
python tools/verify_sampler_host.py --lib build/font_adpcm/library.bin -q
for s in firmware_structure midi i2s_timing i2s_divider dsp qspi_bandwidth; do
    python tools/verify_$s.py
done
```

---

## 调参

**所有参数集中在 [`firmware/include/board_config.h`](firmware/include/board_config.h)：**

| 宏 | 默认 | 作用 |
|---|---|---|
| `BOARD_SAMPLE_RATE` | 48000 | 采样率 |
| `BOARD_MAX_VOICES` | 16 | 复音数 |
| `BOARD_AUDIO_BUF_FRAMES` | 4096 | 音频缓冲 —— **影响延迟** |
| `BOARD_MASTER_VOLUME_PERCENT` | 55 | 主音量（防多声部削顶）|
| `BOARD_HAS_DISPLAY` / `BOARD_HAS_BUTTONS` | 0 | 是否使用数码管/按键 |
| `PIN_*` | — | 全部引脚定义 |

### 关于延迟

**4096 帧缓冲 → MIDI 响应延迟约 85~170 ms，对演奏偏大。**

降到 `1024`（约 43 ms）手感更好，代价是对渲染实时性要求更高。（暂未测试性能是否支持）

---

## 设计要点

### 双核架构

| 核 | 职责 |
|---|---|
| **core 0** | MIDI 解析、按键、显示、串口 —— **可能阻塞的操作全在这里** |
| **core 1** | 音频引擎 —— **绝不阻塞**，按时把采样块推给 I2S |

两核通过**单生产者/单消费者无锁命令队列**通信。
**MIDI 核永远不直接修改声部表** —— 否则会和音频核产生竞争，听感是"一卡一卡"的爆音。

### 音频通路

```
MIDI → 命令队列 → 声部分配 → ADPCM 解码 → 包络 → 混音
     → 双缓冲 → DMA → PIO(I2S) → PCM5102A
```

---

## 已知限制

| 限制 | 说明 |
|---|---|
| **16 复音** | 实测上限约 20，16 留余量。极密集编曲会触发抢声部（音符被截断）|
| **延迟偏大** | 见上文，可调 |
| **无 MIDI THRU** | GP10 空着，可加 |
| **线路电平输出** | 不能直接驱动耳机 |

**性能特征**（实测）：单音、和弦、慢速演奏完全干净；
同时发声 20 个以上开始吃力；高频触发（>30 音/秒）会丢音。
瓶颈在 ADPCM 解码的 flash 随机读 —— 优化方向是**按块解码**（一次解一段到 RAM）。

---

## 音色库

本项目使用 **TimGM6mb** —— 一个 General MIDI 音色库（SoundFont）。

### 为什么不随仓库分发

`TimGM6mb.sf2` 是**第三方作品**，版权归原作者所有。本仓库不分发它，
只分发**已经用它构建好的固件**（在 `build/release/` 里）。

### 下载

| 来源 | 地址 |
|---|---|
| **Debian 软件包**（推荐，最规范）| https://packages.debian.org/timgm6mb-soundfont |
| **Ubuntu 软件包** | https://packages.ubuntu.com/timgm6mb-soundfont |
| MuseScore 内置音色库 | https://musescore.org/zh-hans/用户手册/soundfont-音色库 |

Debian/Ubuntu 上直接 `apt install timgm6mb-soundfont` 即可，
文件会装到 `/usr/share/sounds/sf2/TimGM6mb.sf2`。

**使用前请自行确认该音色库的授权条款**（尤其商用场景）。

### 构建后的库

用本仓库的 `tools/gmss` 编译后得到：

| 项 | 值 |
|---|---|
| 旋律音色 | 128 |
| 鼓组 | 8 |
| 采样区 | 1492 |
| 压缩方式 | IMA/DVI ADPCM 4 bit |
| 大小 | 6.42 MB |
| 校验 | `9496503e` |

**编译命令见上文「从源码构建 → 音色库」。**

> **如果你只是想用这个音源，不需要下载音色库** —— 直接烧
> `build/release/GMSS_TimGM6mb_一次烧录.uf2` 就行，里面已经包含编译好的库。

---

## 许可证

**代码、文档、硬件设计：[MIT](LICENSE)** —— 可自由使用、修改、商用，保留版权声明即可。

覆盖范围：`firmware/`、`tools/`、`docs/`、`hardware/`。

**不覆盖**：音色库 `TimGM6mb.sf2`（第三方作品，版权归原作者，本仓库不分发）。

> ⚠️ **本项目是个人爱好作品，按「现状」提供。**
> 硬件设计未经过安全性、可靠性、电磁兼容性认证，
> **打样、生产、商用前请自行完成必要的验证与合规评估。**

---

## 相关文档

- [产品介绍](docs/产品介绍.md) — 特性、规格、使用说明
- [设计文档](docs/设计文档.md) — BOM、网表、PCB 布局要点、装配检查清单
- [接线图与BOM](docs/接线图与BOM.md)
- [烧录与编程指南](docs/烧录与编程指南.md)

---

## 声明
-硬件设计由本人进行
-设计评估及验证与固件编写均由我们亲爱的蓝色大肥鱼——deepseek进行（吃了7亿token）
-本项目给出的pcb为量产用精简版，后续会补充用于开发测试的带数码管和按键的版本


*本项目在开发过程中定位并修复了 12 个真实缺陷（I2S 计数器、MIDI 引脚、
core1 栈溢出、PIO 阻塞、printf 风暴、DMA 启动时序、跨核数据竞争等），
每一处的原因和排查过程都写在对应源码的注释里。*
