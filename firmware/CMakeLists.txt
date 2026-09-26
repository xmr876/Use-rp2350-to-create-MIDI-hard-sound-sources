# ==================================================================
# RP2350 硬 GM 音源固件（Pico 2 模块 + 载板）
# ==================================================================
#
# 构建：
#   cmake -S . -B build -G Ninja -DPICO_BOARD=pico2 -DCMAKE_BUILD_TYPE=Release
#   cmake --build build
#
# ★ -DPICO_BOARD=pico2 必须写。它让 SDK 用 RP2350 的地址映射；
#   写错会用 RP2040 的链接脚本，SRAM 布局完全不同，启动就崩。
#
# ★ 硬件形态：**Pico 2 模块插在自画载板上**，引脚分配见 include/board_config.h。
#   载板额外挂：PCM5102A 模块、32MB SPI Flash（音色库）、2 位共阴数码管、
#   2 按键、MIDI 输入光耦。

cmake_minimum_required(VERSION 3.13)

# 若没设 PICO_SDK_PATH 就尝试从环境变量取
if(NOT DEFINED PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
endif()

if(NOT DEFINED PICO_SDK_PATH)
    message(FATAL_ERROR
        "未找到 pico-sdk。请设置 PICO_SDK_PATH，例如：\n"
        "  Linux/macOS: export PICO_SDK_PATH=/path/to/pico-sdk\n"
        "  Windows:     setx PICO_SDK_PATH \"C:\\path\\to\\pico-sdk\"")
endif()

include(${PICO_SDK_PATH}/external/pico_sdk_import.cmake)

# ------------------------------------------------------------------
# ★★ 必须在 pico_sdk_init() **之前** 设置
# ------------------------------------------------------------------
# pico_sdk_init() 会立刻处理 boot_stage2 等 SDK 子目录，而
# boot_stage2/CMakeLists.txt 里有 pico_add_dis_output()，
# 它会检查 PICO_NO_PICOTOOL 决定要不要去找 picotool。
# 放到 pico_sdk_init() 之后再 set 就**太晚了**（变量还没定义），
# SDK 照样会去下载并构建 picotool —— 而构建它需要有主机 C++ 编译器。
# 症状：configure 阶段报 "No CMAKE_CXX_COMPILER could be found"。
set(PICO_NO_PICOTOOL  1 CACHE BOOL "不构建/不依赖 picotool" FORCE)
set(PICO_NO_COPRO_DIS 1 CACHE BOOL "跳过协处理器反汇编（需要 picotool）" FORCE)

# ------------------------------------------------------------------
# ★★★ 栈大小 —— 这里踩过一个"整块板子没声音"的大坑 ★★★
# ------------------------------------------------------------------
# pico-sdk 的默认值小得惊人：
#     PICO_STACK_SIZE       = 0x800  (2 KB)   ← 主核
#     PICO_CORE1_STACK_SIZE = 0x800  (2 KB)   ← 第二个核
#     （见 pico-sdk/src/rp2_common/pico_platform/include/.../multicore.h）
#
# 而 audio_engine() 里第一行就是：
#     int16_t block[BOARD_AUDIO_BUF_FRAMES * 2];   // 512*2*2 = 2048 字节
#
# **一个数组就把 2 KB 的栈占得一个字节不剩**，后面
# engine_render → voice_render → sampler_read_frames 的调用帧必然踩出去。
#
# 症状非常隐蔽：
#     · 栈溢出 = 硬故障 = 那个核当场死掉
#     · 连一句日志都发不出来（printf 也在栈上）
#     · 主核完全正常（数码管在跑、初始化标记一个个过）
#     · 现象就是"固件一切都好，但 PCM5102 各脚 0V、没声音"
#   现场为此查了很久，最后靠"给音频核加一句启动上报，结果收不到"定位。
#
# 现在给到 16 KB —— 音频通路最深的一条链
# （engine_render → voice_render → sampler_read_frames → ADPCM 解码）
# 留足余量。RP2350 有 520 KB SRAM，这点栈不算什么。
#
# ★ 必须在 pico_sdk_init() **之前**设置，否则链接脚本已经生成完了。
set(PICO_STACK_SIZE        0x4000 CACHE STRING "主核栈 16KB" FORCE)
set(PICO_CORE1_STACK_SIZE  0x4000 CACHE STRING "第二个核栈 16KB" FORCE)

project(gmss_synth C CXX ASM)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

pico_sdk_init()

# ------------------------------------------------------------------
# 编译选项
# ------------------------------------------------------------------
add_compile_options(
    -Wall
    -Wextra
    -Wno-unused-parameter
)

# Release 下也保留断言相关的诊断（可选）
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    add_compile_options(-O2 -ffast-math)
endif()

# ------------------------------------------------------------------
# 源文件
# ------------------------------------------------------------------
set(GMSS_SOURCES
    src/main.c
    src/dsp_core.c
    src/sampler.c        # P3 音色库读取 + PCM16/ADPCM4 解码
    src/voice.c          # P4 48 复音管理与混音
    src/engine.c         # P5 MIDI 语义 → 复音分配
    src/i2s.c
    src/ui.c
    src/midi_in.c        # UART1 收字节 + MIDI 解析
    # 后续会加入：
    # src/storage_usb.c    # USB 刷写音色库 / USB-MIDI 设备
    #
    # ★ 分层原则：sampler.c / voice.c / engine.c / dsp_core.c
    #   **一行 pico-sdk 都不 include**，所以整条音频通路可以在 PC 上
    #   编译成 x86-64 跑单元测试，并与 Python 参考解码器逐样本对拍。
    #   见 tools/verify_sampler_host.py。
    #
    # ★ 只有 i2s.c / ui.c / midi_in.c / main.c 碰硬件。
    #
    # ★ src/flash_direct.c 已移到 firmware/archive/：那是旧的多片 QSPI 方案，
    #   Pico 2 的 QSPI 引脚没引到排针，载板碰不到 QMI 总线，那条路走不通。
)

add_executable(gmss_synth ${GMSS_SOURCES})

# ------------------------------------------------------------------
# PIO 头文件生成
# ------------------------------------------------------------------
# ★ i2s.pio 会被 pioasm 编译成 i2s.pio.h（含指令编码与配置函数）。
#   生成目录必须加到 include 路径里，否则 #include "i2s.pio.h" 找不到。
pico_generate_pio_header(gmss_synth ${CMAKE_CURRENT_LIST_DIR}/src/i2s.pio)

# uart_rx.pio 已停用：MIDI 接收改回硬件 UART（GP11 = UART1 RX）。
# 文件保留在 src/ 里作为记录（它记录了为什么不能用 PIO 在 GP10 上收）。

# ------------------------------------------------------------------
# 包含路径
# ------------------------------------------------------------------
target_include_directories(gmss_synth PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/include
    ${CMAKE_CURRENT_LIST_DIR}/src
)

# ------------------------------------------------------------------
# 链接库
# ------------------------------------------------------------------
target_link_libraries(gmss_synth PRIVATE
    pico_stdlib
    pico_multicore
    hardware_pio
    hardware_dma
    hardware_clocks
    hardware_irq
    hardware_gpio
    hardware_timer
    hardware_spi         # 音色库 Flash（SPI0）
)

# USB：P7 要用来刷音色库 / 当 USB-MIDI 设备，现在先留着
# pico_enable_stdio_usb(gmss_synth 1)
# pico_enable_stdio_uart(gmss_synth 0)

# ------------------------------------------------------------------
# 输出格式
# ------------------------------------------------------------------
# ★★ 为什么不用 picotool（PICO_NO_PICOTOOL 已在上方设好）
# picotool 是一个**主机** C++ 程序，构建它需要有主机编译器
# （MSVC / MinGW / clang），而本机只有 ARM 交叉编译器。
# 关掉之后 SDK 不再下载/构建它，.elf/.bin 照常产出，
# 只是 .uf2 要我们自己包（见下面的 uf2tool.py）。
#
# 顺带也没有了 boot_stage2 的协处理器反汇编 —— 那只影响 .dis 文本，
# 不影响镜像内容的任何一个字节。
pico_add_extra_outputs(gmss_synth)

# ------------------------------------------------------------------
# 用 uf2tool.py 生成 .uf2
# ------------------------------------------------------------------
# pico_add_extra_outputs 里已经挂了 pico_add_bin_output，
# 产出 gmss_synth.bin（就是 flash 从 0x10000000 起的裸镜像）。
find_package(Python3 COMPONENTS Interpreter REQUIRED)

set(GMSS_UF2_FAMILY "rp2350-arm-s")

# ★ 用 $<TARGET_FILE_DIR:...> 加**写死的**文件名，不要用
#   $<TARGET_PROPERTY:gmss_synth,OUTPUT_NAME> —— 那个属性没被设置过，
#   会展开成空串，命令行变成 ".../.bin .../.uf2"，报"找不到 .bin"。
add_custom_command(TARGET gmss_synth POST_BUILD
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/uf2tool.py
            $<TARGET_FILE_DIR:gmss_synth>/gmss_synth.bin
            $<TARGET_FILE_DIR:gmss_synth>/gmss_synth.uf2
            --base 0x10000000
            --family ${GMSS_UF2_FAMILY}
    COMMENT "用 uf2tool.py 生成 .uf2（family=${GMSS_UF2_FAMILY}）"
    VERBATIM
)

# ------------------------------------------------------------------
# 配置摘要
# ------------------------------------------------------------------
message(STATUS "")
message(STATUS "=== GMSS 音源固件配置 ===")
message(STATUS "  PICO_BOARD   : ${PICO_BOARD}")
message(STATUS "  构建类型     : ${CMAKE_BUILD_TYPE}")
message(STATUS "  pico-sdk     : ${PICO_SDK_PATH}")
message(STATUS "  硬件形态     : Pico 2 模块 + 载板（引脚见 include/board_config.h）")
message(STATUS "  picotool     : 已禁用（PICO_NO_PICOTOOL=1），UF2 由 uf2tool.py 生成")
message(STATUS "==========================")
message(STATUS "")

