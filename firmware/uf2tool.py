#!/usr/bin/env python3
"""
uf2tool.py — 把裸二进制包成 UF2（RP2350 / Pico 2 用）

为什么自己写而不用 picotool：
  picotool 是个 C++ 主机程序，需要有主机编译器才能构建。
  而这台机器上只有 ARM 交叉编译器（arm-none-eabi-gcc），没有 MSVC/MinGW/clang。
  UF2 的格式其实极简单（512 字节块 + 两个魔数），自己包更可控，
  也顺便去掉了构建链上最脆弱的一环。

用法：
    python uf2tool.py <input.bin> <output.uf2> [--base 0x10000000] [--family rp2350-arm-s]
    python uf2tool.py --info <file.uf2>        # 打印 UF2 的信息，用于校验

★ 目标地址是 **XIP 视角的地址**（0x10000000 起），不是 flash 偏移。
  bootrom 会自己减掉 XIP_BASE 再写到 flash 上。
"""

import argparse
import struct
import sys
from pathlib import Path

# ---- UF2 常量（全部来自 pico-sdk 的 boot/uf2.h，不是猜的）----
MAGIC_START0 = 0x0A324655
MAGIC_START1 = 0x9E5D5157
MAGIC_END    = 0x0AB16F30
FLAG_FAMILY_ID_PRESENT = 0x00002000

FAMILIES = {
    "rp2040":         0xE48BFF56,
    "absolute":       0xE48BFF57,
    "data":           0xE48BFF58,
    "rp2350-arm-s":   0xE48BFF59,   # ★ Pico 2 默认（Arm 安全模式）
    "rp2350-riscv":   0xE48BFF5A,
    "rp2350-arm-ns":  0xE48BFF5B,
}

XIP_BASE = 0x10000000
BLOCK_SIZE = 512
MAX_PAYLOAD = 476


def make_uf2(data: bytes, target_addr: int, family: int, payload: int = 256) -> bytes:
    """把 data 包成 UF2。payload 默认 256 字节（= flash 页大小）。

    ★ 为什么用 256 而不是 476：
      flash 的页是 256 字节，按页对齐的块让 bootrom 的擦写路径最简单，
      也是 picotool 的默认值。476 虽然更省空间，但没有实际好处。
    """
    if payload > MAX_PAYLOAD:
        raise ValueError(f"单块数据不能超过 {MAX_PAYLOAD} 字节，给了 {payload}")
    if payload & (payload - 1):
        raise ValueError("payload 必须是 2 的幂（否则块地址不对齐）")
    if target_addr % payload:
        raise ValueError(
            f"目标地址 0x{target_addr:X} 不是 {payload} 的整数倍，"
            "会导致块跨页")

    nblocks = (len(data) + payload - 1) // payload
    out = bytearray()
    for i in range(nblocks):
        chunk = data[i * payload:(i + 1) * payload]
        chunk += b"\xFF" * (payload - len(chunk))
        blk = bytearray(BLOCK_SIZE)
        struct.pack_into(
            "<IIIIIIII", blk, 0,
            MAGIC_START0, MAGIC_START1,
            FLAG_FAMILY_ID_PRESENT,
            target_addr + i * payload,
            payload, i, nblocks, family)
        blk[32:32 + payload] = chunk
        struct.pack_into("<I", blk, 508, MAGIC_END)
        out += blk
    return bytes(out)


def parse_uf2(raw: bytes) -> dict:
    """解析 UF2，返回统计信息；格式不对直接抛异常。"""
    if len(raw) % BLOCK_SIZE:
        raise ValueError(f"文件长度 {len(raw)} 不是 512 的整数倍，不是合法 UF2")
    n = len(raw) // BLOCK_SIZE
    fams, addrs, payloads, nums = set(), [], set(), []
    for i in range(n):
        blk = raw[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
        m0, m1, flags, addr, plen, bno, btot, fam = struct.unpack_from("<IIIIIIII", blk, 0)
        if m0 != MAGIC_START0 or m1 != MAGIC_START1:
            raise ValueError(f"第 {i} 块起始魔数错")
        if struct.unpack_from("<I", blk, 508)[0] != MAGIC_END:
            raise ValueError(f"第 {i} 块结束魔数错")
        if (flags & FLAG_FAMILY_ID_PRESENT) == 0:
            raise ValueError(f"第 {i} 块没有带 familyID，bootrom 会拒绝")
        fams.add(fam)
        addrs.append(addr)
        payloads.add(plen)
        nums.append((bno, btot))
    return {
        "blocks": n,
        "familes": fams,
        "payload_sizes": payloads,
        "first_addr": min(addrs),
        "last_addr": max(addrs),
        "block_numbers_ok": all(b == i and t == n for i, (b, t) in enumerate(nums)),
    }


def _fmt_addr(a: int) -> str:
    return f"0x{a:08X}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="裸二进制 → UF2（RP2040/RP2350）")
    ap.add_argument("input", type=Path, nargs="?", help="输入 .bin")
    ap.add_argument("output", type=Path, nargs="?", help="输出 .uf2")
    ap.add_argument("--base", type=str, default=hex(XIP_BASE),
                    help=f"目标地址，默认 {hex(XIP_BASE)}（XIP 基址）")
    ap.add_argument("--family", type=str, default="rp2350-arm-s",
                    choices=sorted(FAMILIES), help="UF2 家族 ID")
    ap.add_argument("--payload", type=int, default=256, help="每块数据字节数（默认 256）")
    ap.add_argument("--info", type=Path, help="只解析并打印一个 UF2 的信息")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.info:
        raw = args.info.read_bytes()
        info = parse_uf2(raw)
        print(f"{args.info}:")
        print(f"  块数        {info['blocks']}")
        print(f"  载荷大小    {sorted(info['payload_sizes'])}")
        print(f"  地址范围    {_fmt_addr(info['first_addr'])} .. "
              f"{_fmt_addr(info['last_addr'])}")
        print(f"  家族 ID     " + ", ".join(f"0x{f:08X}" for f in info["familes"]))
        print(f"  块号连续    {'是' if info['block_numbers_ok'] else '否 ← 有问题'}")
        return 0

    if not args.input or not args.output:
        ap.error("需要 input 和 output（或用 --info）")
    if not args.input.exists():
        print(f"错误：找不到 {args.input}", file=sys.stderr)
        return 2

    data = args.input.read_bytes()
    base = int(args.base, 0)
    fam = FAMILIES[args.family]

    uf2 = make_uf2(data, base, fam, args.payload)
    args.output.write_bytes(uf2)

    if not args.quiet:
        print(f"  {args.input.name}  {len(data):,} 字节")
        print(f"  → {args.output}  {len(uf2):,} 字节")
        print(f"  目标地址 {_fmt_addr(base)} .. {_fmt_addr(base + len(data))}")
        print(f"  家族 ID  0x{fam:08X} ({args.family})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
