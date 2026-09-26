#!/usr/bin/env python3
"""
merge_uf2.py — 把多个 UF2 合并成一个

用途：16MB 方案里固件在 flash 0x000000、音色库在 0x040000，两段不重叠，
      合并成一个 UF2 后**一次拖进 RP2350 盘**就都写好了，不用分两次进 BOOTSEL。

UF2 格式本身就是一串 (目标地址, 数据) 块，bootrom 会逐块写入、
全部写完后才重启，所以简单拼接在语义上就是对的。
但仍然要做校验 —— 拼接两个"各自合法"的文件不保证"合并后合法"：

  · 地址区间不能重叠（固件区和音色库区重叠 = 后写的覆盖先写的，
    而且顺序还不确定 → 必须直接报错，不能静默）
  · 每个文件的块号必须从 0 连续到 n-1（拼接后要重编号）
  · familyID 必须一致（固件是 rp2350-arm-s，音色库也必须是同一个）

用法：
    python merge_uf2.py fw.uf2 lib.uf2 -o all.uf2
    python merge_uf2.py fw.uf2 -o all.uf2 --info
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

MAGIC_START0 = 0x0A324655
MAGIC_START1 = 0x9E5D5157
MAGIC_END = 0x0AB16F30
FLAG_FAMILY_ID = 0x00002000
BLOCK = 512

FAMILY_NAMES = {
    0xE48BFF56: "rp2040",
    0xE48BFF57: "absolute",
    0xE48BFF58: "data",
    0xE48BFF59: "rp2350-arm-s",
    0xE48BFF5A: "rp2350-riscv",
    0xE48BFF5B: "rp2350-arm-ns",
}


class Uf2Error(RuntimeError):
    pass


def read_uf2(path: Path):
    """读一个 UF2，返回 (块列表, family)。块列表元素是 (addr, payload_bytes)。"""
    raw = path.read_bytes()
    if len(raw) == 0:
        raise Uf2Error(f"{path.name} 是空文件")
    if len(raw) % BLOCK:
        raise Uf2Error(f"{path.name} 长度 {len(raw)} 不是 512 的整数倍，不是合法 UF2")

    n = len(raw) // BLOCK
    blocks = []
    fams = set()
    seen = set()

    for i in range(n):
        b = raw[i * BLOCK:(i + 1) * BLOCK]
        m0, m1, flags, addr, plen, bno, btot, fam = struct.unpack_from("<IIIIIIII", b, 0)
        if m0 != MAGIC_START0 or m1 != MAGIC_START1:
            raise Uf2Error(f"{path.name} 第 {i} 块起始魔数错")
        if struct.unpack_from("<I", b, 508)[0] != MAGIC_END:
            raise Uf2Error(f"{path.name} 第 {i} 块结束魔数错")
        if not (flags & FLAG_FAMILY_ID):
            raise Uf2Error(f"{path.name} 第 {i} 块没带 familyID，bootrom 会拒绝")
        if plen == 0 or plen > 476:
            raise Uf2Error(f"{path.name} 第 {i} 块 payloadSize={plen} 非法")
        if bno != i or btot != n:
            raise Uf2Error(
                f"{path.name} 第 {i} 块的块号字段是 ({bno}/{btot})，"
                f"与按顺序数出来的 ({i}/{n}) 不符 —— 文件可能被改过或截断过")
        if addr in seen:
            raise Uf2Error(f"{path.name} 地址 0x{addr:08X} 出现两次（块重叠）")
        seen.add(addr)
        fams.add(fam)
        blocks.append((addr, b[32:32 + plen]))

    if len(fams) != 1:
        raise Uf2Error(f"{path.name} 里混了多个 familyID: "
                       + ", ".join(f"0x{f:08X}" for f in fams))
    return blocks, fams.pop()


def envelope(a: int, b: int, plen: int) -> tuple[int, int]:
    """算出块覆盖的地址区间 [start, end)"""
    return a, a + plen


def merge(paths, out: Path, payload: int = 256, quiet: bool = False) -> dict:
    all_blocks = []
    family = None
    per_file = []

    for p in paths:
        blocks, fam = read_uf2(p)
        if family is None:
            family = fam
        elif fam != family:
            raise Uf2Error(
                f"{p.name} 的 familyID 0x{fam:08X} 与前面文件的 "
                f"0x{family:08X} 不一致 —— 不能合并")
        lo = min(a for a, _ in blocks)
        hi = max(a + len(d) for a, d in blocks)
        per_file.append((p.name, lo, hi, len(blocks)))
        all_blocks.extend(blocks)

    # 排序后检查区间重叠
    all_blocks.sort(key=lambda x: x[0])
    prev_end = -1
    prev_addr = -1
    for addr, data in all_blocks:
        if addr < prev_end:
            raise Uf2Error(
                f"地址区间重叠：0x{prev_addr:08X} 起的块延伸到 0x{prev_end:08X}，"
                f"而 0x{addr:08X} 又有一块。固件区和音色库区的偏移需要错开。")
        prev_end = addr + len(data)
        prev_addr = addr

    # 重新编号并输出
    n = len(all_blocks)
    buf = bytearray()
    for i, (addr, data) in enumerate(all_blocks):
        blk = bytearray(BLOCK)
        struct.pack_into("<IIIIIIII", blk, 0,
                         MAGIC_START0, MAGIC_START1,
                         FLAG_FAMILY_ID, addr, len(data), i, n, family)
        blk[32:32 + len(data)] = data
        struct.pack_into("<I", blk, 508, MAGIC_END)
        buf += blk

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(buf))

    fam_name = FAMILY_NAMES.get(family, "未知")
    if not quiet:
        for name, lo, hi, cnt in per_file:
            print(f"  {name:24s} {cnt:6d} 块  0x{lo:08X} .. 0x{hi:08X}")
        total_lo = min(a for a, _ in all_blocks)
        total_hi = max(a + len(d) for a, d in all_blocks)
        print(f"  {'合并后':24s} {n:6d} 块  0x{total_lo:08X} .. 0x{total_hi:08X}")
        print(f"  → {out}  {len(buf):,} 字节  family={fam_name} (0x{family:08X})")
        print(f"  覆盖 {total_hi - total_lo:,} 字节的 flash 空间")

    return {
        "blocks": n,
        "family": family,
        "lo": min(a for a, _ in all_blocks),
        "hi": max(a + len(d) for a, d in all_blocks),
        "bytes": len(buf),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="合并多个 UF2 成一个")
    ap.add_argument("inputs", type=Path, nargs="+", help="要合并的 .uf2，按地址自然排序")
    ap.add_argument("-o", "--out", type=Path, help="输出 .uf2（--info 时不需要）")
    ap.add_argument("--info", action="store_true", help="只解析输入，不写输出")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.info:
        for p in args.inputs:
            blocks, fam = read_uf2(p)
            lo = min(a for a, _ in blocks)
            hi = max(a + len(d) for a, d in blocks)
            print(f"{p}:")
            print(f"  块数 {len(blocks)}  地址 0x{lo:08X} .. 0x{hi:08X}  "
                  f"family 0x{fam:08X} ({FAMILY_NAMES.get(fam, '?')})")
        return 0

    if not args.out:
        ap.error("需要 -o/--out（或加 --info 只看信息）")

    try:
        merge(args.inputs, args.out, quiet=args.quiet)
    except Uf2Error as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
