#!/usr/bin/env python3
"""pdfkey_try.py — 暴力试几种 PDF 标准安全处理器变体，找出能解出可读文本的那一种。"""
import hashlib
import re
import struct
import sys

sys.path.insert(0, r"D:\音源\tools")
import pdfdec  # noqa: E402

pdf = open(r"D:\音源\build\ps5_ref.pdf", "rb").read()
ENC_N = 1063
enc = pdfdec._obj_text(pdf, ENC_N)
O = enc[enc.find(b"/O(") + 3 : enc.find(b")/P")]
O = O.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
print("O len:", len(O), O.hex())
ID = re.search(rb"/ID\s*\[\s*<([0-9A-Fa-f]+)>", pdf).group(1)
id0 = bytes.fromhex(ID.decode())
print("id0:", id0.hex())


def key_variant(pad: bytes, perms: int, id_bytes: bytes, md5_rounds: int):
    h = hashlib.md5()
    h.update(pad)
    h.update(O[:32])
    h.update(struct.pack("<i", perms))
    h.update(id_bytes)
    k = h.digest()
    for _ in range(md5_rounds):
        k = hashlib.md5(k[:5]).digest()
    return k[:5]


# 收集一批待解密的字符串
strs = []
for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj", pdf):
    num, gen = int(m.group(1)), int(m.group(2))
    blob = pdf[m.end() : m.end() + 3000]
    for sm in re.finditer(rb"/Producer\s*\(((?:\\.|[^\\()])*)\)", blob):
        strs.append((num, gen, sm.group(1)))
print("候选字符串:", len(strs))

variants = {
    "A_std_PAD": (pdfdec.PAD, 65492, id0, 0),
    "B_nopad": (b"", 65492, id0, 0),
    "C_perm0": (pdfdec.PAD, 0, id0, 0),
    "D_noid": (pdfdec.PAD, 65492, b"", 0),
    "E_50rounds": (pdfdec.PAD, 65492, id0, 50),
    "F_IdSecond": (pdfdec.PAD, 65492, bytes.fromhex(
        re.findall(rb"<([0-9A-Fa-f]+)>", re.search(rb"/ID\s*\[[^\]]*\]", pdf).group(0))[-1].decode()
    ), 0),
}

for name, (pad, perms, idb, rounds) in variants.items():
    key = key_variant(pad, perms, idb, rounds)
    scores = []
    for num, gen, s in strs:
        for use_obj in (True, False):
            k = pdfdec.object_key(key, num, gen, 1) if use_obj else key
            dec = pdfdec.rc4(k, s)
            printable = sum(1 for b in dec if 32 <= b < 127) / max(1, len(dec))
            scores.append((printable, use_obj, num, dec[:30]))
    scores.sort(reverse=True)
    best = scores[0]
    print(f"{name:12s} key={key.hex()}  best printable={best[0]:.2f} objkey={best[1]} obj={best[2]} -> {best[3]!r}")
