# B站 Hi-Res 无损音质投稿 · Pr 打包流程

对应工具：[tools/mux_hires.py](../tools/mux_hires.py)（封装 + 校验一条龙）。

## 0. 一句话结论

**Pr 自己一步导出不了 Hi-Res 投稿文件。**
Pr 的 H.264 / H.265（MP4）导出器音频只有 **AAC、采样率最高 48 kHz**，
而 B站 Hi-Res 判定明确写了「AAC 不属于无损音频编码格式」。

正确姿势是分两路导出，再用 ffmpeg 无损封装：

```
Pr ──► 纯视频 MP4（H.264/HEVC 高码率，取消勾选"导出音频"）┐
Pr ──► WAV 24bit / 96kHz（或 48kHz）                      ┘──► ffmpeg -c:v copy -c:a flac ──► 投稿
```

## 1. B站的三条判定（官方）

Hi-Res 无损音质 2022 年 7 月上线（[IT之家报道](https://www.ithome.com/0/631/177.htm)），
判定只看**你上传的源文件**里那条音轨：

| 条件 | 要求 |
|---|---|
| 音频编码 | ∈ {**alac, ape, dts, flac, mp4als, ralf, shorten, mlp, tak, wavpack, wmalossless, pcm**}；**AAC 不算** |
| 采样率 | **≥ 48 kHz**；≥96 kHz → 平台输出 96 kHz 版，48–96 kHz → 输出 48 kHz 版 |
| 位深 | **≥ 24 bit** |

- 平台侧会把音轨转成自己的 FLAC 无损流（[耳机大家坛实测讨论](http://erji.net/archiver/?tid-2367803.html)）。
- **观众端要大会员**才听得到 Hi-Res 档；早期投稿需要报名活动，现在一般按上述三条自动识别。
- 判定结果在投稿页 / 创作中心确认，别只看自己本地。

## 2. Pr 那边怎么设

| 步骤 | 设置 |
|---|---|
| 序列音频采样率 | 素材是 96 kHz → 设 96000 Hz；素材是 48 kHz → 就设 48000 Hz，**不要强行上采样**（判定能过，但信息量不增加，只是文件更大）。`序列 → 序列设置 → 音频` |
| 导出视频 | 格式 H.264 或 HEVC，**取消勾选"导出音频"**（关键），1080p 建议 16–20 Mbps / 4K 建议 45–80 Mbps，`yuv420p`，关键帧间隔 ≤2 秒 |
| 导出音频 | 格式改 **"波形音频 (Waveform Audio)"** → WAV；采样率 96 kHz（或 48 kHz）、**24-bit**、立体声 |
| 电平 | 总线上峰值留 **−1 dBTP** 余量，别用限幅器顶到 0 dBFS（转 24bit 整数时超 0 会直接削平） |

> 想在 Pr 里一步到位只有歪路：格式选 **QuickTime**、视频 H.264、音频"未压缩 (PCM)" → MOV。
> `pcm` 在白名单里，但 Pr 的 QT 导出器码率控制很粗，且 PCM 96k/24bit 立体声 ≈ 4.6 Mbps ≈ 2 GB/小时。不推荐。

## 3. 封装命令（本机 ffmpeg 9.0.2 实测通过）

### 推荐：MP4 + FLAC

```powershell
D:\音源\toolchain\ffmpeg.exe -y -i video_only.mp4 -i mix_24bit96k.wav `
  -map 0:v:0 -map 1:a:0 `
  -c:v copy -c:a flac `
  -ar 96000 -sample_fmt s32 -bits_per_raw_sample 24 `
  -ac 2 -movflags +faststart `
  out_hires.mp4
```

- `-map` 必须写死 → 成品里**只有 1 条视频 + 1 条音频**（B站只取第一条音频流，多一条就可能取错）
- `-c:v copy` 视频不重编码，Pr 导出的画质原样保留
- `-sample_fmt s32` 是 FLAC 编码器的要求（FLAC 内部用 s32 承载），`-bits_per_raw_sample 24` 才写进文件位深
- **ffmpeg 9.x 不需要 `-strict -2`**；4.x 老版本把 FLAC-in-MP4 当实验性封装，要补上

**备选**：MKV + FLAC（去掉 `-movflags`）；MOV + PCM（`-c:a pcm_s24le`，体积约 9 倍）。

## 4. 一键脚本

```powershell
python tools\mux_hires.py --probe <video> <wav>     # 只看参数，不封装
python tools\mux_hires.py <video> <wav>             # → build/video/<name>_hires.mp4
python tools\mux_hires.py <video> <wav> --mkv       # MKV + FLAC（容器备选）
python tools\mux_hires.py <video> <wav> --pcm       # MOV + PCM（不压缩）
```

脚本会先按 B站三条判定源 WAV，再封装，最后自动做四项校验：
**成品 ffprobe 参数 / 解码回 PCM 的 MD5 与源逐位比对 / 流数量 / faststart / 音视频时长**，
并顺带报 EBU R128 响度与真峰值。

## 5. 手动校验

```powershell
D:\音源\toolchain\ffprobe.exe -v error -select_streams a:0 `
  -show_entries stream=codec_name,sample_rate,channels,bits_per_raw_sample `
  -of default=nw=1 out_hires.mp4
# 期望：codec_name=flac / sample_rate=96000 / bits_per_raw_sample=24

# 证明真无损：两边的解码 PCM MD5 必须一模一样
D:\音源\toolchain\ffmpeg.exe -v error -i mix.wav -map 0:a:0 -f md5 -
D:\音源\toolchain\ffmpeg.exe -v error -i out_hires.mp4 -map 0:a:0 -f md5 -
```

## 6. 实测记录（2026-09-26）

输入：`4k.mp4`（HEVC 4K60，122.5 MB）+ `VID20260926124507_landscape_4k.wav`（PCM 24bit/96kHz，80.7 MB）

| 项目 | 结果 |
|---|---|
| 成品 | `build/video/4k_hires.mp4`，175.4 MB |
| 视频流 | hevc 3840×2160 60fps yuv420p 6994 kbps（copy，未重编码） |
| 音频流 | **flac 96000 Hz 24bit 2ch 3017 kbps** ✅ |
| 无损比对 | 源 `ceac10c6…` = 成品 `ceac10c6…` ✅ 逐位一致 |
| 流数量 / faststart / 时长 | 2 条 ✅ / moov 在前 ✅ / 两边均 2:27 ✅ |
| 响度 | **−14.0 LUFS，真峰值 −0.5 dBFS**（略热，见下） |
| 封装耗时 | 0.7 秒 |

## 7. 坑

1. **只留一条音频流**。Pr 忘了取消"导出音频"，ffmpeg 又没写 `-map` → 成品两条音轨，B站取错就白干。
2. **别把 48k 上采样当 96k**。判定能过，但没有任何实际收益。
3. **容器优先 MP4**。官方只规定音频编码，没规定容器；MP4 最稳，MKV 备选，投稿页报错就换回来。
4. **源视频码率偏低要留意**。`4k.mp4` 是 4K60 只有 **6994 kbps** 的 HEVC，B站二压后画质会吃亏；
   本目录里还有 `build/video/VID20260926124507_landscape_4k.mp4`（647 MB ≈ 35 Mbps）的母版，
   在意画质的话可以从它重出一版 20–40 Mbps 的再封装（`-c:v copy` 不影响音频流程）。
5. **真峰值 −0.5 dBFS 偏热**。无损不会削波，但离 0 太近，播放端任何增益都可能削顶；母版惯例 ≤ −1 dBTP。
6. 环绕声/DTS 那套是另一条线（杜比全景声），做普通 Hi-Res 就规规矩矩**立体声**。

## 8. 参考

- B站 Hi-Res 官方判定：[IT之家 · B站上线 Hi-Res 无损音质](https://www.ithome.com/0/631/177.htm)
- 平台输出规格讨论：[耳机大家坛 · b站的视频和音频的参数](http://erji.net/archiver/?tid-2367803.html)
- 本仓库相关工具：[mux_hires.py](../tools/mux_hires.py)、[mp4info.py](../tools/mp4info.py)、[nvenc_tune.py](../tools/nvenc_tune.py)、[rotate_video.py](../tools/rotate_video.py)
