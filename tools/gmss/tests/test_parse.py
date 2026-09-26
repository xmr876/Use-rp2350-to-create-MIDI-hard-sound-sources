"""
tests/test_parse.py — 验证 SF2 解析器

用合成 sf2 逐项断言：采样头、instrument zone 的全局合并、preset 的力度分层。
可用 pytest 运行，也可直接 `python tests/test_parse.py`。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))

from make_test_sf2 import build_sf2          # noqa: E402
from gmss import parse, sf2defs as D         # noqa: E402


def _sf(tmp_path: Path):
    p = tmp_path / "test.sf2"
    meta = build_sf2(str(p))
    return parse.load_soundfont(str(p)), meta


def test_header_and_samples(tmp_path):
    sf, meta = _sf(tmp_path)
    assert sf.info.get("INAM") == "GMSS Test Font"
    assert len(sf.samples) == 2, f"应有 2 个采样头，实际 {len(sf.samples)}"
    assert len(sf.sample_data) == meta["sample_points"]

    s0, s1 = sf.samples
    assert s0.name == "TestSine_soft"
    assert s1.name == "TestSine_hard"
    assert s0.sample_rate == 44100
    assert s0.original_key == 60
    assert s0.start == 0 and s0.end == meta["sample_points"] // 2
    assert s1.start == meta["sample_points"] // 2
    # 循环点必须落在采样范围内且 loop_start < loop_end
    for s in sf.samples:
        assert s.startloop < s.endloop, f"{s.name} 循环点非法"
        assert s.start <= s.startloop and s.endloop <= s.end


def test_instruments_and_global_merge(tmp_path):
    sf, _ = _sf(tmp_path)
    assert len(sf.instruments) == 2, \
        f"应有 2 个 instrument，实际 {len(sf.instruments)}: " \
        f"{[i.name for i in sf.instruments]}"

    inst0 = sf.instruments[0]
    assert inst0.name == "InstSoft"
    # global zone 被吃掉，只剩 1 个 local zone
    assert len(inst0.zones) == 1, \
        f"global zone 应被合并，实际剩 {len(inst0.zones)} 个 zone"
    z = inst0.zones[0]
    assert z.gens[D.GEN["sampleID"]].amount == 0
    # 来自 global zone 的生成器必须仍然可见
    assert z.get("initialFilterFc") == 13500, "global zone 的生成器未合并进来"
    assert z.get("pan") == 0
    assert z.get("sampleModes") == 1, "应标记为连续循环"
    assert z.get("initialAttenuation") == 60
    # 未指定的生成器走默认值
    assert z.get("coarseTune") == 0
    assert z.get("scaleTuning") == 100


def test_presets_and_velocity_layers(tmp_path):
    sf, _ = _sf(tmp_path)
    assert len(sf.presets) == 1
    p = sf.presets[0]
    assert p.name == "TestPiano"
    assert p.bank == 0 and p.program == 0
    assert len(p.zones) == 2, f"应有 2 个力度层，实际 {len(p.zones)}"

    z_soft, z_hard = p.zones
    assert (z_soft.key_lo, z_soft.key_hi) == (0, 127)
    assert (z_soft.vel_lo, z_soft.vel_hi) == (0, 63), \
        f"弱层力度范围错: {z_soft.vel_lo}-{z_soft.vel_hi}"
    assert (z_hard.vel_lo, z_hard.vel_hi) == (64, 127), \
        f"强层力度范围错: {z_hard.vel_lo}-{z_hard.vel_hi}"
    assert z_soft.instrument_idx == 0
    assert z_hard.instrument_idx == 1


def test_timecents_and_centibels():
    # SF2 规范：秒 = 2^(tc/1200)。tc=0 → 1s，tc=1200 → 2s，tc=-1200 → 0.5s
    assert abs(D.tc2ms(0) - 1000.0) < 1e-9
    assert abs(D.tc2ms(1200) - 2000.0) < 1e-6
    assert abs(D.tc2ms(-1200) - 500.0) < 1e-9
    assert D.tc2ms(-32768) == 0.0
    # -12000 tc ≈ 0.977ms，是 SF2 里"最短起音"的常用值
    assert abs(D.tc2ms(-12000) - 0.9765625) < 1e-9
    # centibels: 0cb = 满幅，100cb = -10dB ≈ 0.316
    assert abs(D.cb2gain(0) - 1.0) < 1e-12
    assert abs(D.cb2gain(100) - 0.316227766) < 1e-6
    assert D.cb2q15(0) == 32767
    assert D.cb2q15(1000) < 10      # 衰减 100dB ≈ 静音


def test_range_byte_order():
    """keyRange/velRange 的高低字节顺序是 SF2 最容易踩的坑，单独守一条。

    SF2 规范 p.19 的 rangesType = {uint8 byLo; uint8 byHi;}，
    低字节 = 低值、高字节 = 高值。NAudio 的 SampleMap 也是这么取的。
    编码：pack_range(lo,hi) = (hi << 8) | lo
    """
    assert D.pack_range(0, 127) == 0x7F00
    assert D.pack_range(0, 60) == 0x3C00
    assert D.pack_range(36, 60) == 0x3C24
    assert D.pack_range(0, 63) == 0x3F00
    assert D.pack_range(64, 127) == 0x7F40

    # 解包必须与打包互逆
    for lo, hi in [(0, 127), (0, 60), (36, 60), (1, 1), (0, 0), (127, 127)]:
        assert D.key_range(D.pack_range(lo, hi)) == (lo, hi), f"往返失败 {lo}-{hi}"
    for lo, hi in [(0, 63), (64, 127), (100, 127)]:
        assert D.vel_range(D.pack_range(lo, hi)) == (lo, hi), f"往返失败 {lo}-{hi}"

    # 关键：绝不能把 16 位整体当范围读（那会把 0-60 变成 15360）
    assert D.key_range(0x3C00) != (60, 0)
    assert D.key_range(0x3C00) == (0, 60)


def test_loop_points_are_integral_cycles(tmp_path):
    """循环段长度应是整数个周期，且循环段内不能有淡出"""
    sf, _ = _sf(tmp_path)
    sr = 44100
    period = sr / 262.5          # 合成文件用的频率，见 make_test_sf2.TEST_FREQ
    assert abs(period - 168.0) < 1e-9, "测试前提：该频率下周期应为整数"

    for s in sf.samples:
        loop_len = s.endloop - s.startloop
        cycles = loop_len / period
        assert abs(cycles - round(cycles)) < 1e-6, \
            f"{s.name} 循环长度 {loop_len} 不是整数周期 ({cycles:.6f})"
        assert loop_len == int(round(period)) * 8, \
            f"{s.name} 循环长度应等于 8 个周期 = {int(round(period)) * 8}，实际 {loop_len}"
        # 循环段之后必须还有空间（淡出区不能侵入循环段）
        assert s.endloop < s.end, f"{s.name} 循环段顶到了采样末尾，没有淡出空间"


if __name__ == "__main__":
    import shutil

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    # 不用系统临时目录（沙箱下不可写），用工作区内的 tests/tmp
    tmp_root = Path(__file__).resolve().parent / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    for t in tests:
        td = tmp_root / t.__name__
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)
        td.mkdir(parents=True, exist_ok=True)
        try:
            # 按签名决定是否传 tmp_path，这样纯函数测试也能复用同一个 runner
            nargs = t.__code__.co_argcount
            t(td) if nargs >= 1 else t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            msg = str(e) or "<裸 assert，无消息>"
            print(f"  FAIL  {t.__name__}: {msg}")
            tb = sys.exc_info()[2]
            while tb.tb_next:
                tb = tb.tb_next
            print(f"        于 {t.__name__} 第 {tb.tb_lineno} 行")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
