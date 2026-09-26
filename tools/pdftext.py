#!/usr/bin/env python3
"""
pdftext.py — 纯标准库 PDF 文本提取（不依赖 pypdf / pdfminer）。

只做一件事：把 PDF 里所有 FlateDecode 内容流的文本show操作符抽出来，
按页输出。够用来读说明书、查规格表。

用法：
    python tools/pdftext.py build/ps5_ref.pdf                 # 全部页
    python tools/pdftext.py build/ps5_ref.pdf --page 45       # 单页
    python tools/pdftext.py build/ps5_ref.pdf --grep MIDI     # 只打含关键词的页
"""
from __future__ import annotations

import argparse
import re
import sys
import zlib


def _decode_pdf_string(raw: bytes) -> str:
    """解 PDF 字面字符串 / 十六进制字符串为文本。"""
    if raw.startswith(b"<"):
        hx = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
        if len(hx) % 2:
            hx += b"0"
        data = bytes.fromhex(hx.decode("ascii"))
        # 常见：UTF-16BE BOM
        if data[:2] in (b"\xfe\xff",):
            return data[2:].decode("utf-16-be", "replace")
        return data.decode("latin-1", "replace")

    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):          # backslash
            n = body[i + 1]
            simple = {ord("n"): 10, ord("r"): 13, ord("t"): 9,
                      ord("b"): 8, ord("f"): 12,
                      ord("("): 40, ord(")"): 41, ord("\\"): 92}
            if n in simple:
                out.append(simple[n]); i += 2; continue
            if 0x30 <= n <= 0x37:                     # octal
                j = i + 1
                oct_digits = b""
                while j < len(body) and len(oct_digits) < 3 and 0x30 <= body[j] <= 0x37:
                    oct_digits += bytes([body[j]]); j += 1
                out.append(int(oct_digits, 8) & 0xFF); i = j; continue
            i += 2; continue
        out.append(c); i += 1
    return out.decode("latin-1", "replace")


# 一段内容流里的文本 show 操作符
TOKEN_RE = re.compile(
    rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|\[|\]|TJ|Tj|T\*|Td|TD|ET|BT",
    re.S,
)


def _streams(pdf: bytes) -> list[bytes]:
    """返回所有可解压的流内容。"""
    out = []
    for m in re.finditer(rb"stream\r?\n", pdf):
        start = m.end()
        end = pdf.find(b"endstream", start)
        if end < 0:
            continue
        chunk = pdf[start:end].rstrip(b"\r\n")
        if chunk[:1] == b"\x78":                       # zlib magic
            try:
                out.append(zlib.decompress(chunk))
                continue
            except zlib.error:
                try:
                    out.append(zlib.decompressobj().decompress(chunk))
                    continue
                except zlib.error:
                    pass
        out.append(chunk)
    return out


def extract(pdf: bytes) -> list[str]:
    """逐流提取文本，近似当成逐页。"""
    pages = []
    for st in _streams(pdf):
        if b"Tj" not in st and b"TJ" not in st and b"Td" not in st:
            continue
        buf = []
        for tok in TOKEN_RE.finditer(st):
            t = tok.group(0)
            if t in (b"T*", b"Td", b"TD", b"ET"):
                buf.append("\n")
            elif t.startswith(b"(") or t.startswith(b"<"):
                buf.append(_decode_pdf_string(t))
        text = "".join(buf)
        text = re.sub(r"\n{2,}", "\n", text)
        if text.strip():
            pages.append(text)
    return pages


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="纯标准库 PDF 文本提取")
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, default=None, help="只输出第 N 段（1 起）")
    ap.add_argument("--grep", default=None, help="只输出含该关键词的段（不区分大小写）")
    ap.add_argument("--max-chars", type=int, default=4000, help="每段最多输出字符数")
    args = ap.parse_args(argv)

    data = open(args.pdf, "rb").read()
    pages = extract(data)
    print(f"[{args.pdf}] 抽出 {len(pages)} 段文本", file=sys.stderr)

    key = args.grep.lower() if args.grep else None
    for i, p in enumerate(pages, 1):
        if args.page and i != args.page:
            continue
        if key and key not in p.lower():
            continue
        print(f"\n===== 段 {i} =====")
        print(p[: args.max_chars])
    return 0


if __name__ == "__main__":
    sys.exit(main())
