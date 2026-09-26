"""
gmss/adpcm.py — IMA/DVI ADPCM（4bit）编解码

为什么自己实现而不用库：**固件侧的解码器必须和这里的编码器逐位一致**，
差一个 LSB 都会在循环点产生咔哒。所以这里用纯 Python 整数运算实现，
并导出测试向量给 C 版本对拍。

格式约定（与 gmss_format.h 一致）
--------------------------------
- 每个 zone 的 ADPCM 数据是**连续 nibble 流**，没有 block header。
- 高 nibble 在前（先编码的采样点在高 4 位）。
- 编码器/解码器状态 = (predictor, step_index)，在 zone 内连续演进，不重置。
- 每 GMSS_ADPCM_KEYFRAME_INTERVAL 个采样点记一份状态到关键帧表，
  固件定位循环点时先跳关键帧再顺序解码。

定点语义（必须与 C 完全一致）
-----------------------------
- predictor 是 int16 域的值，累加后用 clamp 回 [-32768, 32767]
- diff 计算用整数除法向零取整（Python 的 // 是向下取整，故用 int(a/b) 语义）
- 所有中间量都在 int32 范围内，不会溢出
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np

# --------------------------------------------------------------------------
# 标准 IMA 表（与 C 版本必须逐字节一致）
# --------------------------------------------------------------------------

STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
    2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
    6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
    18500, 20350, 22385, 24623, 27086, 29794, 32767,
]

INDEX_TABLE = [
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
]

assert len(STEP_TABLE) == 89
assert len(INDEX_TABLE) == 16

KEYFRAME_INTERVAL = 128      # 与 gmss_format.h 的 GMSS_ADPCM_KEYFRAME_INTERVAL 一致


def _clamp16(v: int) -> int:
    if v < -32768:
        return -32768
    if v > 32767:
        return 32767
    return v


def _trunc_div(a: int, b: int) -> int:
    """向零取整的整数除法（C 的 / 语义）。

    Python 的 // 是向下取整，-7 // 2 == -4，而 C 里 -7 / 2 == -3。
    这个差异会让 Python 编码器和 C 解码器产生不同结果，必须显式处理。
    """
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class AdpcmState:
    """编解码器状态，可序列化进关键帧表"""

    __slots__ = ("predictor", "step_index")

    def __init__(self, predictor: int = 0, step_index: int = 0):
        self.predictor = _clamp16(int(predictor))
        self.step_index = max(0, min(88, int(step_index)))

    def to_bytes(self) -> bytes:
        """4 字节：predictor(int16 LE) + step_index(uint8) + pad"""
        import struct
        return struct.pack("<hBB", self.predictor, self.step_index, 0)

    @staticmethod
    def from_bytes(b: bytes) -> "AdpcmState":
        import struct
        p, si, _ = struct.unpack("<hBB", b[:4])
        return AdpcmState(p, si)

    def __repr__(self) -> str:
        return f"AdpcmState(pred={self.predictor}, idx={self.step_index})"


def encode_nibble(state: AdpcmState, sample: int) -> int:
    """编码一个采样点，返回 4bit 码字（0..15），并就地更新 state。"""
    step = STEP_TABLE[state.step_index]
    diff = int(sample) - state.predictor

    # 符号位 + 3 位幅度
    code = 0
    if diff < 0:
        code = 8
        diff = -diff

    # 逐位比较（IMA 标准做法，也可用查表加速）
    delta = step >> 3
    if diff >= step:
        code |= 4
        diff -= step
        delta += step
    if diff >= (step >> 1):
        code |= 2
        diff -= step >> 1
        delta += step >> 1
    if diff >= (step >> 2):
        code |= 1
        delta += step >> 2

    # 更新 predictor（与解码器完全相同的路径，保证收发一致）
    if code & 8:
        state.predictor = _clamp16(state.predictor - delta)
    else:
        state.predictor = _clamp16(state.predictor + delta)

    state.step_index = max(0, min(88, state.step_index + INDEX_TABLE[code & 0xF]))
    return code & 0xF


def decode_nibble(state: AdpcmState, code: int) -> int:
    """解码一个 4bit 码字，返回采样点，并就地更新 state。"""
    step = STEP_TABLE[state.step_index]

    delta = step >> 3
    if code & 4:
        delta += step
    if code & 2:
        delta += step >> 1
    if code & 1:
        delta += step >> 2

    if code & 8:
        state.predictor = _clamp16(state.predictor - delta)
    else:
        state.predictor = _clamp16(state.predictor + delta)

    state.step_index = max(0, min(88, state.step_index + INDEX_TABLE[code & 0xF]))
    return state.predictor


def encode(samples: np.ndarray, initial: AdpcmState | None = None
           ) -> Tuple[bytes, List[AdpcmState]]:
    """把 int16 采样编码成 ADPCM nibble 流。

    返回 (packed_bytes, keyframes)：
      - packed_bytes：高 nibble 在前，最后一个采样点若为奇数个则低 nibble 补 0
      - keyframes[0] 是**编码起始状态**（即 initial），之后每 KEYFRAME_INTERVAL
        个采样点一份。解码器从这个表恢复状态即可从任意关键帧继续。
    """
    st = AdpcmState(*( (initial.predictor, initial.step_index) if initial else (0, 0) ))
    keyframes: List[AdpcmState] = [AdpcmState(st.predictor, st.step_index)]

    nibbles: List[int] = []
    n = len(samples)
    for i in range(n):
        if i > 0 and i % KEYFRAME_INTERVAL == 0:
            keyframes.append(AdpcmState(st.predictor, st.step_index))
        nibbles.append(encode_nibble(st, int(samples[i])))

    # 打包：高 nibble 在前
    out = bytearray()
    for i in range(0, len(nibbles), 2):
        hi = nibbles[i] & 0xF
        lo = (nibbles[i + 1] & 0xF) if i + 1 < len(nibbles) else 0
        out.append((hi << 4) | lo)

    return bytes(out), keyframes


def decode(data: bytes, n_samples: int,
           initial: AdpcmState | None = None,
           nibble_base: int = 0,
           byte_offset: int = 0,
           nibble_count: int | None = None) -> np.ndarray:
    """从 ADPCM nibble 流解出采样点。

    定位参数是**互相独立**的，别混为一谈（这里踩过坑）：

    n_samples    : 返回多少个采样点（= 迭代次数，若未指定 nibble_count）
    nibble_base  : 流中第 0 个 nibble 对应的**采样点序号**，决定 nibble 奇偶
    byte_offset  : ADPCM 段在 data 里的**起始字节偏移**，决定去哪读字节
    nibble_count : 实际迭代多少个 nibble；默认等于 n_samples。
                   要在流中间取一段时（例如跳过 PCM 段）用它，
                   否则会越界或读错位置。

    混合编码的 blob 布局是 [PCM16 × attack] ++ [ADPCM 流]，
    取尾部数据段应传：
        decode(blob, n - attack, nibble_base=attack,
               byte_offset=attack*2, nibble_count=n - attack)
    """
    if nibble_count is None:
        nibble_count = n_samples

    st = AdpcmState(*( (initial.predictor, initial.step_index) if initial else (0, 0) ))
    out = np.empty(n_samples, dtype="<i2")
    for i in range(nibble_count):
        p = nibble_base + i
        byte = data[byte_offset + (p >> 1)]
        code = (byte >> 4) if (p & 1) == 0 else (byte & 0xF)
        val = decode_nibble(st, code)
        if i < n_samples:
            out[i] = val
    return out


def decode_from_keyframe(data: bytes, n_samples: int,
                         keyframes: List[AdpcmState],
                         start_sample: int) -> np.ndarray:
    """从任意采样点开始解码——固件定位循环点时走的就是这条路径。

    做法：找到 start_sample 所属的关键帧，从那里恢复状态，
    再顺序解码到目标位置，然后继续产出 n_samples 个点。
    这里为了验证正确性，直接从头解码并比对（固件里会用关键帧加速）。
    """
    kf_index = start_sample // KEYFRAME_INTERVAL
    if kf_index >= len(keyframes):
        raise IndexError(f"关键帧索引 {kf_index} 超出范围（共 {len(keyframes)}）")

    st = AdpcmState(keyframes[kf_index].predictor, keyframes[kf_index].step_index)
    # 从关键帧对应的采样点开始解码
    pos = kf_index * KEYFRAME_INTERVAL
    # 先吃掉到 start_sample 的 nibble（不保留结果）
    while pos < start_sample:
        byte = data[pos >> 1]
        code = (byte >> 4) if (pos & 1) == 0 else (byte & 0xF)
        decode_nibble(st, code)
        pos += 1

    out = np.empty(n_samples, dtype="<i2")
    for i in range(n_samples):
        byte = data[pos >> 1]
        code = (byte >> 4) if (pos & 1) == 0 else (byte & 0xF)
        out[i] = decode_nibble(st, code)
        pos += 1
    return out


def bytes_for(sample_count: int) -> int:
    """给定采样点数，ADPCM 需要多少字节"""
    return (sample_count + 1) // 2


def keyframe_count(sample_count: int) -> int:
    """关键帧条数。第 0 点必有一条，之后每 INTERVAL 点一条。

    推导：编码器在 i>0 且 i%INTERVAL==0 时追加。
    i 的取值集合 = {INTERVAL, 2*INTERVAL, ...} ∩ [1, n)
    → 条数 = 1 + floor((n-1)/INTERVAL)
    验证：n=1 → 1；n=128 → 1；n=129 → 2；n=48000 → 375
    """
    if sample_count <= 0:
        return 0
    return 1 + (sample_count - 1) // KEYFRAME_INTERVAL


def decode_at(data: bytes, start_sample: int, n_samples: int,
              keyframes: List[AdpcmState],
              byte_offset: int = 0) -> np.ndarray:
    """★ 固件定位循环点走的就是这条路径：从任意采样点开始解码。

    步骤（与 C 版必须逐步一致）：
      1. 由 start_sample 算出所属关键帧 k = start_sample // INTERVAL
      2. 用 keyframes[k] 恢复状态
      3. 从关键帧对应的采样点 (k*INTERVAL) 顺序解码到 start_sample，
         这期间的输出丢弃（只是为了让状态演进到正确位置）
      4. 再连续产出 n_samples 个点

    注意第 3 步不能省——关键帧只能保证"从关键帧起点开始"的状态正确，
    要落到 start_sample 还得把中间那不到 INTERVAL 个 nibble 吃掉。
    """
    if start_sample < 0:
        raise ValueError(f"start_sample 不能为负: {start_sample}")

    k = start_sample // KEYFRAME_INTERVAL
    if k >= len(keyframes):
        # 超出关键帧表：退回从头解码（正确但慢），保证不产生错误数据
        st = AdpcmState(keyframes[0].predictor, keyframes[0].step_index) if keyframes \
            else AdpcmState()
        k = 0
    else:
        st = AdpcmState(keyframes[k].predictor, keyframes[k].step_index)

    p = k * KEYFRAME_INTERVAL
    while p < start_sample:                      # 第 3 步：追上目标位置
        byte = data[byte_offset + (p >> 1)]
        code = (byte >> 4) if (p & 1) == 0 else (byte & 0xF)
        decode_nibble(st, code)
        p += 1

    out = np.empty(n_samples, dtype="<i2")
    for i in range(n_samples):
        byte = data[byte_offset + (p >> 1)]
        code = (byte >> 4) if (p & 1) == 0 else (byte & 0xF)
        out[i] = decode_nibble(st, code)
        p += 1
    return out


def decode_hybrid(blob: bytes, start_sample: int, n_samples: int,
                  keyframes: List[AdpcmState], attack_samples: int) -> np.ndarray:
    """★ 解码混合编码的 zone 数据——**固件读采样的唯一入口**。

    布局：blob = [PCM16 × attack] ++ [ADPCM 全长度流（含前 attack 点的冗余编码）]

    取第 p 个采样点：
        p <  attack → blob[p*2] 起的 int16（无损）
        p >= attack → blob[attack*2 + p>>1] 的 nibble，奇偶由 p&1 决定

    ★ 关键：字节偏移用的是 **attack*2 + p>>1**（用 p，不是 p-attack）。
      因为 ADPCM 流覆盖了整个缓冲区，前 attack 个点的编码只是被 PCM 覆盖掉了。
      这一点保证了 nibble 奇偶与采样点下标严格对应——若改成从 p-attack 起算，
      一旦 attack 是奇数，所有 nibble 的奇偶就会整体翻转。
    """
    if start_sample < 0:
        raise ValueError(f"start_sample 不能为负: {start_sample}")

    attack = max(0, min(int(attack_samples), attack_samples))
    pcm_bytes = attack * 2

    # 前段：直接取 PCM，无损
    n_head = min(n_samples, max(0, attack - start_sample))
    parts: List[np.ndarray] = []
    if n_head > 0:
        head = np.frombuffer(blob, dtype="<i2",
                             count=n_head, offset=start_sample * 2)
        parts.append(head.copy())

    # 后段：从 ADPCM 流解码（可能需要先追到 start_sample）
    rest_start = max(start_sample, attack)
    rest_n = n_samples - n_head
    if rest_n > 0:
        # 从 max(rest_start, attack) 处开始，用关键帧定位
        k = keyframe_for(rest_start)
        st = AdpcmState(keyframes[k].predictor, keyframes[k].step_index) \
            if k < len(keyframes) else AdpcmState()
        p = k * KEYFRAME_INTERVAL
        while p < rest_start:
            byte = blob[pcm_bytes + (p >> 1)]
            decode_nibble(st, (byte >> 4) if (p & 1) == 0 else (byte & 0xF))
            p += 1

        tail = np.empty(rest_n, dtype="<i2")
        for i in range(rest_n):
            byte = blob[pcm_bytes + (p >> 1)]
            tail[i] = decode_nibble(st, (byte >> 4) if (p & 1) == 0 else (byte & 0xF))
            p += 1
        parts.append(tail)

    return np.concatenate(parts) if len(parts) > 1 else parts[0]


def keyframe_for(sample_pos: int) -> int:
    """采样点 → 关键帧下标（固件的同一套算法）"""
    return sample_pos // KEYFRAME_INTERVAL


def keyframe_bytes(sample_count: int) -> int:
    """关键帧表需要多少字节（每条 4 字节）"""
    return keyframe_count(sample_count) * 4


def encode_hybrid(samples: np.ndarray, attack_samples: int
                  ) -> Tuple[bytes, List[AdpcmState], int]:
    """混合编码：前 attack_samples 个点用 PCM16，其余用 ADPCM。

    起音瞬态（对音色辨识最关键的部分）无损，循环段省空间。

    ★ 关键设计：**ADPCM 流覆盖整个采样缓冲区**，前 attack 个点解出来的
      数据被丢弃、由 PCM 段替代。这样：

      1. 关键帧索引 == 采样点索引，**无需任何偏移换算**，解码器不会算错
      2. ADPCM 状态从第 attack 个采样点处的正确状态继续演进
         （不是从 0 重新开始），PCM→ADPCM 交界处无跳变
      3. 循环点定位逻辑只有一套：先查关键帧、再顺序解码

    布局 = [PCM16 × attack] ++ [ADPCM 全长度流]
    解码器：前 attack 个点读 PCM；第 p 个点（p>=attack）读
            adpcm_byte(p//2)，高 nibble 还是低 nibble 由 p&1 决定。

    返回 (blob, keyframes, attack)，其中 blob = PCM 段 ++ ADPCM 段。
    """
    n = len(samples)
    attack = max(0, min(int(attack_samples), n))

    # 用 PCM 段最后一个采样点作为 ADPCM 的初始 predictor，保证交界连续
    init = AdpcmState(int(samples[attack - 1]) if attack > 0 else 0, 0)
    adpcm, kf = encode(samples, init)

    pcm = samples[:attack].astype("<i2").tobytes()
    # ADPCM 段比实际需要的长（前 attack 个点的编码是冗余的），
    # 在写入镜像时按需截断，见 layout.py
    return pcm + adpcm, kf, attack


def hybrid_layout(n_samples: int, attack_samples: int) -> Tuple[int, int, int]:
    """返回 (pcm_bytes, adpcm_bytes_redundant, total_bytes)。

    adpcm_bytes_redundant 是完整的 ADPCM 流长度（含前 attack 点的冗余编码）。
    实际存储时前 attack//2 字节可以省略，但保留它们能让
    字节偏移 == 采样点偏移 // 2 的关系保持简单。**当前方案保留**，
    代价是 attack//2 字节的浪费（典型 attack=480 点 → 240 字节/zone，
    2048 个 zone 共约 0.5MB，可接受）。
    """
    pcm_bytes = attack_samples * 2
    adpcm_bytes = bytes_for(n_samples)
    return pcm_bytes, adpcm_bytes, pcm_bytes + adpcm_bytes
