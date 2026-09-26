"""
gmss/build.py — 命令行入口：SF2 → 板载镜像

用法：
    python -m gmss.build <soundfont.sf2> --out build/font/ [选项]

选项：
    --no-loop           禁用循环（调试用，所有采样按一次性播放）
    --melodic A,B,...   只保留指定的旋律 program（默认全部 128 个）
    --drums A,B,...     只保留指定的鼓组 program（默认全部）
    --no-adpcm          全部用 PCM16 不压缩（调试用，体积会大很多）
    --name NAME         写入库名的 ASCII 标识（默认 GMSS）
    --zip               打包成单个 .zip 便于传输

★ v2 单片方案：输出单个 library.bin，文件偏移 0 对应 flash 地址 0x40000。
  同时输出 library.uf2，可以直接拖进 RPI-RP2 盘（目标地址已写进 UF2 头）。
"""
from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

import numpy as np

from . import layout, parse, plan


def _parse_prog_list(s: str | None) -> set[int] | None:
    if not s:
        return None
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _fmt_bytes(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gmss.build",
        description="把 .sf2 音色库转换成 RP2350 板载 GMSS 镜像",
    )
    ap.add_argument("sf2", type=Path, help="输入的 .sf2 文件")
    ap.add_argument("--out", type=Path, default=Path("build/font"),
                    help="输出目录（默认 build/font）")
    ap.add_argument("--no-loop", action="store_true", help="禁用循环")
    ap.add_argument("--no-adpcm", action="store_true",
                    help="全部用 PCM16 不压缩（体积约为压缩版的 3.3 倍）")
    ap.add_argument("--melodic", type=str, default=None,
                    help="只保留这些旋律 program，如 0-7,16,40")
    ap.add_argument("--drums", type=str, default=None,
                    help="只保留这些鼓组 program，如 0")
    ap.add_argument("--name", type=str, default="GMSS", help="库名标识")
    ap.add_argument("--zip", action="store_true", help="同时打包成 zip")
    ap.add_argument("-q", "--quiet", action="store_true")

    args = ap.parse_args(argv)

    if not args.sf2.exists():
        print(f"错误：找不到文件 {args.sf2}", file=sys.stderr)
        return 2

    t0 = time.time()
    print(f"读取 {args.sf2} …")
    try:
        sf = parse.load_soundfont(str(args.sf2), verbose=not args.quiet)
    except Exception as e:  # noqa: BLE001
        print(f"错误：解析 SF2 失败：{e}", file=sys.stderr)
        return 1

    print("规划 zone …")
    melodic_progs = _parse_prog_list(args.melodic)
    drum_progs = _parse_prog_list(args.drums)
    mel, drum = plan.plan(
        sf,
        use_loop=not args.no_loop,
        melodic_programs=melodic_progs,
        drum_programs=drum_progs,
    )
    if not mel and not drum:
        print("错误：没有规划出任何 zone。检查 --melodic/--drums 过滤条件，"
              "或该 SF2 是否含 bank 0 的 preset。", file=sys.stderr)
        return 1

    if not args.quiet:
        sm = plan.summarize(mel + drum)
        print(f"  旋律 {len(mel)} zone / 鼓 {len(drum)} zone，"
              f"唯一采样 {sm['unique_samples']}，"
              f"解码后合计 {_fmt_bytes(sm['pcm_bytes'])}，"
              f"其中带循环 {sm['looped']} 条")

    if args.no_adpcm:
        print("  注意：--no-adpcm 已启用，整段无损 PCM16。"
              "正常压缩下体积约为它的 30%（实测约 3.3 倍压缩）")

    print(f"布局到单片 {layout.CHIP_CAPACITY // (1024*1024)}MB flash …")
    try:
        placed = layout.place(mel + drum,
                              verbose=not args.quiet,
                              force_pcm16=args.no_adpcm)
    except layout.LayoutError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    print(f"写出镜像到 {args.out} …")
    try:
        info = layout.write_images(placed, args.out,
                                   build_name=args.name, verbose=not args.quiet)
    except layout.LayoutError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    # UF2：把库镜像包成可直接拖进 RPI-RP2 盘的形式，目标地址 = XIP + LIB_BASE
    lib = (args.out / "library.bin").read_bytes()
    uf2_path = args.out / "library.uf2"
    uf2_bytes = layout.write_uf2(lib, layout.XIP_BASE + layout.LIB_BASE, uf2_path)
    if not args.quiet:
        print(f"    library.uf2    {uf2_bytes / 1e6:7.2f} MB  "
              f"→ 0x{layout.XIP_BASE + layout.LIB_BASE:08X}")

    # 校验和自检：从镜像里按 zone 数 + 128 槽音色表重新定位并算一遍。
    # ★ 不要用 header 里的 instrument_count——那是"实际非空的音色数"，
    #   而音色表固定占 128 个槽（program 直接索引），两者不是一回事。
    lib = (args.out / "library.bin").read_bytes()
    zoff = int.from_bytes(lib[20:24], "little")
    nz = int.from_bytes(lib[12:16], "little")
    ndrum = int.from_bytes(lib[18:20], "little")
    end = (zoff + nz * layout.ZONE_SIZE
           + 128 * layout.INSTR_SIZE
           + ndrum * layout.INSTR_SIZE)
    if end > len(lib):
        print(f"错误：音色表越出 library.bin（{end} > {len(lib)}）", file=sys.stderr)
        return 1
    recalc = layout.fnv1a(lib[zoff:end])
    if recalc != info["checksum"]:
        print(f"错误：校验和不一致（写入 {info['checksum']:08x}，"
              f"回读 {recalc:08x}）", file=sys.stderr)
        return 1

    # 再与工具内部记录的原始字节比对一次，确保磁盘内容 == 内存内容
    if lib[zoff:end] != info["table_bytes"]:
        print("错误：library.bin 的表区与内存中的不一致（写出过程有问题）",
              file=sys.stderr)
        return 1
    print(f"  自检通过：校验和 {info['checksum']:08x}，表区 {len(info['table_bytes'])} 字节")

    print()
    print("完成：")
    for k, v in info["files"].items():
        print(f"  {args.out / k}   {_fmt_bytes(v)}")
    print(f"  {uf2_path}   {_fmt_bytes(uf2_bytes)}")
    print(f"  zone 总数 {info['total_zones']}，"
          f"旋律音色 {info['melodic_programs']}，鼓组 {info['drumkits']}，"
          f"校验和 {info['checksum']:08x}")
    print(f"  池地址 0x{info['pool_addr']:08X}（XIP 0x"
          f"{layout.XIP_BASE + info['pool_addr']:08X}）")
    print(f"  用时 {time.time() - t0:.1f}s")

    if args.zip:
        zpath = args.out.with_suffix(".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(args.out / "library.bin", "library.bin")
            zf.write(uf2_path, "library.uf2")
            zf.write(args.out / "library_map.txt", "library_map.txt")
        print(f"  已打包 {zpath}  {_fmt_bytes(zpath.stat().st_size)}")

    print()
    print("下一步：烧录")
    print(f"  方式 A（推荐，USB 拖拽）：把 {uf2_path.name} 拖进 RP2350 盘（目标地址已写进 UF2 头）")
    print(f"  方式 B（快，需编程器）：用 CH341A/RT809 把 library.bin 写到 flash 偏移 0x"
          f"{layout.LIB_BASE:X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
