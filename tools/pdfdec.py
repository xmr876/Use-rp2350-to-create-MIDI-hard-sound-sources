#!/usr/bin/env python3
"""
pdfdec.py — 解开带"空用户密码"标准加密的 PDF，并抽出文本。

tascam.eu 上的 PS5 说明书 PDF 是加密的（/Encrypt + /Filter/Standard），
所以普通的 zlib 解压拿到的是密文。这里实现 PDF 标准安全处理器
（Algorithm 2，空用户密码，R2/R3 的 RC4 变体），把字符串和流解密出来。

只用标准库：hashlib.md5 + 自写 RC4。

用法：
    python tools/pdfdec.py build/ps5_ref.pdf out.txt
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
import zlib

PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])


def rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % klen]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(len(data))
    i = j = 0
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[n] = b ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def _obj_text(pdf: bytes, num: int) -> bytes | None:
    """取对象 num 的原始字典文本。"""
    pat = rb"(?:^|[\r\n\s])" + str(num).encode() + rb"\s+\d+\s+obj"
    for m in re.finditer(pat, pdf):
        end = pdf.find(b"endobj", m.end())
        return pdf[m.end():end if end > 0 else m.end() + 4096]
    return None


def _dict_ints(d: bytes) -> dict[str, int]:
    out = {}
    for k, v in re.findall(rb"/(\w+)\s+(\d+)", d):
        out[k.decode()] = int(v)
    return out


def _dict_bytes(d: bytes, key: str, n: int) -> bytes:
    m = re.search(rb"/" + key.encode() + rb"\s*<([0-9A-Fa-f]+)>", d)
    if not m:
        m = re.search(rb"/" + key.encode() + rb"\s*\(((?:\\.|[^\\()])*)\)", d)
        if not m:
            raise KeyError(key)
        return m.group(1)
    hx = m.group(1)
    if len(hx) % 2:
        hx += b"0"
    return bytes.fromhex(hx.decode())


def file_key(pdf: bytes, enc_num: int) -> tuple[bytes, int, int, int]:
    enc = _obj_text(pdf, enc_num)
    if enc is None:
        raise SystemExit(f"找不到加密字典对象 {enc_num}")
    di = _dict_ints(enc)
    V = di.get("V", 0)
    R = di.get("R", 2)
    P = di.get("P", -1)
    if P > 0x7FFFFFFF:
        P -= 1 << 32
    length = di.get("Length", 40) // 8
    O = _dict_bytes(enc, "O", 32)
    ID = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", pdf).group(1)
    if len(ID) % 2:
        ID += b"0"
    id0 = bytes.fromhex(ID.decode())

    h = hashlib.md5()
    h.update(PAD)                       # 空用户密码 → 全用 PAD
    h.update(O[:32])
    h.update(struct.pack("<i", P))
    h.update(id0)
    if R >= 4 and enc.find(b"/EncryptMetadata false") >= 0:
        h.update(b"\xff\xff\xff\xff")
    key = h.digest()
    if R >= 3:
        for _ in range(50):
            key = hashlib.md5(key[:length]).digest()
    return key[:length], V, R, length


def object_key(key: bytes, num: int, gen: int, V: int) -> bytes:
    if V >= 5:
        raise SystemExit("AES/V5 加密暂不支持")
    k = key + bytes([num & 0xFF, (num >> 8) & 0xFF,
                     (num >> 16) & 0xFF, gen & 0xFF, (gen >> 8) & 0xFF])
    return hashlib.md5(k).digest()[: min(len(key) + 5, 16)]


DECODE_PARMS = re.compile(rb"/DP\s*<<(.*?)>>", re.S)


def main(argv=None) -> int:
    ap_args = sys.argv[1:] if argv is None else argv
    pdf_path = ap_args[0]
    out_path = ap_args[1] if len(ap_args) > 1 else None
    only = ap_args[2] if len(ap_args) > 2 else None

    pdf = open(pdf_path, "rb").read()
    m = re.search(rb"/Encrypt\s+(\d+)\s+\d+\s+R", pdf)
    if not m:
        raise SystemExit("这个 PDF 没有 /Encrypt")
    enc_num = int(m.group(1))
    key, V, R, length = file_key(pdf, enc_num)
    sys.stderr.write(f"V={V} R={R} keylen={length} bytes\n")

    # 解密所有流
    chunks = []
    for mm in re.finditer(rb"(\d+)\s+(\d+)\s+obj(.*?)stream\r?\n", pdf, re.S):
        num, gen = int(mm.group(1)), int(mm.group(2))
        head = mm.group(3)
        s = mm.end()
        e = pdf.find(b"endstream", s)
        if e < 0:
            continue
        raw = pdf[s:e].rstrip(b"\r\n")
        ok = object_key(key, num, gen, V)
        dec = rc4(ok, raw)
        if b"FlateDecode" in head:
            try:
                dec = zlib.decompressobj(15).decompress(dec)
            except zlib.error:
                try:
                    dec = zlib.decompressobj(-15).decompress(dec)
                except zlib.error:
                    continue
        if only and only.encode() not in dec:
            continue
        if b"Tj" in dec or b"TJ" in dec or b"BT" in dec:
            chunks.append((num, dec))
    sys.stderr.write(f"解出 {len(chunks)} 个含文本的流\n")

    # 从内容流里抓文本
    out = []
    for num, st in chunks:
        buf = []
        for tok in re.finditer(
            rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|TJ|Tj|T\*|Td|TD|ET|BT", st, re.S
        ):
            t = tok.group(0)
            if t in (b"T*", b"Td", b"TD", b"ET"):
                buf.append("\n")
            elif t.startswith(b"("):
                body = t[1:-1]
                body = re.sub(rb"\\([nrtbf()\\])", lambda x: {
                    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                    b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\",
                }[x.group(1)], body)
                buf.append(body.decode("latin-1", "replace"))
            elif t.startswith(b"<") and len(t) > 2:
                hx = re.sub(rb"[^0-9A-Fa-f]", b"", t)
                if len(hx) % 2:
                    hx += b"0"
                try:
                    buf.append(bytes.fromhex(hx.decode()).decode("latin-1", "replace"))
                except Exception:
                    pass
        txt = "".join(buf)
        txt = re.sub(r"[ \t]{2,}", " ", txt)
        if txt.strip():
            out.append(f"\n===== obj {num} =====\n{txt}")

    text = "\n".join(out)
    if out_path:
        open(out_path, "w", encoding="utf-8", errors="replace").write(text)
        sys.stderr.write(f"写入 {out_path}（{len(text)} 字符）\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
