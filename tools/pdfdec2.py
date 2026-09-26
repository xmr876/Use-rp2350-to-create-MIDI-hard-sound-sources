#!/usr/bin/env python3
"""
pdfdec2.py — 正确版：解开 TASCAM PS5 说明书的 PDF 加密并抽取文本。

上一版失败的原因：流边界用 find(b'endstream') 找，加密后的二进制里
也会出现 "endstream" 字样，切出来的密文长度不对，RC4 解出来就是乱码。
这里改成**按 /Length 精确切片**（支持 /Length N 0 R 间接引用）。
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
import zlib

sys.path.insert(0, r"D:\音源\tools")
import pdfdec  # noqa: E402


def resolve_length(pdf: bytes, raw: bytes) -> int | None:
    m = re.search(rb"/Length\s+(\d+)", raw)
    if m:
        return int(m.group(1))
    m = re.search(rb"/Length\s+(\d+)\s+\d+\s+R", raw)
    if m:
        obj = pdfdec._obj_text(pdf, int(m.group(1)))
        if obj:
            mm = re.search(rb"(\d+)", obj)
            if mm:
                return int(mm.group(1))
    return None


def all_objects(pdf: bytes):
    """产出 (num, gen, dict_text, stream_bytes or None)。"""
    for m in re.finditer(rb"(?:^|[\r\n])\s*(\d+)\s+(\d+)\s+obj\b", pdf):
        num, gen = int(m.group(1)), int(m.group(2))
        p = m.end()
        s_idx = pdf.find(b"stream", p)
        e_obj = pdf.find(b"endobj", p)
        if s_idx == -1 or (e_obj != -1 and s_idx > e_obj):
            yield num, gen, pdf[p:e_obj if e_obj != -1 else p + 200], None
            continue
        raw = pdf[p:s_idx]
        start = s_idx + 6
        if pdf[start : start + 2] == b"\r\n":
            start += 2
        elif pdf[start : start + 1] in (b"\n", b"\r"):
            start += 1
        L = resolve_length(pdf, raw)
        if L is None:
            continue
        yield num, gen, raw, pdf[start : start + L]


def extract_text(st: bytes) -> str:
    buf = []
    for tok in re.finditer(
        rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|TJ|Tj|T\*|Td|TD|ET", st, re.S
    ):
        t = tok.group(0)
        if t in (b"T*", b"Td", b"TD", b"ET"):
            buf.append("\n")
        elif t.startswith(b"("):
            body = re.sub(
                rb"\\([nrtbf()\\])",
                lambda x: {
                    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                    b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\",
                }[x.group(1)],
                t[1:-1],
            )
            buf.append(body.decode("latin-1", "replace"))
        elif len(t) > 2:
            hx = re.sub(rb"[^0-9A-Fa-f]", b"", t)
            if len(hx) % 2:
                hx += b"0"
            try:
                buf.append(bytes.fromhex(hx.decode()).decode("latin-1", "replace"))
            except Exception:
                pass
    txt = "".join(buf)
    return re.sub(r"[ \t]{2,}", " ", txt)


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else r"D:\音源\build\ps5_ref.pdf"
    dst = sys.argv[2] if len(sys.argv) > 2 else r"D:\音源\build\ps5_ref_dec.txt"
    pdf = open(src, "rb").read()

    enc_n = int(re.search(rb"/Encrypt\s+(\d+)\s+\d+\s+R", pdf).group(1))
    key, V, R, klen = pdfdec.file_key(pdf, enc_n)
    sys.stderr.write(f"V={V} R={R} key={key.hex()} ({klen}B)\n")

    out, n_text, n_stream = [], 0, 0
    for num, gen, raw, st in all_objects(pdf):
        if st is None:
            continue
        n_stream += 1
        ok = pdfdec.object_key(key, num, gen, V)
        dec = pdfdec.rc4(ok, st)
        if b"FlateDecode" in raw:
            try:
                dec = zlib.decompressobj(15).decompress(dec)
            except zlib.error:
                try:
                    dec = zlib.decompressobj(-15).decompress(dec)
                except zlib.error:
                    continue
        txt = extract_text(dec)
        if len(txt.strip()) > 20 and sum(c.isprintable() or c == "\n" for c in txt) / len(txt) > 0.7:
            n_text += 1
            out.append(f"\n===== obj {num} =====\n{txt}")
    sys.stderr.write(f"扫了 {n_stream} 个流，抽出 {n_text} 段文本\n")
    open(dst, "w", encoding="utf-8", errors="replace").write("\n".join(out))
    sys.stderr.write(f"已写入 {dst}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
