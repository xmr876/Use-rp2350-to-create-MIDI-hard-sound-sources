"""
gmss/riff.py — 最小 RIFF 容器解析（SoundFont .sf2 的壳）

SF2 结构：
    RIFF <size> 'sfbk'
      LIST <size> 'INFO'   → 文本元数据
      LIST <size> 'sdta'   → 'smpl' 采样数据 (int16 LE)
      LIST <size> 'pdta'   → 9 个 hydra 记录块

只做容器层，不解释内容。
"""
from __future__ import annotations

import struct
from typing import Dict, Iterator, Tuple


class RiffError(ValueError):
    pass


def _read_u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def iter_chunks(data: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """在 [start, end) 内迭代 (chunk_id, body_offset, body_size)。

    RIFF 规定所有 chunk 按偶数字节对齐，奇数长度要补 1 字节。
    """
    off = start
    while off + 8 <= end:
        cid = data[off:off + 4]
        csize = _read_u32(data, off + 4)
        body = off + 8
        if body + csize > end:
            raise RiffError(
                f"chunk {cid!r} 声明大小 {csize} 超出容器边界 "
                f"(body={body}, end={end})，文件可能损坏或截断"
            )
        yield cid, body, csize
        off = body + csize + (csize & 1)


def load(path: str) -> Dict[str, bytes]:
    """读取 .sf2，返回 {'INFO': {...}, 'smpl': bytes, 'pdta': {chunk_id: bytes}}"""
    with open(path, "rb") as f:
        data = f.read()

    if len(data) < 12:
        raise RiffError(f"文件过小 ({len(data)} 字节)，不是有效的 SF2")
    if data[0:4] != b"RIFF":
        raise RiffError(f"缺少 RIFF 头，实际是 {data[0:4]!r}")
    if data[8:12] != b"sfbk":
        raise RiffError(f"不是 SoundFont 文件（期望 'sfbk'，实际 {data[8:12]!r}）")

    riff_size = _read_u32(data, 4)
    riff_end = min(8 + riff_size, len(data))

    info: Dict[str, str] = {}
    smpl: bytes | None = None
    pdta: Dict[bytes, bytes] = {}

    for cid, body, csize in iter_chunks(data, 12, riff_end):
        if cid != b"LIST" or csize < 4:
            continue
        list_type = data[body:body + 4]
        inner_start, inner_end = body + 4, body + csize

        if list_type == b"INFO":
            for iid, ibody, isize in iter_chunks(data, inner_start, inner_end):
                # INFO 内容是零结尾字符串，且可能有填充
                raw = data[ibody:ibody + isize]
                info[iid.decode("latin-1")] = raw.split(b"\x00", 1)[0].decode(
                    "latin-1", errors="replace"
                )

        elif list_type == b"sdta":
            for iid, ibody, isize in iter_chunks(data, inner_start, inner_end):
                if iid == b"smpl":
                    smpl = data[ibody:ibody + isize]
                # 忽略 'sm24'（24bit 扩展，本格式不用）

        elif list_type == b"pdta":
            for iid, ibody, isize in iter_chunks(data, inner_start, inner_end):
                pdta[iid] = data[ibody:ibody + isize]

    if smpl is None:
        raise RiffError("缺少 sdta/smpl 采样数据块")
    if not pdta:
        raise RiffError("缺少 pdta 记录块")

    return {"INFO": info, "smpl": smpl, "pdta": pdta}


# pdta 里应有的 9 个记录块，顺序固定
PDTA_CHUNKS = (b"phdr", b"pbag", b"pmod", b"pgen",
               b"inst", b"ibag", b"imod", b"igen", b"shdr")


def check_pdta(pdta: Dict[bytes, bytes]) -> None:
    missing = [c.decode() for c in PDTA_CHUNKS if c not in pdta]
    if missing:
        raise RiffError(f"pdta 缺少记录块: {', '.join(missing)}")
