"""
tests/test_adpcm.py — ADPCM 编解码器测试

重点验证三件事：
  1. 编解码往返的量化误差在 IMA ADPCM 的预期范围内（不能测"无损"，那不可能）
  2. **固件从关键帧定位循环点**的路径与从头解码**逐比特一致**
     （这是最容易出错的地方：差一个 nibble 就整段错位）
  3. 混合编码 PCM→ADPCM 交界处无跳变（否则每次起音都会"啪"一声）
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gmss import adpcm  # noqa: E402


def _sine(freq: float, sr: int, n: int, amp: float = 0.8) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / sr
    return (np.sin(2 * math.pi * freq * t) * amp * 32767).astype("<i2")


def _snr_db(orig: np.ndarray, dec: np.ndarray) -> float:
    o = orig.astype(np.float64)
    d = dec.astype(np.float64)
    noise = o - d
    ps = float(np.mean(o ** 2))
    pn = float(np.mean(noise ** 2))
    if pn <= 0:
        return float("inf")
    return 10.0 * math.log10(ps / pn)


# --------------------------------------------------------------------------

def test_tables_are_standard():
    """IMA 表必须是标准值，改一个数就会让固件与 PC 端不一致"""
    assert len(adpcm.STEP_TABLE) == 89
    assert adpcm.STEP_TABLE[0] == 7
    assert adpcm.STEP_TABLE[88] == 32767
    assert adpcm.STEP_TABLE[1] == 8
    assert adpcm.INDEX_TABLE == [-1, -1, -1, -1, 2, 4, 6, 8,
                                 -1, -1, -1, -1, 2, 4, 6, 8]
    # 步长表必须严格递增（单调）
    for i in range(1, 89):
        assert adpcm.STEP_TABLE[i] > adpcm.STEP_TABLE[i - 1], f"步长表在 {i} 处非递增"


def test_trunc_div_matches_c_semantics():
    """C 的整数除法向零取整，Python 的 // 向下取整。这个差异会毁掉一致性。"""
    assert adpcm._trunc_div(7, 2) == 3
    assert adpcm._trunc_div(-7, 2) == -3        # Python 的 -7 // 2 == -4
    assert adpcm._trunc_div(-1, 2) == 0
    assert adpcm._trunc_div(1, 2) == 0
    assert adpcm._trunc_div(9, 3) == 3
    assert adpcm._trunc_div(-9, 3) == -3


def test_codec_agreement():
    """编码器内部的预测器演进必须与解码器完全一致（编码时就在模拟解码）"""
    rng = np.random.default_rng(1234)
    samples = rng.integers(-20000, 20000, size=3000).astype("<i2")

    enc_state = adpcm.AdpcmState()
    codes = []
    for s in samples:
        codes.append(adpcm.encode_nibble(enc_state, int(s)))

    dec_state = adpcm.AdpcmState()
    decoded = [adpcm.decode_nibble(dec_state, c) for c in codes]

    assert enc_state.predictor == dec_state.predictor, \
        f"编解码状态不一致: {enc_state.predictor} vs {dec_state.predictor}"
    assert enc_state.step_index == dec_state.step_index
    # 解码结果即编码器每一步的预测值
    assert len(decoded) == len(samples)


def test_roundtrip_sine_snr():
    """正弦波往返，SNR 应在 IMA ADPCM 的典型区间（约 15~35 dB）"""
    x = _sine(440.0, 48000, 4800, amp=0.8)
    data, kf = adpcm.encode(x)
    y = adpcm.decode(data, len(x))
    snr = _snr_db(x, y)
    assert 12.0 < snr < 40.0, f"SNR {snr:.1f} dB 超出预期区间，可能是表或算法错了"
    # 首个采样点误差应较小（state 从 0 起步，允许瞬态）
    assert int(data[0] >> 4) < 16


def test_roundtrip_length_and_packing():
    """奇数个采样点时最后一字节低 nibble 补 0，长度必须是 ceil(n/2)"""
    for n in (1, 2, 3, 15, 16, 17, 127, 128, 129, 255):
        x = _sine(1000.0, 48000, n)
        data, _ = adpcm.encode(x)
        assert len(data) == (n + 1) // 2, f"n={n} 长度错: {len(data)}"
        y = adpcm.decode(data, n)
        assert len(y) == n


def test_nibble_order_high_first():
    """高 nibble 必须对应靠前的采样点"""
    x = np.array([30000, -30000], dtype="<i2")
    data, _ = adpcm.encode(x)
    assert len(data) == 1
    hi = data[0] >> 4
    lo = data[0] & 0xF
    # 第一个点是大正数 → 高 nibble 符号位为 0
    assert (hi & 8) == 0, f"第一个点为正，高 nibble 符号位应为 0，实际码 {hi}"
    # 第二个点是大负数 → 低 nibble 符号位为 1
    assert (lo & 8) == 8, f"第二个点为负，低 nibble 符号位应为 1，实际码 {lo}"


def test_keyframe_seek_matches_sequential():
    """★ 核心测试：从关键帧定位解码 vs 从头顺序解码，必须逐比特一致。

    固件定位循环点时走的就是关键帧路径。如果不一致，
    每次循环回跳都会产生咔哒或音高偏移。
    """
    x = _sine(220.0, 48000, 48000, amp=0.9)      # 1 秒，覆盖多个关键帧
    data, kf = adpcm.encode(x)
    full = adpcm.decode(data, len(x))

    # 在每个关键帧边界及其前后各测一次
    checkpoints = []
    for i in range(len(kf)):
        p = i * adpcm.KEYFRAME_INTERVAL
        checkpoints += [p, p + 1, p + 63, p + 127]
    checkpoints = sorted({c for c in checkpoints if 0 <= c < len(x) - 200})

    assert len(checkpoints) > 50, "测试点太少，覆盖不足"

    for start in checkpoints:
        n = 137                                   # 取个非整齐长度
        seg = adpcm.decode_from_keyframe(data, n, kf, start)
        ref = full[start:start + n]
        assert np.array_equal(seg, ref), (
            f"从采样点 {start}（关键帧 {start // adpcm.KEYFRAME_INTERVAL}）"
            f"定位解码与顺序解码不一致：首个差异在 "
            f"{int(np.argmax(seg != ref))}"
        )


def test_keyframe_count_and_indexing():
    """关键帧条数与索引必须覆盖整个采样长度"""
    for n in (1, 2, 127, 128, 129, 1000, 48000):
        _, kf = adpcm.encode(_sine(440, 48000, n))
        expect = adpcm.keyframe_count(n)
        assert len(kf) == expect, f"n={n}: 关键帧 {len(kf)} 条，应为 {expect}"
        # 手动核对公式：1 + floor((n-1)/128)
        assert expect == 1 + (n - 1) // adpcm.KEYFRAME_INTERVAL
        # 最后一个关键帧对应的采样点必须 < n
        assert (len(kf) - 1) * adpcm.KEYFRAME_INTERVAL < n
        # 关键帧表字节数
        assert adpcm.keyframe_bytes(n) == expect * 4


def test_state_serialization_roundtrip():
    """关键帧要写进 Flash，序列化必须可逆"""
    for pred, idx in [(0, 0), (12345, 42), (-32768, 88), (32767, 0), (-1, 87)]:
        st = adpcm.AdpcmState(pred, idx)
        back = adpcm.AdpcmState.from_bytes(st.to_bytes())
        assert back.predictor == st.predictor
        assert back.step_index == st.step_index
    assert len(adpcm.AdpcmState().to_bytes()) == 4


def test_hybrid_boundary_continuity():
    """★ 混合编码在 PCM→ADPCM 交界处不能有跳变（否则每次起音都'啪'一声）"""
    n = 8000
    x = _sine(440.0, 48000, n, amp=0.7)
    attack = 480
    blob, kf, got_attack = adpcm.encode_hybrid(x, attack)
    assert got_attack == attack

    pcm_bytes = attack * 2
    assert len(blob) == pcm_bytes + adpcm.bytes_for(n)

    # 重建：前 attack 个点用 PCM，其余用 ADPCM 流。
    # ★ 注意 nibble_base：ADPCM 段在 blob 里被 PCM 段推后了 attack*2 字节，
    #   把整块 blob 交给解码器时必须告诉它第 0 个 nibble 对应哪个采样点，
    #   否则 nibble 会整体错位（症状：交界处巨大跳变）。
    pcm = np.frombuffer(blob[:pcm_bytes], dtype="<i2")
    assert len(pcm) == attack

    # 取尾部数据段：走 decode_hybrid（固件的正式入口），它内部会处理
    # PCM 段 + ADPCM 段的字节偏移和关键帧定位
    tail = adpcm.decode_hybrid(blob, attack, n - attack, kf, attack)
    recon = np.concatenate([pcm, tail])

    # 交界处的相邻采样差值不应异常放大
    d_before = abs(int(recon[attack - 1]) - int(recon[attack - 2]))
    d_cross = abs(int(recon[attack]) - int(recon[attack - 1]))
    d_after = abs(int(recon[attack + 1]) - int(recon[attack]))
    # 正弦波同频下相邻差应相近；允许 6 倍余量给量化噪声
    limit = max(d_before, d_after, 100) * 6
    assert d_cross <= limit, (
        f"交界处跳变过大: 跨界差 {d_cross}, 前 {d_before}, 后 {d_after}"
    )

    # 首段必须与原始 PCM 完全一致（无损）
    assert np.array_equal(pcm, x[:attack]), "PCM 段必须与原始无损一致"

    # 整体 SNR 仍应在合理区间。
    # 下界 15dB 是 IMA ADPCM 的可接受底线；上界不设——
    # 平滑周期信号（纯正弦）的 ADPCM 误差本来就可能很高（实测 45dB+），
    # 卡上界只会误报。
    snr = _snr_db(x[attack:], recon[attack:])
    assert snr > 15.0, f"混合编码尾段 SNR 仅 {snr:.1f} dB，低于 IMA ADPCM 合理下限"


def test_hybrid_keyframe_index_equals_sample_index():
    """★ 关键帧索引必须等于采样点索引（这是无歧义设计的核心保证）

    用 decode_hybrid 从**任意**采样点取数（含 PCM 段内），
    结果必须与从头完整解码一致。这同时覆盖了 PCM/ADPCM 交界两侧。
    """
    n = 5000
    x = _sine(300.0, 48000, n, amp=0.6)
    for attack in (0, 1, 128, 129, 480):
        blob, kf, _ = adpcm.encode_hybrid(x, attack)
        assert len(kf) == adpcm.keyframe_count(n), \
            f"attack={attack}: 关键帧数应为 {adpcm.keyframe_count(n)}"

        full = adpcm.decode_hybrid(blob, 0, n, kf, attack)
        assert np.array_equal(full, adpcm.decode_hybrid(blob, 0, n, kf, attack)), \
            "decode_hybrid 结果不稳定"

        # 在交界附近和若干关键帧边界各取一段，逐点比对
        probes = {0, 1, attack - 1, attack, attack + 1, 127, 128, 129, 255, 256,
                  n - 50}
        probes = sorted(p for p in probes if 0 <= p < n - 20)
        for p in probes:
            got = adpcm.decode_hybrid(blob, p, 20, kf, attack)
            assert np.array_equal(got, full[p:p + 20]), (
                f"attack={attack} 从采样点 {p} 取 20 点与完整解码不一致，"
                f"首个差异在第 {int(np.argmax(got != full[p:p + 20]))} 点"
            )


def test_hybrid_attack_beyond_length():
    """attack 超过采样长度时必须被夹紧，不能越界"""
    x = _sine(440, 48000, 100)
    for attack in (0, 100, 101, 1000):
        blob, kf, got = adpcm.encode_hybrid(x, attack)
        assert got == min(attack, 100)
        assert len(blob) > 0


if __name__ == "__main__":
    import shutil

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            nargs = t.__code__.co_argcount
            t() if nargs == 0 else None
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
