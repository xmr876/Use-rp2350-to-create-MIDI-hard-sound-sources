"""
tests/test_pipeline.py — 端到端：SF2 → 规划 → 布局 → 镜像 → 回读校验

这是最有价值的一组测试，因为 C 结构体布局、ADPCM 编码、采样池分配
这三块只要有一处对不上，症状都是"音频全乱"而不是报错。

★ v2：单片 flash 方案 —— 产物只有 library.bin 一个文件，
  文件偏移 0 对应 flash 地址 layout.LIB_BASE (0x40000)，
  所以索引采样必须做 `pz.sample_off - layout.LIB_BASE` 的换算。
回读校验用 struct 按 gmss_format.h 的字段定义重新解析，等于做了一次
"PC 端 ↔ 格式定义" 的双向对拍。
"""
from __future__ import annotations

import dataclasses
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from make_test_sf2 import build_sf2          # noqa: E402
from gmss import layout, parse, plan         # noqa: E402

GMSS_MAGIC = 0x53534D47
# （旧的 CHIP_MAGIC / chipN.bin 已随多片方案一起废弃，不再有片头魔数可查）


def _build(tmp: Path):
    sf2 = tmp / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, drum = plan.plan(sf)
    # ★ place()/write_images() 不再有 chip_count：所有采样进同一个池
    placed = layout.place(mel + drum, verbose=False)
    info = layout.write_images(placed, tmp / "img",
                               build_name="TEST", verbose=False)
    return placed, info, tmp / "img"


def test_struct_sizes_match_format_header():
    """★ 布局常量必须与 gmss_format.h 逐字段一致。

    这些数字是逐字段累加核对过的。改 C 结构体时必须同步改这里，
    否则 zone 表会静默错位（音频全乱但不报错）。
    """
    assert layout.ZONE_SIZE == 56, "gmss_zone_t 应为 56 字节"
    assert layout.INSTR_SIZE == 32, "gmss_instrument_t 应为 32 字节"
    assert layout.HEADER_SIZE == 64, "gmss_header_t 应为 64 字节"
    assert layout.CHIP_HDR_SIZE == 16, "gmss_chip_header_t 应为 16 字节"

    # 重新按字段算一遍，防止有人"顺手"改常量而没改结构体。
    # 按 gmss_format.h 的字段顺序逐段累加：
    #   4  key_lo..vel_hi
    #   4  chip_id, codec, flags
    #  24  sample_off, sample_len, loop_start, loop_end,
    #      attack_samples, keyframe_off, keyframe_count   (6 × u32)
    #   6  root_key:u8, tune_cents:i8, gain_db100:i16, pad0:u16
    #   8  env_attack, env_decay, env_sustain, env_release
    #   4  loop_xfade, pad0
    #   4  reserved0
    # 直接按格式串算，并逐段核对字段布局。
    # 字段表: 4(BBBB) + 4(BBH) + 28(IIIIIII) + 4(Bbh) + 8(HHhH) + 4(hH) + 4(I) = 56
    ZFMT = "<BBBBBBHIIIIIIIBbhHHhHhHI"
    assert struct.calcsize(ZFMT) == layout.ZONE_SIZE, \
        f"zone 格式串 {struct.calcsize(ZFMT)} != ZONE_SIZE {layout.ZONE_SIZE}"
    assert (4 + 4 + 4 * 7 + 4 + 8 + 4 + 4) == layout.ZONE_SIZE, "字段累加应为 56"


def test_pack_zone_roundtrip(tmp_path):
    """pack_zone 的输出必须能按 C 字段定义读回来"""
    placed, _, _ = _build(tmp_path)
    for pz in placed[:1]:
        raw = layout.pack_zone(pz)
        assert len(raw) == layout.ZONE_SIZE
        # 24 个字段，与 gmss_format.h 的 gmss_zone_t 一一对应
        # (offset 36 是 3 个字段：root_key(uint8)/tune_cents(int8)/gain_db100(int16) = 4 字节，
        #  ★ 这里原来多写了一个 pad0，使格式串比 C 结构体长 2 字节 ——
        #    见 layout.pack_zone 的说明和 tools/check_struct_sizes.py)
        (kl, kh, vl, vh, chip, codec, flags, off, slen, ls, le,
         attack, kf_off, kf_n, root, tune, gain,
         ea, ed, es, er, fx, _pad2, rsv) = struct.unpack_from(
            "<BBBBBBHIIIIIIIBbhHHhHhHI", raw, 0)

        assert (kl, kh) == (pz.z.key_lo, pz.z.key_hi)
        assert (vl, vh) == (pz.z.vel_lo, pz.z.vel_hi)
        assert chip == pz.chip_id
        # ★ 单片方案：chip_id 恒为 0，保留字段只为对齐 C 结构体长度
        assert chip == 0, "单片方案下 chip_id 必须是 0"
        assert codec == pz.codec
        assert flags & layout.ZF_LOOPED, "循环采样必须置 LOOPED 标志"
        assert slen == pz.sample_len
        # ★ sample_off 是**整片 flash 的绝对地址**（含 LIB_BASE），不是片内偏移
        assert off == pz.sample_off
        assert off >= layout.LIB_BASE, \
            f"sample_off 0x{off:X} 未含 LIB_BASE 0x{layout.LIB_BASE:X}"
        # 关键帧表紧跟采样数据，所以 kf_off == len(blob 的数据部分)
        if pz.keyframes:
            assert kf_off != 0xFFFFFFFF
            # ★ 这一条是硬性约定：偏移**不含**关键帧表自身，
            #   多算了整张表就会去错误位置读关键帧 → 循环点全错。
            assert kf_off == pz.data_bytes, (
                f"keyframe_off {kf_off} 应等于采样数据长度 {pz.data_bytes}")
            assert kf_n == len(pz.keyframes)
        # 包络值域
        assert 1 <= ea <= 10000
        assert 0 <= es <= 32767
        assert er >= 5


def test_library_header_fields(tmp_path):
    """library.bin 的头部字段必须与 gmss_format.h 一致

    ★ 旧方案头部在 boot.bin 里；单片方案的头部是 library.bin 的头 64 字节。
    """
    placed, info, img = _build(tmp_path)
    b = (img / "library.bin").read_bytes()

    (magic, vmaj, vmin, hsz, srate, nzones, ninst, ndrum,
     zoff, ioff, poff, nchip, flags, _r0, tbytes, cks, _r1) = \
        struct.unpack_from("<IBBHIIHHIIIBBHIII", b, 0)

    assert magic == GMSS_MAGIC, f"魔数错: 0x{magic:08X}"
    assert (vmaj, vmin) == (1, 0)
    assert hsz == layout.HEADER_SIZE
    assert srate == 48000, "板载采样率必须是 48000"
    assert nzones == len(placed)
    assert nchip == 1, "单片方案：chip_count 恒为 1"
    assert zoff == layout.HEADER_SIZE, "zone 表必须紧跟头部"
    assert ioff >= zoff + nzones * layout.ZONE_SIZE
    # ★ 头部里的 pool_off 是**库内相对偏移**（与文件偏移同一坐标系），
    #   而 zone 里的 sample_off 是 flash 绝对地址 —— 两者相差 LIB_BASE。
    assert poff == info["pool_off"]
    assert info["pool_addr"] == layout.LIB_BASE + poff
    assert poff % layout.POOL_ALIGN == 0, "池偏移必须 4KB 对齐（突发读不跨页）"
    assert min(pz.sample_off for pz in placed) == info["pool_addr"], \
        "第一条采样必须正好落在池起点上"
    assert cks == info["checksum"]
    # total_bytes 是"库内相对"的末尾偏移：不能小于池起点，也不能超出文件
    assert info["pool_off"] <= tbytes <= len(b), \
        f"total_bytes {tbytes} 不在 [池起点, 文件长度] 区间内"


def test_zone_table_is_readable(tmp_path):
    """整个 zone 表必须能顺序读完且内容自洽"""
    placed, _, img = _build(tmp_path)
    b = (img / "library.bin").read_bytes()
    zoff, nzones, poff = (struct.unpack_from("<I", b, 20)[0],
                          struct.unpack_from("<I", b, 12)[0],
                          struct.unpack_from("<I", b, 28)[0])
    pool_addr = layout.LIB_BASE + poff

    sample_offs = set()
    for i in range(nzones):
        f = struct.unpack_from(
            "<BBBBBBHIIIIIIIBbhHHhHhHI", b, zoff + i * layout.ZONE_SIZE)
        (kl, kh, vl, vh, chip, codec, flags, off, slen, ls, le,
         attack, kf_off, kf_n, root, tune, gain,
         ea, ed, es, er, fx, _p2, rsv) = f

        assert 0 <= kl <= kh <= 127, f"zone{i} 键范围非法: {kl}-{kh}"
        # 力度 0 是合法的（MIDI 里 velocity 0 等同 note-off）
        assert 0 <= vl <= vh <= 127, f"zone{i} 力度范围非法: {vl}-{vh}"
        assert chip == 0, f"zone{i} 单片方案下 chip_id 应为 0，实际 {chip}"
        assert codec in (0, 1, 2), f"zone{i} codec 非法: {codec}"
        # ★ 采样偏移是 flash 绝对地址，必须落在池起点之后（旧版查的是片内
        #   POOL_OFFSET，那个常量已不再表示池的位置）
        assert off >= pool_addr, \
            f"zone{i} 采样偏移 0x{off:X} 落在池起点 0x{pool_addr:X} 之前"
        assert off < layout.LIB_BASE + len(b), f"zone{i} 采样偏移越出镜像"
        assert off % layout.POOL_ALIGN == 0, \
            f"zone{i} 采样偏移未按 {layout.POOL_ALIGN} 字节对齐: 0x{off:X}"
        assert slen > 0, f"zone{i} 采样长度为 0"
        assert 0 <= root <= 127
        assert -100 <= tune <= 100, f"zone{i} 音分修正越界: {tune}"
        if flags & layout.ZF_LOOPED:
            assert ls != 0xFFFFFFFF
            assert 0 <= ls < le <= slen, f"zone{i} 循环点越界: {ls}..{le} / {slen}"
            assert attack <= slen
        sample_offs.add(off)

    # ★ 语义变化：旧设计用"至少落在 2 片"来证明交错分配生效。
    #   单片方案没有片可分，等价的性质是"内容不同的采样必须落在不同偏移上"
    #   （测试数据的两个力度层内容特意做成不同，否则去重会合成一份）。
    assert len(sample_offs) >= 2, \
        f"2 个内容不同的采样应占 2 处偏移，实际 {sorted(sample_offs)}"


def test_library_image_is_valid(tmp_path):
    """单片镜像的头部、表区填充与文件长度（旧 test_chip_images_are_valid 的等价物）

    ★ 语义变化：旧方案每片一个 chipN.bin，各有 16 字节片头 + 0xFF 填充；
      单片方案只有一个 library.bin，头部就是 gmss_format.h 的 gmss_header_t，
      表区（头 + zone 表 + 音色表 + 鼓表）到池起点之间必须是 0xFF 擦除态。
    """
    placed, info, img = _build(tmp_path)
    p = img / "library.bin"
    assert p.exists(), "缺少 library.bin"
    assert not (img / "boot.bin").exists(), "单片方案不再产出 boot.bin"
    assert not (img / "chip0.bin").exists(), "单片方案不再产出 chipN.bin"

    lib = p.read_bytes()
    magic, vmaj, vmin, hsz = struct.unpack_from("<IBBH", lib, 0)
    assert magic == GMSS_MAGIC, f"魔数错: 0x{magic:08X}"
    assert hsz == layout.HEADER_SIZE
    assert len(lib) >= info["pool_off"], "镜像比池起点还短"

    # 表区之后、池起点之前必须是擦除态 0xFF
    table_end = (layout.HEADER_SIZE + len(placed) * layout.ZONE_SIZE
                 + 128 * layout.INSTR_SIZE
                 + info["drumkits"] * layout.INSTR_SIZE)
    assert table_end <= info["pool_off"], "表区越过了池起点"
    assert lib[table_end:info["pool_off"]] == \
        b"\xFF" * (info["pool_off"] - table_end), "表区与池之间未填充 0xFF"

    # 地图文件必须写出来（烧录时要靠它核对地址）
    map_txt = (img / "library_map.txt").read_text(encoding="utf-8")
    assert f"0x{layout.LIB_BASE:08X}" in map_txt
    assert f"pool_addr       = 0x{info['pool_addr']:08X}" in map_txt


def test_sample_data_lands_at_declared_offset(tmp_path):
    """★ 最关键的校验：zone 声明的偏移处必须真的有数据，且关键帧表落在分配区内。

    这一条如果不过，固件读出来就是 0xFF 而不是音频，
    现象是"完全没声音"或"循环点咔咔响"，而不是报错。

    ★ 坐标换算：sample_off 是 flash 绝对地址，文件偏移 = sample_off - LIB_BASE。
      漏掉这个减法就会去文件头部附近读，读到的全是表数据（同样是静默错误）。
    """
    placed, info, img = _build(tmp_path)
    lib = (img / "library.bin").read_bytes()

    # 直接拿 PlacedZone 的已知信息做精确检查
    for pz in placed:
        file_off = pz.sample_off - layout.LIB_BASE
        assert file_off >= info["pool_off"], \
            f"换算后的文件偏移 {file_off} 落在表区内（池从 {info['pool_off']} 开始）"

        head = lib[file_off:file_off + 64]
        assert head != b"\xFF" * 64, \
            f"文件偏移 {file_off} (flash 0x{pz.sample_off:X}) 处全是 0xFF，数据没写进去"

        # 关键帧表紧跟在采样数据之后，两者都必须落在本条的分配区内
        kf_bytes = len(pz.keyframes) * 4
        end = file_off + pz.data_bytes + kf_bytes
        assert end <= file_off + pz.alloc_bytes, (
            f"关键帧表越出分配区: sample_off={pz.sample_off} "
            f"data={pz.data_bytes} kf={kf_bytes} alloc={pz.alloc_bytes}")
        assert end <= len(lib), f"关键帧表越出文件: {end} > {len(lib)}"

        # 分配区必须被真实数据覆盖，不能全是 0xFF
        region = lib[file_off:end]
        assert region != b"\xFF" * len(region), "分配区内没有有效数据"

        # 关键帧表必须真的读得出来（声明的偏移处不是填充）
        if pz.keyframes:
            kf_file_off = file_off + pz.data_bytes
            assert lib[kf_file_off:kf_file_off + kf_bytes] != \
                b"\xFF" * kf_bytes, "关键帧表位置是 0xFF，偏移算错了"


def test_allocations_do_not_overlap(tmp_path):
    """★ 采样池里的分配区绝不能重叠（否则后一个采样盖掉前一个）

    ★ 语义变化：旧方案按 chip_id 分组、组内查重叠（"同一片内不能重叠"）；
      单片方案只有一个池，所以是**全体一条序列**查重叠 —— 检查更强而不是更弱。
      去重共享同一偏移的条目算出的是同一个区间，不算重叠。
    """
    placed, _, _ = _build(tmp_path)
    assert {pz.chip_id for pz in placed} == {0}, "单片方案下所有 chip_id 应为 0"

    spans = sorted((pz.sample_off, pz.sample_off + pz.alloc_bytes, i)
                   for i, pz in enumerate(placed))
    for (s0, e0, i0), (s1, e1, i1) in zip(spans, spans[1:]):
        assert e0 <= s1, (
            f"采样池分配区重叠: 第{i0}条 [0x{s0:X},0x{e0:X}) 与 "
            f"第{i1}条 [0x{s1:X},0x{e1:X}) 交叠")


def test_pool_overflow_raises(tmp_path):
    """采样池/镜像装不下时必须抛 LayoutError，不能静默溢出

    ★ v2 新增：容量上限是单片 16MB（LIB_CAPACITY）。把容量人为压到 1 个对齐块
      就能触发，不必真造 16MB 数据 —— 溢出若被静默忽略，烧出来就是一个
      "地址越界但校验和正确"的库，只有到固件读不到采样时才会发现。
    """
    sf2 = tmp_path / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, drum = plan.plan(sf)

    # 1) place()：池容量装不下第一条采样
    try:
        layout.place(mel + drum, capacity=layout.POOL_ALIGN, verbose=False)
    except layout.LayoutError:
        pass
    else:
        raise AssertionError("采样池溢出时 place() 应抛 LayoutError")

    # 2) write_images()：镜像超出给定容量
    placed = layout.place(mel + drum, verbose=False)
    try:
        layout.write_images(placed, tmp_path / "img_small",
                            build_name="T", capacity=1024, verbose=False)
    except layout.LayoutError:
        pass
    else:
        raise AssertionError("镜像超出容量时 write_images() 应抛 LayoutError")


def test_dedup_shares_one_copy(tmp_path):
    """内容相同的采样只编码一次，多个 zone 共享同一位置"""
    sf2 = tmp_path / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, drum = plan.plan(sf)

    # 人为构造两个引用同一 Sample 对象的 zone
    z0 = mel[0]
    z_dup = plan.PlannedZone(
        key_lo=0, key_hi=127, vel_lo=0, vel_hi=127,
        sample=z0.sample, attack_ms=1, decay_ms=1, sustain_q15=32767,
        release_ms=5, gain_db100=0, percussive=False, program=1, bank=0,
    )
    placed = layout.place([z0, z_dup], verbose=False)
    # ★ 语义变化：旧设计靠"落在同一片"表达共享（那时偏移是片内相对的）；
    #   单片方案下共享 = 同一个 flash 绝对地址，而且只占一份分配区。
    assert placed[0].chip_id == placed[1].chip_id == 0, "单片方案下都在 0 号池"
    assert placed[0].sample_off == placed[1].sample_off, "相同采样应共享偏移"
    assert placed[0].blob == placed[1].blob, "相同采样应共享同一份编码数据"


def test_instrument_table_partitions_zone_table(tmp_path):
    """★ instrument 表必须是"每个 (bank, program) 一段、段内 zone 连续"的精确切分

    钉住三条硬约定：
      1. 旋律音色表**固定 128 槽**（缺失的 program 补空条目）
      2. zone 表按 (bank, program) 重排，同组连续 —— 固件才能用
         (zone_first, zone_count) 顺序读；漏掉重排就会读到别的音色的 zone
      3. 校验和覆盖 zone 表 + 音色表 + 鼓表，且磁盘上那一段与内存里逐字节一致
    """
    sf2 = tmp_path / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, _ = plan.plan(sf)

    # 造两个 program，并给每个 program 打上可识别的键范围标签，
    # 这样从 zone 表反读就能判断某条 zone 属于哪个音色。
    tag = {(0, 2): (30, 40), (0, 5): (10, 20)}
    zones = [
        dataclasses.replace(mel[0], program=5, key_lo=10, key_hi=20, part="P5"),
        dataclasses.replace(mel[0], program=2, key_lo=30, key_hi=40, part="P2"),
        dataclasses.replace(mel[1], program=5, key_lo=10, key_hi=20, part="P5"),
    ]   # 故意乱序送入：重排必须把它们按 program 归拢

    placed = layout.place(zones, verbose=False)
    info = layout.write_images(placed, tmp_path / "img",
                               build_name="T", verbose=False)
    lib = (tmp_path / "img" / "library.bin").read_bytes()

    nz, ninst, ndrum, zoff, ioff = (
        struct.unpack_from("<IBBHIIHHIIIBBHIII", lib, 0)[i]
        for i in (5, 6, 7, 8, 9))
    assert nz == len(zones)
    assert ninst == len(tag), f"应有 {len(tag)} 个非空音色，实际 {ninst}"

    # 2) 音色表固定 128 槽：鼓表从 instr_off + 128*INSTR_SIZE 开始
    ranges = {}
    for slot in range(128):
        zf, zc, bank, prog, _fx, _pad = struct.unpack_from(
            "<HHBBbb", lib, ioff + slot * layout.INSTR_SIZE)
        if zc:
            ranges[(bank, prog)] = (zf, zc)
    assert set(ranges) == set(tag), f"音色表里的音色不对: {sorted(ranges)}"

    # 1) 区间必须无缝切分整张 zone 表（不重不漏）
    covered = sorted(i for zf, zc in ranges.values() for i in range(zf, zf + zc))
    assert covered == list(range(nz)), \
        f"instrument 区间未能无缝切分 zone 表: {sorted(ranges.items())}"

    # 2) 段内 zone 必须真的属于该音色（用键范围标签反查）
    for key, (zf, zc) in ranges.items():
        for i in range(zf, zf + zc):
            f = struct.unpack_from(
                "<BBBBBBHIIIIIIIBbhHHhHhHI", lib, zoff + i * layout.ZONE_SIZE)
            assert (f[0], f[1]) == tag[key], (
                f"zone{i} 键范围 {(f[0], f[1])} 不属于音色 {key} "
                f"（zone 表没有按 (bank, program) 重排？）")

    # 3) 校验和覆盖 zone 表 + 128 槽音色表 + 鼓表
    assert len(info["table_bytes"]) == \
        nz * layout.ZONE_SIZE + 128 * layout.INSTR_SIZE + ndrum * layout.INSTR_SIZE
    assert info["checksum"] == layout.fnv1a(info["table_bytes"])
    assert lib[zoff:zoff + len(info["table_bytes"])] == info["table_bytes"], \
        "磁盘上的表区与内存中的不一致"


def test_drum_table_is_written_and_covered(tmp_path):
    """鼓组（bank 128）音色表必须接在 128 槽旋律表之后，并被校验和覆盖"""
    sf2 = tmp_path / "test.sf2"
    build_sf2(str(sf2))
    sf = parse.load_soundfont(str(sf2))
    mel, _ = plan.plan(sf)

    drums = [dataclasses.replace(z, bank=128, program=0) for z in mel]
    placed = layout.place(drums, verbose=False)
    info = layout.write_images(placed, tmp_path / "img",
                               build_name="T", verbose=False)
    lib = (tmp_path / "img" / "library.bin").read_bytes()

    nz, ninst, ndrum, zoff, ioff = (
        struct.unpack_from("<IBBHIIHHIIIBBHIII", lib, 0)[i]
        for i in (5, 6, 7, 8, 9))
    assert nz == len(drums)
    assert ninst == 0, "全是鼓组时旋律音色数应为 0"
    assert ndrum == 1, "应识别出 1 个鼓组"
    assert info["drumkits"] == 1

    # 鼓表条目紧跟在 128 个旋律槽之后
    zf, zc, bank, prog, _fx, _pad = struct.unpack_from(
        "<HHBBbb", lib, ioff + 128 * layout.INSTR_SIZE)
    assert (bank, prog) == (128, 0)
    assert (zf, zc) == (0, len(drums)), "鼓组的 zone 区间应从 0 起覆盖所有鼓 zone"

    # 校验和必须把鼓表也算进去
    assert len(info["table_bytes"]) == \
        nz * layout.ZONE_SIZE + 128 * layout.INSTR_SIZE + ndrum * layout.INSTR_SIZE
    assert info["checksum"] == layout.fnv1a(info["table_bytes"])
    assert lib[zoff:zoff + len(info["table_bytes"])] == info["table_bytes"]


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
            tb = sys.exc_info()[2]
            while tb.tb_next:
                tb = tb.tb_next
            print(f"        于第 {tb.tb_lineno} 行")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)
