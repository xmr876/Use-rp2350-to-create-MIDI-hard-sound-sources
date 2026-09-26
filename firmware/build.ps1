# ==================================================================
# build.ps1 — 一键构建 GMSS 音源固件
# ==================================================================
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\build.ps1            # 增量构建
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -Clean     # 全清重建
#
# 产物（在 $BuildDir 里）：
#   gmss_synth.uf2    ← 拖到 RP2350 盘符即可烧录
#   gmss_synth.bin    ← 裸二进制，用 picotool / 编程器烧
#   gmss_synth.elf    ← 调试用（SWD + gdb）
#   gmss_synth.map    ← 想看代码占了多少 flash 就看它
#
# ==================================================================
# ★★ 两个必须知道的坑（都是踩出来的，不是理论）
# ==================================================================
#
# 【坑 1】工具链必须放在**纯 ASCII 路径**下
#   arm-none-eabi-gcc 用 ANSI 代码页去 stat 自己的库目录，
#   路径里有中文时它找不到 libgcc/libc，链接报：
#       cannot find -lgcc / -lc
#   而且 `gcc -print-file-name=libgcc.a` 会只回显 "libgcc"（没找到）。
#   用 junction/符号链接糊弄也没用 —— Windows 会把路径 canonicalize 回中文原路径。
#   → 所以工具链放在 D:\pico-tc\，**构建目录也必须是 ASCII 路径**。
#
# 【坑 2】picotool 需要有**主机** C++ 编译器才能构建
#   本机只有 ARM 交叉编译器，没有 MSVC/MinGW/clang。
#   所以 CMakeLists.txt 里开了 PICO_NO_PICOTOOL=1（且必须在 pico_sdk_init()
#   **之前**设置，否则 SDK 照样去找 picotool）。
#   代价：SDK 不再自动出 .uf2，改由 uf2tool.py 自己包（格式很简单）。
#   如果哪天装了主机编译器，把那个开关去掉就能用官方 picotool。
#
# ==================================================================

param(
    [switch]$Clean,
    [string]$BuildType = "Release",
    [string]$Board     = "pico2",
    # ★ 魔改 16MB 之后必须传这个，否则 flash_range_* 的边界检查
    #   会把 4MB 以上的地址判为越界，音色库写不进去。
    [int]$FlashBytes   = 16777216
)

$ErrorActionPreference = "Stop"

# ---------------- 路径（ASCII！见坑 1）----------------
$SdkPath = if ($env:PICO_SDK_PATH)       { $env:PICO_SDK_PATH }       else { "D:\pico-tc\pico-sdk" }
$GccPath = if ($env:PICO_TOOLCHAIN_PATH) { $env:PICO_TOOLCHAIN_PATH } else { "D:\pico-tc\arm-gcc" }
$ZigDir  = "D:\pico-tc"          # 里面放 zig-cc.bat / zig-cxx.bat（主机编译器包装）

$env:PICO_SDK_PATH       = $SdkPath
$env:PICO_TOOLCHAIN_PATH = $GccPath
# CC/CXX 是给 SDK 内部的 pioasm 用的（它是个主机程序，要用主机编译器编）
if (Test-Path "$ZigDir\zig-cc.bat") {
    $env:CC  = "$ZigDir\zig-cc.bat"
    $env:CXX = "$ZigDir\zig-cxx.bat"
}

$PyRoot = Split-Path (Get-Command python).Source -Parent
$env:PATH = "$PyRoot;$PyRoot\Scripts;$ZigDir;$GccPath\bin;$env:PATH"

$Root = Split-Path $MyInvocation.MyCommand.Path -Parent

# ★ 构建目录放在 ASCII 路径下（坑 1）。
#   源码目录本身可以是中文 —— 编译阶段读源码没问题，
#   但生成物落在中文路径下时 objcopy/objdump 会打不开。
$BuildDir = if ($Root -match '^[\x00-\x7F]+$') {
    Join-Path $Root "build"
} else {
    "D:\pico-build\gmss"
}

# ---------------- 前置检查 ----------------
foreach ($p in @($SdkPath, $GccPath)) {
    if (-not (Test-Path $p)) { throw "找不到 $p" }
}
if (-not (Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue)) {
    throw "arm-none-eabi-gcc 不在 PATH 上（检查 $GccPath\bin）"
}

Write-Host "构建目录: $BuildDir" -ForegroundColor DarkGray

if ($Clean -and (Test-Path $BuildDir)) {
    Write-Host "清理 $BuildDir ..." -ForegroundColor Yellow
    Remove-Item $BuildDir -Recurse -Force
}

# ---------------- 配置 ----------------
if (-not (Test-Path (Join-Path $BuildDir "CMakeCache.txt"))) {
    Write-Host "配置 cmake（PICO_BOARD=$Board, flash=$($FlashBytes/1MB)MB, $BuildType）..." -ForegroundColor Cyan
    cmake -S $Root -B $BuildDir -G Ninja `
        -DPICO_BOARD=$Board `
        -DPICO_FLASH_SIZE_BYTES=$FlashBytes `
        -DCMAKE_BUILD_TYPE=$BuildType
    if ($LASTEXITCODE -ne 0) { throw "cmake 配置失败" }
}

# ---------------- 构建 ----------------
Write-Host "编译 ..." -ForegroundColor Cyan
cmake --build $BuildDir
if ($LASTEXITCODE -ne 0) { throw "编译失败" }

# ---------------- 结果 ----------------
Write-Host ""
Write-Host "=== 产物 ===" -ForegroundColor Green
$uf2 = Join-Path $BuildDir "gmss_synth.uf2"
if (Test-Path $uf2) {
    Write-Host ("  {0}   {1:N1} KB" -f $uf2, ((Get-Item $uf2).Length / 1KB))
    Write-Host ""
    Write-Host "烧录：按住 Pico 2 的 BOOTSEL 键插 USB，出现 RP2350 盘后把 .uf2 拖进去" -ForegroundColor Green
} else {
    Write-Host "  没生成 .uf2，检查上面的输出" -ForegroundColor Red
}
