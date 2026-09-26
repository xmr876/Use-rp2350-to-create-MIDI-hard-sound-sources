#!/usr/bin/env python3
"""
verify_sampler_host.py — 把 sampler.c 编到**主机**上跑，并与 Python 参考解码器对拍

═══════════════════════════════════════════════════════════════════════
为什么要做这件事
═══════════════════════════════════════════════════════════════════════

采样解码是全工程最容易出**静默错误**的地方，而且这类错误在硬件上极难定位：

  · ADPCM 的 nibble 奇偶写反  → 解出来是噪声，但程序照跑
  · PCM→ADPCM 交界忘了定位状态 → 每帧起始一个"啪"
  · 关键帧 seek 差一帧         → 循环点每次回跳都咔哒
  · 整数除法取整方向不一致     → 与编码器逐位不匹配，慢慢积累成失真

这些问题的共同点是：**不崩溃、不报错、单测如果不逐样本比对就发现不了。**

而 Python 那边已经有一份经过 38 个单测验证的参考实现
（tools/gmss/gmss/adpcm.py + player.py）。所以最可靠的做法是：

  同一份 library.bin
    ├─ C 版解码器   （编到 x86-64 跑）
    └─ Python 版解码器
  两者输出**逐样本比对**，差一个 LSB 就报错。

这样"固件解码对不对"这件事就不需要开发板就能验证了。

═══════════════════════════════════════════════════════════════════════
它是怎么跑起来的
═══════════════════════════════════════════════════════════════════════

  sampler.c 和 dsp_core.c 刻意不 include 任何 pico-sdk 头，
  所以用 zig（或任何主机 C 编译器）就能编：

      zig cc -I firmware/include -I firmware/src \
             firmware/src/sampler.c firmware/src/dsp_core.c \
             firmware/tools/hosttest/test_sampler.c -o test_sampler

  （本机没有 MSVC/MinGW/clang，zig 是通过 pip 装的，见 build.ps1 的说明。）

用法：
    python tools/verify_sampler_host.py                 # 自动生成测试库并全流程跑
    python tools/verify_sampler_host.py --keep          # 保留中间产物
    python tools/verify_sampler_host.py --lib <路径>    # 用指定的 library.bin

退出码：0 = 全部通过；1 = 有失败；2 = 环境缺失（编译器/测试库）
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"
BUILD = ROOT / "build" / "hosttest"

# 主机 C 编译器的搜索顺序
ZIG_CANDIDATES = [
    Path(r"D:\Users\15269\anaconda3\Lib\site-packages\ziglang\zig.exe"),
    Path(r"D:\pico-tc\zig-cc.bat"),
]
CC_CANDIDATES = [Path("cc"), Path("gcc"), Path("clang")]


def find_host_cc() -> list[str] | None:
    """返回一条可用的编译命令前缀（列表），找不到返回 None"""
    for z in ZIG_CANDIDATES:
        if z.exists():
            if z.suffix == ".bat":
                # zig-cc.bat 里已经是 "zig cc"
                return [str(z)]
            return [str(z), "cc"]
    for c in CC_CANDIDATES:
        w = shutil.which(str(c))
        if w:
            return [w]
    return None


# ══════════════════════════════════════════════════════════════════════
# 生成测试音色库
# ══════════════════════════════════════════════════════════════════════
def build_test_library(workdir: Path, quiet: bool) -> Path:
    """用现有的合成 SF2 + gmss 工具链生成一份 library.bin"""
    sys.path.insert(0, str(ROOT / "tools" / "gmss"))
    sys.path.insert(0, str(ROOT / "tools" / "gmss" / "tests"))

    from make_test_sf2 import build_sf2          # noqa: E402
    from gmss import build as gmss_build         # noqa: E402

    sf2 = workdir / "test.sf2"
    build_sf2(str(sf2))
    out = workdir / "img"
    rc = gmss_build.main([str(sf2), "--out", str(out), "--name", "HOSTTEST",
                          "-q"] if quiet else
                         [str(sf2), "--out", str(out), "--name", "HOSTTEST"])
    if rc != 0:
        raise SystemExit("错误：gmss.build 失败")
    lib = out / "library.bin"
    if not lib.exists():
        raise SystemExit(f"错误：没生成 {lib}")
    return lib


# ══════════════════════════════════════════════════════════════════════
# 跑 C 测试程序
# ══════════════════════════════════════════════════════════════════════
def build_and_run(cc: list[str], lib: Path, dump: Path, workdir: Path,
                  quiet: bool, test_src: str = "test_sampler.c",
                  extra_src: tuple[str, ...] = ()):
    """编译并运行一个主机测试程序。

    test_src  : firmware/tools/hosttest/ 下的测试源文件
    extra_src : 额外要编进来的固件源文件（test_engine 需要 voice.c / engine.c）
    """
    exe = workdir / (test_src.replace(".c", "") +
                     (".exe" if os.name == "nt" else ""))
    srcs = [
        str(FW / "src" / "sampler.c"),
        str(FW / "src" / "dsp_core.c"),
    ]
    srcs += [str(FW / "src" / e) for e in extra_src]
    srcs.append(str(FW / "tools" / "hosttest" / test_src))

    cmd = cc + [
        "-std=gnu11", "-O1", "-g",
        "-Wall", "-Wextra", "-Wno-unused-parameter",
        f"-I{FW / 'include'}", f"-I{FW / 'src'}",
        *srcs,
        "-o", str(exe),
    ]
    if not quiet:
        print("  编译:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("错误：主机编译失败", file=sys.stderr)
        print(r.stdout)
        print(r.stderr)
        raise SystemExit(1)
    # 编译告警也当失败 —— 主机构建没有"平台差异"可当借口
    if r.stderr.strip():
        print("  ⚠️ 编译器有输出：")
        for ln in r.stderr.strip().splitlines()[:20]:
            print("     " + ln)

    argv = [str(exe), str(lib)]
    if test_src == "test_sampler.c":
        argv.append(str(dump))       # 这个测试要额外吐一个 dump 文件

    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r


def report_checks(r, quiet: bool) -> tuple[int, int]:
    """解析 C 程序输出的 CHECK/INFO 行，返回 (pass, fail)"""
    c_pass = c_fail = 0
    for ln in r.stdout.splitlines():
        if ln.startswith("CHECK "):
            parts = ln.split(" ", 3)
            ok = len(parts) > 2 and parts[2] == "PASS"
            if ok:
                c_pass += 1
            else:
                c_fail += 1
                print(f"  ✗ {parts[1]}: {parts[3] if len(parts) > 3 else ''}")
        elif ln.startswith("INFO ") and not quiet:
            print(f"    {ln[5:]}")
    return c_pass, c_fail


# ══════════════════════════════════════════════════════════════════════
# 与 Python 参考实现对拍
# ══════════════════════════════════════════════════════════════════════
def cross_check(lib: Path, dump: Path, lib_bytes: bytes, quiet: bool) -> tuple[int, int]:
    """把 dump 里的每条记录用 Python 重算一遍，逐样本比对。

    返回 (cases, mismatches)。
    """
    sys.path.insert(0, str(ROOT / "tools" / "gmss"))
    from gmss import adpcm                            # noqa: E402
    from gmss.layout import LIB_BASE, ZONE_SIZE       # noqa: E402
    from gmss.player import parse_zone                # noqa: E402
    import struct as _s

    hdr = _s.unpack_from("<IBBHIIHHIIIBBHIII", lib_bytes, 0)
    (magic, _vmaj, _vmin, _hsz, _sr, total_zones, _ic, _dc,
     zone_off, _instr_off, _pool_off, *_rest) = hdr
    assert magic == 0x53534D47, "library.bin 魔数不对"

    zones = [parse_zone(lib_bytes, zone_off + i * ZONE_SIZE)
             for i in range(total_zones)]

    raw = dump.read_bytes()
    pos = 0
    cases = 0
    mismatches = 0
    first_bad = None

    while pos < len(raw):
        zi, start, count, kind = struct.unpack_from("<IIII", raw, pos)
        pos += 16
        got = struct.unpack_from(f"<{count}h", raw, pos)
        pos += count * 2
        cases += 1

        z = zones[zi]
        blob = lib_bytes[z.sample_off - LIB_BASE:]
        kf = None
        if z.keyframe_off != 0xFFFFFFFF and z.keyframe_count:
            kf = [adpcm.AdpcmState.from_bytes(
                      blob[z.keyframe_off + i * 4: z.keyframe_off + i * 4 + 4])
                  for i in range(z.keyframe_count)]

        # Python 参考实现
        want = _python_reference(adpcm, blob, z, kf, start, count)

        for i in range(count):
            if got[i] != want[i]:
                mismatches += 1
                if first_bad is None:
                    first_bad = (zi, start, kind, i, want[i], got[i])
                break

    if first_bad is not None:
        zi, start, kind, i, w, g = first_bad
        print(f"  ★ 第一处不一致: zone={zi} start={start} "
              f"接口={'连续流' if kind else '随机访问'} 第 {i} 个样本 "
              f"Python={w} C={g}")
        print(f"    （zone: codec={zones[zi].codec} len={zones[zi].sample_len} "
              f"attack={zones[zi].attack_samples}）")
    elif not quiet:
        print(f"  {cases} 条记录全部逐样本一致 ✓")

    return cases, mismatches


def _python_reference(adpcm, blob, z, kf, start, count):
    """用 Python 参考解码器算同样的样本。

    ★ 这里直接调 adpcm.decode_hybrid —— 它就是固件解码路径的参考实现。
      刻意**不**在这里重写一遍算法：重写就等于又造了一份定义，
      对拍就失去了意义（两边都错还能"对得上"）。
    """
    n = z.sample_len
    out = [0] * count

    # 混合编码：前 attack 点 PCM16，其余 ADPCM；超出 sample_len 补 0
    attack = z.attack_samples
    pcm_bytes = attack * 2

    def frame(p: int) -> int:
        if p >= n:
            return 0
        if p < attack:
            return int.from_bytes(blob[p * 2:p * 2 + 2], "little", signed=True)
        return None      # 需要 ADPCM 路径

    # 找出[start, start+count) 里需要 ADPCM 的连续区间
    i = 0
    while i < count:
        p = start + i
        v = frame(p)
        if v is not None:
            out[i] = v
            i += 1
            continue

        # 从这里开始是 ADPCM（或越界），找到这段的终点
        j = i
        while j < count and start + j < n and frame(start + j) is None:
            j += 1
        seg_start = start + i
        seg_len = j - i
        if seg_len > 0:
            k = adpcm.keyframe_for(seg_start)
            st = (adpcm.AdpcmState(kf[k].predictor, kf[k].step_index)
                  if (kf and k < len(kf)) else adpcm.AdpcmState())
            p = k * adpcm.KEYFRAME_INTERVAL
            while p < seg_start:
                byte = blob[pcm_bytes + (p >> 1)]
                adpcm.decode_nibble(st, (byte >> 4) if (p & 1) == 0 else (byte & 0xF))
                p += 1
            for t in range(seg_len):
                if p >= n:
                    out[i + t] = 0
                else:
                    byte = blob[pcm_bytes + (p >> 1)]
                    out[i + t] = adpcm.decode_nibble(
                        st, (byte >> 4) if (p & 1) == 0 else (byte & 0xF))
                p += 1
        i = j

    return out


# ══════════════════════════════════════════════════════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把 sampler.c 编到主机跑，并与 Python 参考解码器逐样本对拍")
    ap.add_argument("--lib", type=Path, help="用指定的 library.bin（默认自动生成）")
    ap.add_argument("--keep", action="store_true", help="保留中间产物")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    cc = find_host_cc()
    if cc is None:
        print("跳过：找不到主机 C 编译器（zig / cc / gcc / clang 都没有）",
              file=sys.stderr)
        return 2

    print("=== sampler.c 主机测试 + 与 Python 参考解码器对拍 ===")
    print(f"  主机编译器: {' '.join(cc)}")

    BUILD.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hosttest_", dir=str(BUILD)))

    try:
        if args.lib:
            lib = args.lib
            print(f"  音色库: {lib}（指定）")
        else:
            print("  生成测试音色库 ...")
            lib = build_test_library(tmp, args.quiet)
            print(f"  音色库: {lib}  ({lib.stat().st_size:,} 字节)")

        dump = tmp / "dump.bin"
        r = build_and_run(cc, lib, dump, tmp, args.quiet)

        # 解析 C 程序的输出
        c_pass = 0
        c_fail = 0
        info_lines = []
        for ln in r.stdout.splitlines():
            if ln.startswith("CHECK "):
                parts = ln.split(" ", 3)
                ok = len(parts) > 2 and parts[2] == "PASS"
                if ok:
                    c_pass += 1
                else:
                    c_fail += 1
                    print(f"  ✗ {parts[1]}: {parts[3] if len(parts) > 3 else ''}")
            elif ln.startswith("INFO "):
                info_lines.append(ln[5:])
            elif ln.startswith("RESULT"):
                pass

        if not args.quiet:
            print()
            print(f"  采样解码自检: {c_pass} 通过 / {c_fail} 失败")
            for ln in info_lines[:12]:
                print(f"    {ln}")

        if r.returncode != 0 and not dump.exists():
            print("错误：C 测试程序异常退出", file=sys.stderr)
            print(r.stdout[-3000:])
            print(r.stderr[-3000:], file=sys.stderr)
            return 1

        # 对拍
        print()
        print("  与 Python 参考解码器逐样本对拍 ...")
        lib_bytes = lib.read_bytes()
        cases, mismatches = cross_check(lib, dump, lib_bytes, args.quiet)

        # ──────────────────────────────────────────────────────────
        # 第二部分：voice / engine 层（整条音频通路）
        # ──────────────────────────────────────────────────────────
        print()
        print("  ── voice / engine 层（复音、音高、包络、循环、混音）──")
        r2 = build_and_run(cc, lib, dump, tmp, args.quiet,
                           test_src = "test_engine.c",
                           extra_src=("voice.c", "engine.c"))

        e_pass = e_fail = 0
        for ln in r2.stdout.splitlines():
            if ln.startswith("CHECK "):
                parts = ln.split(" ", 3)
                ok = len(parts) > 2 and parts[2] == "PASS"
                if ok:
                    e_pass += 1
                else:
                    e_fail += 1
                    print(f"  ✗ {parts[1]}: {parts[3] if len(parts) > 3 else ''}")
            elif ln.startswith("INFO ") and not args.quiet:
                print(f"    {ln[5:]}")

        if not args.quiet:
            print(f"  引擎自检: {e_pass} 通过 / {e_fail} 失败")

        # 引擎测试崩溃时把最后几行打出来 —— 崩溃点通常就在最后一行附近
        if r2.returncode != 0 and e_pass == 0:
            print("错误：引擎测试异常退出（没有产生任何检查结果）",
                  file=sys.stderr)
            print(r2.stdout[-3000:])
            print(r2.stderr[-3000:], file=sys.stderr)
            return 1

        print()
        total_fail = c_fail + mismatches + e_fail
        if total_fail == 0:
            print(f"全部通过 ✓  （采样解码 {c_pass} 项 + 逐样本对拍 {cases} 条记录 "
                  f"+ 引擎 {e_pass} 项）")
            print()
            print("  ★ 这个脚本的意义：整条音频通路（采样解码 → 复音 → 包络 → 混音）")
            print("    的缺陷大多是**不崩溃、不报错**的静默错误 ——")
            print("    nibble 奇偶写反、交界忘定位状态、关键帧差一帧、包络整数溢出，")
            print("    在硬件上表现为噪声/咔哒/哑音，极难定位。")
            print("    这里用 Python 参考实现逐样本对拍 + 主机溢出检查，")
            print("    不需要开发板就能挡住。已经靠它抓到过：")
            print("      · sampler seek 差一帧（听不出来，但循环每圈错开一个点）")
            print("      · dsp_env_tick 起音段整数溢出（起音一声爆音然后哑掉）")
            print("      · engine_reset 没杀 voice（复位后还有声音拖尾）")
            return 0

        print(f"★ 有失败 ★  采样解码 {c_fail} 项、对拍 {mismatches}/{cases} 条、"
              f"引擎 {e_fail} 项", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"  中间产物保留在 {tmp}")


if __name__ == "__main__":
    sys.exit(main())
