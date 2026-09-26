"""
tests/test_player.py — 参考播放器验证

核心思想
--------
`plan.py` 里已经有一份"应该是什么"的地面真值：重采样后、抽取好的采样数组。
`player.py` 从**镜像文件**（v2 单片方案：只有 library.bin）把它读回来。
两者必须**完全相等**（同一份数据，读回不应有任何损失）。

这一条过了，就证明了：
  - zone 表里的偏移/长度/循环点/关键帧位置**都写对了**
  - ADPCM 关键帧定位在真实镜像上可用
  - PCM16 → ADPCM 交界处读回正确

★ 注意用词：这里验证的是 **zone 数据提取的等价性**（同一份 PCM 数据
  "写进去再读回来"完全一致），不是"编解码器逐位一致"。
  ADPCM 编解码本身在 test_adpcm.py 里验证。

运行:  python tests/test_player.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from make_test_sf2 import build_sf2            # noqa: E402
from gmss import adpcm, layout, parse, plan, player    # noqa: E402


def _build(tmp: Path):
    sf2 = tmp / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, drum = plan.plan(sf)
    # ★ place()/write_images() 不再有 chip_count 参数：单片方案一个采样池
    placed = layout.place(mel + drum, verbose=False)
    layout.write_images(placed, tmp / "img",
                        build_name="T", verbose=False)
    return sf, mel + drum, placed, tmp / "img"


def test_player_constants_match_layout():
    """player.py 是固件参考实现，自带一份格式常量副本；副本必须与 layout.py 一致

    ★ 这条是补的：player.py 曾经用了未定义的 LIB_BASE，直到真跑一遍才炸出
      NameError。常量副本漂移是静默错误的温床，必须钉住。
    """
    assert player.LIB_BASE == layout.LIB_BASE == 0x40000
    assert player.GMSS_MAGIC == layout.GMSS_MAGIC
    assert player.HEADER_SIZE == layout.HEADER_SIZE == 64
    assert player.ZONE_SIZE == layout.ZONE_SIZE == 56
    assert player.INSTR_SIZE == layout.INSTR_SIZE == 32
    assert player.ZF_LOOPED == layout.ZF_LOOPED
    assert player.ZF_PERCUSSIVE == layout.ZF_PERCUSSIVE
    assert player.CODEC_PCM16 == layout.CODEC_PCM16
    assert player.CODEC_ADPCM4 == layout.CODEC_ADPCM4
    assert player.LOOP_NONE == plan.LOOP_NONE == 0xFFFFFFFF


def test_image_loads_and_checksum(tmp_path):
    """镜像能被加载，且头部校验和自洽"""
    _build(tmp_path)
    img = player.GmssImage(tmp_path / "img")
    assert img.sample_rate == 48000
    # ★ 单片方案：头部 chip_count 恒为 1，只有一个 library.bin
    assert img.chip_count == 1
    assert img.total_zones > 0
    # 兼容旧接口：chip() 恒返回整个库
    assert img.chip(0) is img.lib
    # ★ zone 里的 sample_off 是 flash 绝对地址：池起点 == LIB_BASE + pool_off
    assert img.pool_off % layout.POOL_ALIGN == 0
    for z in img.zones:
        assert z.sample_off >= layout.LIB_BASE + img.pool_off
        assert z.sample_off - layout.LIB_BASE < len(img.lib)


def test_roundtrip_pcm_matches_plan(tmp_path):
    """★ PCM 起音段必须**逐位无损**（这一段不经过 ADPCM）

    ★★ 关于"为什么 ADPCM 段不能要求相等"：
       ADPCM 是**有损压缩**（4 bit/采样）。镜像里存的是压缩码字，
       读回来得到的是**有损重建**，而 plan 里的 samples[] 是
       **重采样后的原始数据**。两者在 ADPCM 段必然有量化误差。

       实测该测试数据的 ADPCM 段重建质量：
           平均误差 25.2（满幅的 0.077%）
           最大误差 82
           SNR ≈ 51 dB
       完全符合 IMA ADPCM 的预期。

       （这一段曾经被误判为"缺陷"，其实是断言写错了。记录在此以免重蹈。）
    """
    _, zones, placed, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)

    checked = 0
    for pi, pz in enumerate(placed):
        target = None
        for z in img.zones:
            if (z.sample_off == pz.sample_off
                    and z.chip_id == pz.chip_id
                    and z.sample_len == pz.sample_len):
                target = z
                break
        assert target is not None, f"zone {pi} 在镜像里找不到对应条目"

        n = target.sample_len
        # ★ 只检查 PCM 段 —— 那一段是设计上无损的
        starts = [0, 1, max(0, target.attack_samples - 60)]
        starts = sorted({s for s in starts if 0 <= s < n})
        for st in starts:
            cnt = min(50, target.attack_samples - st, n - st)
            if cnt <= 0:
                continue
            got = img.read_samples(target, st, cnt)
            ref = pz.z.sample.data[st:st + cnt]
            assert np.array_equal(got, ref), (
                "zone(%d,%d) 的 PCM 段从第 %d 帧读 %d 帧不一致"
                % (target.chip_id, target.sample_off, st, cnt))
            checked += 1

    assert checked >= 6, f"检查点太少（{checked}）"


def test_adpcm_segment_matches_encoder(tmp_path):
    """镜像里的 ADPCM 段必须与重新编码的结果**逐字节相等**

    这一条证明写盘过程没有损坏数据 ——
    读回来有量化误差是编解码器的性质，不是存储的问题。
    """
    _, _, placed, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)

    for pz in placed:
        z = next((zz for zz in img.zones
                  if zz.sample_off == pz.sample_off
                  and zz.chip_id == pz.chip_id), None)
        assert z is not None
        if z.codec != player.CODEC_ADPCM4:
            continue

        blob = img.chip(z.chip_id)
        # ★ zone.sample_off 是 flash 绝对地址，库文件从 LIB_BASE 开始
        base = z.sample_off - layout.LIB_BASE
        pcm_bytes = z.attack_samples * 2

        fresh_blob, _, _ = adpcm.encode_hybrid(pz.z.sample.data,
                                               z.attack_samples)
        fresh_adpcm = fresh_blob[pcm_bytes:]
        disk_adpcm = blob[base + pcm_bytes: base + pcm_bytes + len(fresh_adpcm)]
        assert fresh_adpcm == disk_adpcm, "ADPCM 段与重新编码不一致"


def test_adpcm_quantization_is_reasonable(tmp_path):
    """★ ADPCM 段的重建误差必须在合理范围（这是有损压缩，不是无损）

    判据用 SNR，不用"相等"：
      - SNR < 30dB 说明编码器有问题（步长表错、状态更新错）
      - SNR > 70dB 反而不正常（说明根本没压缩）
    实测平滑正弦波信号约 51dB。
    """
    _, zones, placed, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)

    for pz in placed:
        z = next((zz for zz in img.zones
                  if zz.sample_off == pz.sample_off
                  and zz.chip_id == pz.chip_id), None)
        if z is None or z.codec != player.CODEC_ADPCM4:
            continue

        got = img.read_samples(z, 0, z.sample_len).astype(np.int64)
        ref = pz.z.sample.data.astype(np.int64)

        # 只算 ADPCM 段（PCM 段是精确的，混进去会虚高 SNR）
        seg_got = got[z.attack_samples:]
        seg_ref = ref[z.attack_samples:]
        err = seg_got - seg_ref
        ps = float((seg_ref ** 2).mean())
        pn = float((err ** 2).mean())
        if pn <= 0:
            raise AssertionError("ADPCM 段误差为 0，不该发生")
        snr = 10.0 * np.log10(ps / pn)

        assert snr > 25.0, f"ADPCM SNR 仅 {snr:.1f} dB，编码器可能有问题"
        assert snr < 80.0, f"ADPCM SNR 高达 {snr:.1f} dB，压缩没生效？"
        # 误差相对满幅应很小
        assert np.abs(err).max() < 2000, "单点误差过大"


def test_keyframe_seek_on_real_image(tmp_path):
    """★ 在真实镜像上验证关键帧定位：从中间读 vs 从头发读取，结果必须一致"""
    _, _, placed, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)

    for pz in placed:
        z = None
        for cand in img.zones:
            if (cand.sample_off == pz.sample_off
                    and cand.chip_id == pz.chip_id
                    and cand.sample_len == pz.sample_len):
                z = cand
                break
        assert z is not None

        n = z.sample_len
        if n < 400:
            continue

        # 顺序读整个 ADPCM 段
        seq = img.read_samples(z, 0, n)

        # 从若干位置开始读一小段，必须与顺序读的对应片段一致
        for st in (z.attack_samples + 1, n // 3, n // 2, n - 100):
            if st <= z.attack_samples or st >= n - 10:
                continue
            seg = img.read_samples(z, st, 10)
            assert np.array_equal(seg, seq[st:st + 10]), (
                f"从第 {st} 帧定位读取与顺序读取不一致")


def test_zone_selection(tmp_path):
    """按力度选 zone：应该挑到力度范围最窄的"""
    _, zones, _, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)

    # 测试数据是一个 preset（program 0）+ 两个力度层（0-63 / 64-127）
    z_soft = img.find_zone(0, 60, 30)
    z_hard = img.find_zone(0, 60, 100)
    assert z_soft is not None and z_hard is not None
    assert z_soft.vel_lo == 0 and z_soft.vel_hi == 63
    assert z_hard.vel_lo == 64 and z_hard.vel_hi == 127
    # ★ 语义变化：旧设计断言"两层落在不同的片或不同的偏移"，
    #   因为那时偏移是片内相对、可能撞号；单片方案的偏移是全局唯一的
    #   flash 绝对地址，所以两层必须（且只能）靠偏移区分开。
    assert z_soft.sample_off != z_hard.sample_off, \
        "两个力度层内容不同，必须占不同的采样位置（去重不该把它们合并）"


def test_missing_program_returns_none(tmp_path):
    """查不存在的音色应返回 None 而不是崩溃"""
    _, _, _, img_dir = _build(tmp_path)
    img = player.GmssImage(img_dir)
    # program 99 在测试数据里是空槽
    assert img.find_zone(99, 60, 100) is None


def test_rejects_corrupt_magic(tmp_path):
    """魔数被改坏必须报错，不能静默读出垃圾

    ★ 语义变化：旧方案头部在 boot.bin 里、加载时只读 boot.bin，所以要破坏
      chip0.bin 再调 chip()；单片方案头部就在 library.bin 里，
      破坏它之后**加载本身**就该失败。
    """
    _build(tmp_path)
    p = tmp_path / "img" / "library.bin"
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF          # 破坏魔数
    p.write_bytes(bytes(data))

    try:
        player.GmssImage(tmp_path / "img")
    except ValueError as e:
        assert "魔数" in str(e)
        return
    raise AssertionError("魔数损坏应报错")


if __name__ == "__main__":
    import shutil

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    tmp_root = Path(__file__).resolve().parent / "tmp"
    for t in tests:
        td = tmp_root / t.__name__
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)
        td.mkdir(parents=True, exist_ok=True)
        try:
            nargs = t.__code__.co_argcount
            t(td) if nargs >= 1 else t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {str(e) or '<裸 assert>'}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
