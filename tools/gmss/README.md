# GMSS 音色库转换工具链

把标准 `.sf2` 音色库转换成 RP2350 板载 Flash 使用的 GMSS 二进制格式。

## 目录结构

```
tools/gmss/
├── gmss/
│   ├── riff.py       RIFF 容器解析（sf2 外壳）
│   ├── sf2defs.py    SF2 记录结构、生成器枚举、单位换算
│   ├── parse.py      hydra 记录 → Python 对象
│   ├── adpcm.py      IMA-ADPCM 编解码 + 混合编码 + 关键帧定位
│   ├── plan.py       GM 映射、zone 规划、统一重采样
│   ├── layout.py     单片 Flash 布局、二进制打包、镜像输出
│   ├── build.py      命令行入口
│   └── __init__.py
└── tests/
    ├── make_test_sf2.py   合成一个最小合法 .sf2（无需真实音色库即可测试）
    ├── test_parse.py      解析器测试（6 项）
    ├── test_adpcm.py      ADPCM 编解码测试（12 项）
    ├── test_pipeline.py   端到端 + 格式对拍（11 项）
    ├── test_player.py     参考播放器回读对拍（9 项）
    └── tmp/               测试生成物
```

## 快速开始

```bash
cd tools/gmss

# 跑全部测试（38 项）
python -m pytest tests -q

# 也可以单独跑（不依赖 pytest）
python tests/test_parse.py
python tests/test_adpcm.py
python tests/test_pipeline.py
python tests/test_player.py

# 构建镜像
python -m gmss.build soundfont.sf2 --out build/font/
```

依赖：Python 3.10+、numpy。**不依赖任何第三方 SF2 库**（自己解析，避免依赖链和
许可证问题）。

## 已实现

- [x] RIFF 容器解析，含 chunk 奇偶对齐处理
- [x] `phdr` / `pbag` / `pgen` / `inst` / `ibag` / `igen` / `shdr` 全部记录解析
- [x] zone 全局区（global zone）自动合并到 local zone
- [x] keyRange / velRange 字节序正确处理
- [x] timecents / centibels 单位换算
- [x] SF3（Ogg 采样）检测并明确报错，不产出垃圾
- [x] **IMA/DVI ADPCM 编解码器**（纯整数，与 C 版逐位一致）
- [x] **混合编码**（起音 PCM16 + 循环段 ADPCM），实测 **3.3 倍压缩**
- [x] **ADPCM 关键帧定位**（固件跳循环点走的就是这条路径）
- [x] **GM 映射与 zone 规划**（三层引用展开、包络合并、统一重采样到 48kHz）
- [x] **单片 Flash 布局**（顺序紧凑排列 + 4KB 对齐 + 内容去重）
- [x] **镜像输出**（`library.bin` + `library_map.txt`，带长度断言与校验和自检）
- [x] **参考播放器**（`player.py` 从镜像回读，与规划数据逐位对拍）
- [x] **命令行入口** `python -m gmss.build`
- [x] 测试 **38 项**（解析 6 + ADPCM 12 + 端到端 11 + 播放器 9）

## 待实现

- [ ] `flash.py` — USB CDC 刷写协议客户端（协议见 `docs/烧录与编程指南.md` §2.3）

## 镜像布局（v2 单片）

产物只有**一个** `library.bin`，**文件偏移 0 对应 flash 地址 `GMSS_LIB_BASE` = 0x40000**
（XIP 视角 `0x10040000`）。同一目录另有 `library_map.txt` 记录池地址/校验和/大小，
烧录前拿它对账 —— 烧到错误地址的库不会报错，只会静音。

```
文件偏移            内容
+0x000000           gmss_header_t        (64 B)
+0x000040           gmss_zone_t[]        (58 B × N)
+...                gmss_instrument_t[]  (32 B × 128，旋律槽固定 128 个)
+...                鼓组音色表            (32 B × D)
+...                0xFF 填充到 4KB 对齐
+pool_off           采样池                每条采样 4KB 对齐
```

★ **寻址约定（最容易搞错的一点）**：

| 字段 | 坐标系 |
|---|---|
| `gmss_zone_t.sample_off` | **flash 绝对地址**（含 `LIB_BASE`）；索引文件要减 `LIB_BASE` |
| `gmss_zone_t.keyframe_off` | **相对 `sample_off` 的偏移**，等于采样数据部分长度（不含关键帧表自身） |
| 头部的 `pool_off` / `total_bytes` | **库内相对偏移**（== 文件偏移） |

`layout.PlacedZone.chip_id` 与头部的 `chip_count` 保留但恒为 0 / 1 —— 它们只是
旧多片方案的字段占位，用来保持 C 结构体长度不变。

`library.uf2` 也已生成，目标地址写进 UF2 头，可以直接拖进 RPI-RP2 盘。

## 混合编码的数据布局（固件必须一致）

一个 zone 的 blob：

```
[ PCM16 × attack_samples ] ++ [ ADPCM 全长度流 ]
```

**ADPCM 流覆盖整个采样缓冲区**（不是只覆盖尾部）。前 `attack_samples` 个点的
ADPCM 编码是冗余的（解码时被 PCM 覆盖），换来三个好处：

1. **关键帧索引 == 采样点索引**，无需偏移换算，解码器不可能算错
2. ADPCM 解码器在第 `attack` 点处处于正确状态（不是从 0 重启），交界无跳变
3. 循环点定位只有一套逻辑：查关键帧 → 顺序解码

取第 `p` 个采样点：
```
p <  attack → blob[p*2] 起的 int16（无损）
p >= attack → blob[attack*2 + p>>1] 的 nibble，奇偶由 p&1 决定
             ★ 注意是 p>>1 而不是 (p-attack)>>1
```

冗余代价 = `attack_samples/2` 字节/zone（典型 240B，全库约 0.5MB），可接受。

Python 参考实现：`adpcm.decode_hybrid()` 是固件读采样的唯一入口。

## 关于 ADPCM 段的"误差"（曾误判为缺陷）

**结论：不存在缺陷。ADPCM 是有损压缩，读回来必然有量化误差。**

这一段曾被当成"已知缺陷"排查了多轮，实际上是**测试断言写错了**：
断言"读回 == 规划数据"在 ADPCM 段是错误期望。

### 为什么有误差

| 数据 | 含义 |
|---|---|
| `samples[]`（plan 里） | **重采样后的原始数据**（16bit） |
| 镜像里存的 | ADPCM 压缩码字（4 bit/采样） |
| 读回来的 | **有损重建**的结果 |

PCM 起音段（前 `attack_samples` 帧）设计上无损，两者逐位相等；
ADPCM 段必然有量化误差。

### 实测重建质量（测试数据）

| 指标 | 值 |
|---|---|
| 平均误差 | 25.2（满幅的 0.077%） |
| 最大误差 | 82 |
| **SNR** | **≈ 51 dB** |

符合 IMA ADPCM 的预期。`test_adpcm_quantization_is_reasonable`
用 SNR（25~80dB 区间）而不是"相等"来判定。

### 排查过程中排除掉的可能（都有证据）

| 检查 | 结果 |
|---|---|
| 单步 `encode_nibble`/`decode_nibble` round-trip | ✅ 正确 |
| 镜像 PCM 段 == 规划数据 | ✅ 逐字节相等 |
| 镜像 ADPCM 段 == 重新编码 | ✅ 逐字节相等 |
| 关键帧表写读一致（437 条） | ✅ 全对 |
| `decode_at` 定位 == 顺序解码 | ✅ 已证明 |
| ADPCM 段重建 SNR | ✅ 51 dB（正常） |

## 踩过的坑（改代码前务必读）

这些都是写测试时实际抓到的 bug，不是理论风险：

1. **`shdr` 是 46 字节，`inst` 是 22 字节，`phdr` 是 38 字节。**
   三个都不一样。`inst` 尤其反直觉（名字 20 字节 + 一个 u16 就完了）。
   写错会导致采样寻址整体错位，而且**不会报错**，只会声音全乱。

2. **keyRange / velRange 的字节序。**
   SF2 规范 p.19 的 `rangesType` 是 `{uint8 byLo; uint8 byHi;}`：
   **低字节 = 低值，高字节 = 高值**。编码 `(hi << 8) | lo`。
   网上有资料声称"低字节=高音、高字节=低音"，**那是错的**
   （那个说法与它自己举的 `0-60 → 15360` 例子自相矛盾）。
   权威佐证：NAudio 的 `SampleMap.KeyLowRange = KeyRange & 0xFF`。

3. **终端 bag 的 `genNdx` 指向终端记录自身**，即等于真实生成器条数。
   所以区间钳位必须用 `total_gens` 而不是 `total_gens - 1`，
   否则最后一条真实生成器会被当成终端丢掉（症状：zone 少一个生成器）。
   见 `parse.py::_parse_zones` 里的注释。

4. **preset/instrument 的第一个 zone 若是全局区，必须不含任何生成器。**
   一旦把 keyRange 塞进全局区，它就不再被识别为 global，
   于是多出一个空 zone 并且 local zone 拿不到全局参数。

5. **timecents 换算：`秒 = 2^(tc/1200)`**，不是 `2^(tc/1200)/1000`。
   tc=0 → 1 秒，tc=1200 → 2 秒，tc=-1200 → 0.5 秒。

6. **循环采样：循环段长度必须是整数个周期，且淡出不能侵入循环段。**
   前者保证相位连续，后者保证循环段内没有音量变化。两者缺一都会咔哒。

7. **ADPCM 的两个定位参数是独立的，别混为一谈。**
   `nibble_base`（第 0 个 nibble 对应哪个采样点，决定**奇偶**）和
   `byte_offset`（ADPCM 段在 blob 里的**起始字节**）是两回事。
   混合编码下必须同时给对，否则 nibble 会整体错位。

8. **从中间开始解码必须先按关键帧恢复状态。**
   编码器从 sample 0 起跑，第 `attack` 点处的 predictor 早就不是初值了。
   直接用初始状态解码会得到完全错误的数据。
   固件里这就是"定位循环点"的路径 → 用 `decode_hybrid()`。

9. **`decode()` 的 `n_samples` 和 `nibble_count` 要分清。**
   在流中间取一段时，返回点数 ≠ 需要迭代的 nibble 数，
   否则要么越界要么读错位置。

10. **`keyframe_off` 必须等于「采样数据部分的长度」，不含关键帧表自身。**
    曾经把关键帧表追加到 `blob` 之后再用 `len(blob)` 当偏移，
    结果偏移多了整张表的长度（1748 字节），
    固件会去错误位置读关键帧 → 循环点定位全错。
    修法：`blob` 只放采样数据，关键帧表在 `write_images()` 里拼接。
    见 `layout.py::PlacedZone.data_bytes` 的注释。
