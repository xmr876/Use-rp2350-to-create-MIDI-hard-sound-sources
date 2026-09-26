# TASCAM Pocketstudio 5 · 设置与播放

这份文档对应本目录的工具：

| 工具 | 作用 |
|---|---|
| `tools/ps5_play.py` | 走 USB 转 MIDI 线，**实时**把曲子推给 PS5 的内藏音源 |
| `tools/ps5_make_smf.py` | 生成 `.MID` 文件，拷进卡里让 **PS5 自己播放** |
| `tools/ps5_verify.py` | 一键跑完全部验证（结构 / SMF / 发送配对 / 画图） |
| `tools/ps5_check_piece.py` | 乐曲结构自检（挂音、复音、音域、通道合法性） |
| `tools/ps5_pianoroll.py` | 画出钢琴卷帘图，肉眼检查编曲 |

## 0. 五首曲子

所有工具都支持 `--piece` 选曲风：

| `--piece` | 曲子 | 长度 | 风格 |
|---|---|---|---|
| `piece`（默认） | 流行曲 | 80 小节 @ 96 BPM = **3 分 20 秒** | C–Am–F–G，钢琴/弦乐/木吉他 |
| `jrock` | 日摇 | 104 小节 @ 170 BPM = **2 分 27 秒** | Am→C→升全音 D，失真强力和弦 |
| `serene` | 悠扬 | 64 小节 @ 66 BPM = **3 分 53 秒** | g 小调→♭B→收在 G 大调，无鼓 |
| `solister` | Dream Solister 风 | 138 小节 @ 178 BPM = **3 分 6 秒** | 铜管乐队底的热血动漫 OP |
| `jpop` | **J-POP（16 复音）** | 60 小节 @ 120 BPM = **2 分 0 秒** | 王道进行 F-G-Em-Am-F-G-C，**峰值正好 16 复音** |

```powershell
python tools\ps5_play.py --piece jpop            # 实时推
python tools\ps5_polyphony.py                    # 数同时发声数（复音数）
python tools\ps5_channels.py                     # 五首的通道占用一览
python tools\ps5_collisions.py                   # 挂音检查（同音高重叠）
python tools\ps5_audit.py                        # 元数据一致性审计
python tools\ps5_verify.py --piece all           # 五首一起跑完整验证
```

### J-POP 这首：16 复音是**硬预算**，不是"多堆乐器"

做的是**上限 16**（不是至少 16）—— 给每一层分配固定的音符数，加起来
正好卡进 16。用户要的是"最大 16"，超了就没意义了。

| 层 | 音符数 | 内容 | 音区 |
|---|---|---|---|
| 钢琴 | 3 | 根音+三音+五音 | F3–A4 |
| 电钢 | 2 | 三音+五音（高八度） | 高一个八度 |
| 合成垫 | 2 | 五音+高八度根音 | 更高，空气感 |
| 弦乐 | 2 | 根音+三音（低八度） | 低八度，厚度 |
| （长音层小计） | **9** | | |
| 合唱 | 1 | 高八度五音 | 只在高频点一下 |
| 贝斯 | 1 | 根音 | |
| 主奏 | 1–3 | 单音线条（长音处叠到 2–3） | |
| 铜管和声 | 1 | 三度，只在副歌 | |
| 吉他 | 1 | 八分切分，**避开正拍** | |
| 鼓 | ~4 | 打击乐 | |

**实测各段峰值**（`ps5_polyphony.py` 数出来的，不是我估的）：

```
INTRO=7  V1=12  PRE=13  CHO=16  BREAK=5  V2=15  CHO2=16  OUTRO=10
```

两处关键编法，都是为了压住复音数：

1. **吉他避开正拍**（从第 2 个八分音符起）—— 正拍上已经有钢琴、
   贝斯、底鼓、吊镲挤在一起，吉他再叠一个就顶破 16。从反拍进既省
   预算，也更接近 J-POP 里实际扫弦的编法
2. **吊镲刻意做短**（0.4 拍而非 0.9）—— 拖长半拍就会多占一个发声

> ⚠️ **音高一律不重复**：同一个音高如果被两层同时按下，先到的
> note-off 会把后一个音也关掉 —— 硬件音源上就是挂音。所以每层取的
> 和弦音都是**不同的一批**。

**和声**用 J-POP 最经典的**王道进行**（小室進行 / royal road）：
`F - G - Em - Am - F - G - C`（IV-V-iii-vi-IV-V-I），C 大调，八小节一循环。
它让 IV 起头、最后才落到 I —— 那种"绕一圈才回家"的感觉就是这个味道的来源。

**其他四首的设计要点**：

**流行曲**：五段式起承转合，BREAK 段抽掉鼓和贝斯做对比。

**日摇**：
1. 主调 Am，副歌转 C 大调（关系大调）
2. 最后一遍副歌整体升全音（C→D、G→A、Am→Bm、F→G）
3. 失真吉他只用强力和弦（根音+五度+八度，**不加三度**）
4. 八分音符驱动贝斯；副歌切 16 分踩镲 + 反拍底鼓

**悠扬**：
1. 速度 66 BPM；**完全不用鼓**，铺底改用竖琴分解 + 合唱/弦乐长音
2. 主调 g 小调，中段转关系大调 ♭B，尾段收在 **G 大调**（皮卡迪三度）
3. 旋律以级进和长音为主，大量留白

**Dream Solister 风**（**不是原曲复制**，是同风格原创改编）：
1. **铜管是主角** —— 小号主奏 + 长号充当 euphonium 齐奏
2. 副歌用**三度和声**叠在旋律下方
3. 间奏是 show 段：长号连续八分音符领奏 + 小号对答
4. 和声骨架参考该曲公开的副歌走向（乐理信息，非旋律），
   含 **E7**（V/vi）与 **F#m7-5**（半减）两处招牌手法

#### 通道占用总表（0 起算 → 实际通道号）

| 通道 | 实际 | 用途 |
|---|---|---|
| 0 | 1 | 钢琴 |
| 1 | 2 | 贝斯 |
| 2 | 3 | 弦乐 |
| 3 | 4 | 吉他（木吉他 / 失真 / 清音） |
| 4 | 5 | 主奏（小号 / 长笛 / 主音合成器） |
| 5 | 6 | 铜管组 |
| 6 | 7 | 合唱 |
| 7 | 8 | 马林巴 |
| 8 | 9 | 暖垫 |
| **9** | **10** | **鼓组专用 —— 任何乐器都不能占** |
| 10 | 11 | 钟琴（悠扬） |
| 11 | 12 | 竖琴（悠扬） |
| 12 | 13 | 低音弦乐（悠扬） |
| 13 | 14 | 电钢 |
| 14 | 15 | 长号/上低音号、铜管和声 |
| 15 | 16 | 合成垫（J-POP，16 复音的主力铺底层） |

> **声像全部 = 64（居中）**，左右声道等量输出。
> 监听只接一路时，被推到一边的乐器会整轨听不见。`ps5_audit.py` 检查这条。

---

## 1. 先说最重要的一件事：PS5 不能录 MIDI

Reference Manual 第 5 章原文：

> You can also play MIDI directly into the Pocketstudio 5 from a keyboard
> or a sequencer, using the internal tone generator. **However, you cannot
> record MIDI sequences on the Pocketstudio 5 using this setup.**

所以有两条路，**建议两条都走一遍**：

- **路线 A（实时）**：电脑推 MIDI，PS5 当音源现场出声。
  —— 立刻能听到，不用卡、不用电脑识别设备。**先用这条确认链路通。**
- **路线 B（SMF）**：生成 `.MID` 放到卡上，PS5 自己当音序器播放。
  —— 这才是"PS5 自己会放"的正解，而且能一直重复放。

---

## 2. 接线

```
电脑 ──USB──▶ USB 转 MIDI 线 ──5 针 DIN──▶ PS5 的 MIDI IN
```

**关键**：USB 转 MIDI 线上有两个 DIN 头，丝印为 `MIDI OUT` 的那个
插到 PS5 的 **MIDI IN** 插座上（PS5 背板，5 针圆口）。

> ⚠ 这类廉价线的 IN/OUT 丝印经常是**反的**。如果完全没反应，
> 第一件事就是把两个头对调，再怀疑别的。

出声走这两条中的任意一条：

- **LINE OUTPUT**（3.5mm 立体声）→ 音箱 / 声卡
- **PHONES**（3.5mm）→ 耳机

---

## 3. PS5 上的设置（实时路线 / 路线 A）

### 3.1 开机与推子
1. 接好电源，开机。
2. **TG 推子**拉起来 —— 这是音源的音量，不拉就没声。
   （手册："Control the volume of the tone generator with the TG fader"）
3. **MASTER 推子**拉起来。
4. 线路输出 / 耳机接好，音量别开太大（GM 音源整曲齐奏时电平不低）。

### 3.2 音源模式（TgMode）
主菜单 → **SYSTEM** → `TgMode`，两个选择：

| 取值 | 含义 | 用哪个 |
|---|---|---|
| `Pattern` | 用 PS5 自带的伴奏型 | 不用 |
| `SMFPlay` | 放卡上的 SMF | **路线 B 用这个** |

**路线 A（电脑实时推 MIDI）**：两者都收 MIDI。`TgMode=Pattern` 时
MIDI 只认**通道 1**（手册："The MIDI instrument is only received on MIDI
channel 1"），其它通道的音会丢。所以：

> ✅ **实时推的时候，把 TgMode 设成 `SMFPlay`**，此时 16 个声部
> 按各自的 Rx.Ch 全通道接收，才能听到完整的 10 声部编曲。

### 3.3 各声部的接收通道（Rx.Ch）
主菜单 → **TG** 菜单 → 可以逐个声部设 `Instrument` / `Level` / `Pan` /
`Mute` / `Rx.Ch` / `ChoType` / `ChoSend` / `RevType` / `RevSend` / `KeyTrans`。

**正常情况下不用动**：工具在曲子开头会自己发一遍

- Program Change（音色）
- CC7 音量、CC10 声像、CC91 混响

但有两个前提要保证：

1. **PS5 的声部别被 Mute 了**。如果某个声部 `Mute=on`，工具发的音色
   和音符都被吃掉，那一轨就是哑的。
2. **Rx.Ch 保持默认**（声部 1↔通道 1 … 声部 10↔通道 10）。
   如果你以前改过，按这个对应改回来，或者用 TG 菜单一个个核对。

### 3.4 通道 → 乐器对照表

**流行曲（`--piece piece`）**

| 通道 | 声部 | 音色（GM 号） | 在这首曲子里干什么 |
|---|---|---|---|
| 1 | 钢琴 | Acoustic Grand Piano (0) | 分解和弦 / 柱式和弦 / 律动 |
| 2 | 贝斯 | Electric Bass finger (33) | 根音 + 走动经过音 |
| 3 | 弦乐 | String Ensemble 1 (48) | A/C 段旋律，BREAK 段和弦垫 |
| 4 | 木吉他 | Acoustic Guitar nylon (24) | 16 分琶音 |
| 5 | 长笛 | Flute (73) | C 段对答句 |
| 6 | 铜管 | Brass Section (61) | 反拍重音 |
| 7 | 合唱 | Choir Aahs (52) | 铺底长音 |
| 8 | 马林巴 | Marimba (12) | BREAK 段主题 |
| 9 | 暖垫 | Pad 2 warm (89) | 中频铺底 |
| 10 | 鼓 | 鼓组（通道 10 专用） | 底鼓/军鼓/踩镲/过门 |

**日摇（`--piece jrock`）** —— 只占 5 个声部，把空间留给吉他和鼓

| 通道 | 声部 | 音色（GM 号） | 在这首曲子里干什么 |
|---|---|---|---|
| 2 | 贝斯 | Electric Bass pick (34) | 八分音符驱动，全曲不停 |
| 4 | 吉他 | Distortion Guitar (30) | 强力和弦节奏（动机/闷音/副歌长和弦） |
| 5 | 主音合成器 | Lead 2 sawtooth (81) | 主旋律 + SOLO 段 16 分独奏 |
| 9 | 暖垫 / 清音吉他 | Pad 2 warm (89) → Clean Guitar (27) | CHO/CHO2 铺底；**V2 段切成清音吉他** |
| 10 | 鼓 | 鼓组（通道 10 专用） | 底鼓/军鼓/16 分踩镲/吊镲/过门 |

> ⚠ **一个通道只能有一种乐器**（PS5 每个声部一个音色）。日摇里通道 9
> 在 V2 段（53-64 小节）被切成清音吉他，是因为暖垫那段正好是停的 ——
> 两者时间上完全不重叠。这是靠 `SECTION_SWAP` 在段边界发 Program Change
> 实现的。`ps5_check_piece.py` 会检查 SETUP 里有没有通道被定义两遍。

> PS5 是 **128 个 GM 音色 + 5 套鼓组**，且手册规定**只有通道 10 能放鼓组**
> （"you can only assign drum kits to part 10"）。两首曲子都是这么分配的。

### 3.5 效果
音源自带两个效果：**合唱** 和 **混响/延迟**。它们和音频轨的效果是
**完全独立**的两套，不能互相用。本曲用 CC91 给各声部设了混响量
（弦乐 72、合唱 80，贝斯 18 保持干净）。想改的话在 TG 菜单里改
`RevSend` / `ChoSend`。

---

## 4. PS5 上的设置（SMF 路线 / 路线 B）

### 4.1 生成文件
```powershell
python tools\ps5_make_smf.py --verify                 # 流行曲 → PS5TUNE.MID（约 28 KB）
python tools\ps5_make_smf.py --piece jrock --verify   # 日摇   → JROCK.MID（约 40 KB）
```
两个文件名都已经是 **8.3 格式 + `.MID`** —— PS5 只认这个。

### 4.2 把文件弄进卡里
PS5 自己就是一个 USB 读卡器，不需要额外的读卡器：

1. PS5 关机，拔掉和电脑之间的 USB 线。
2. **按住 `ENTER` 键不放，然后开机** → 屏幕显示 **`USB MODE`**。
3. 松手，用标准 USB 线把 PS5 连到电脑。
4. 电脑上会出现一个**可移动磁盘**。
5. 把 `PS5TUNE.MID` 拷到卡上的 **`SMF`** 文件夹里。

> ⚠ **必须放进 `SMF` 文件夹**。手册："Make sure that you copy the SMFs
> to the SMF folder on the card. If you copy them anywhere else, you will
> not be able to use them with the Pocketstudio 5."
>
> ⚠ 文件名要 **8.3**（主名 ≤8 字符）。超了 PS5 会自动截断改名，
> 可能和卡上别的曲子撞名。

6. 按操作系统的"安全弹出"流程断开，拔线，PS5 重新正常开机。

### 4.3 在 PS5 上加载并播放

1. 主菜单 → **CARD** 菜单 → 光标移到 **`SMF LOAD`** → `ENTER`。
2. 光标选到 **`PS5TUNE`** / **`JROCK`** → `ENTER`。
3. 加载完成后，音源会**自动切到 `SMFPlay`**（手册："When an SMF is
   loaded, the tone generator is automatically set to play back SMFs"）。
   可以在 SYSTEM 菜单里用 `TgMode` 确认一下。
4. 按 **`PLAY`**。（`STOP` / `REW` / `F FWD` / `MARK` 都是正常走带键）
5. **TG 推子 + MASTER 推子**拉起来 —— 和路线 A 一样，不拉没声。

#### ⚠️ 屏幕上显示 `SONG1` 是正常的，不是错误

`SONG1` 是 PS5 **自己创建的那首歌的名字**。手册第 39 页：
"a new song is created with the name SONGx"；格式化卡时也会自动建一首。
所以主页显示 `SONG1` 只说明**这张卡在 PS5 上是认的**（这其实是好消息）。

**判断 SMF 有没有加载成功，要看 TG 屏幕**：

| 看哪里 | 加载成功 | 没加载 |
|---|---|---|
| **TG 屏幕**（主菜单 → TG） | 顶部显示 **SMF 的序列名或文件名** | 不显示 |
| 主页 | 仍然显示 `SONG1`（正常） | 显示 `SONG1` |
| `SYSTEM → TgMode` | 自动变成 **`SMFPlay`** | 还是 `Pattern` 或 `Bouncing` |

> 换句话说：**主页显示 `SONG1` 和 SMF 加载成功并不矛盾**，两个是不同层面。
> 只有 `SMF LOAD` 列表里根本看不到你的文件，才是真问题。

#### SMF LOAD 列表里看不到文件？按顺序查

1. 文件是不是在卡的 **`SMF`** 文件夹里（丢在根目录或别处 PS5 一律看不见）
2. 文件名是不是 **8.3**（主名 ≤8 字符）+ **`.MID`**
3. 卡在电脑上打开后，**根目录**应该长这样：

```
（卡根目录）/
├── SMF/          ← .MID 文件放这里
├── MP3/          ← 混音成品放这里
├── SONG1/        ← 歌曲文件夹（含 MTRK.PKT / SONGINFO.PKT）
├── SONG2/ ...
├── FXPATCH.PKT
├── PATTERN.001
└── SYSINFO.PKT
```

4. **`SMF` 文件夹是不是不见了**？手册明确警告（第 36 页）：
   "Do not delete or rename these folders or files. If you do, you will
   not be able to access the data on the card from the Pocketstudio 5."
   如果被删了或改了名，**PS5 就认不出卡上的数据**了。
   这种情况下按手册第 40 页的 **"Optimizing a card"** 流程让 PS5 重建目录结构。

#### 先用探针文件排除变量

`build\SMF\PROBE.MID`（672 字节，格式 0，单轨，55 秒 C 大调音阶）
是专门用来做对照实验的：

- 它**只用 MIDI 通道 1 + 钢琴音色**，所以即使 `TgMode` 还停在
  `Pattern` 模式（只收通道 1）也应该能响
- 格式 0 是最老最通用的 SMF，兼容性最好

```powershell
python tools\ps5_probe_smf.py    # 重新生成
python tools\verify_smf.py       # 三个文件一起自检
```

| PROBE.MID 的表现 | 说明什么 |
|---|---|
| 能加载、能响 | 卡的路径和加载流程都没问题 → 再看 `PS5TUNE.MID` / `JROCK.MID` |
| 能加载但不响 | 加载流程对，问题在音源设置（TG 推子 / TgMode） |
| 列表里看不到 | 问题在"文件位置"或"卡的目录结构"（见上面第 3、4 条） |

**加载后你还能做的调整**（SYSTEM 菜单）：

- `Tempo`：50% ~ 200% 变速。MIDI 变速**不改变音高**，想慢速跟弹很方便。
- TG 菜单里可以改每个声部的音色 / 音量 / 声像 / 移调（±36 半音）/
  静音 —— 比如想跟弹旋律，就把**通道 3（弦乐）Mute 掉**。
- 建议把时间显示切成**小节/拍**（`Time:BAR`），比分秒直观。

### 4.4 想删掉
`CARD` 菜单 → **`SMF DELETE`** → 选 `PS5TUNE` → `ENTER` → 显示 `Complete!!`。
**没有撤销**，删前在电脑上留个备份。

---

## 5. 跑起来

### 5.1 先看设备
```powershell
python tools\ps5_play.py --list
```
```
  [0] Microsoft GS Wavetable Synth  ← 微软自带，不是你的 USB-MIDI
  [1] USB2.0-MIDI  ← USB-MIDI
  [2] MIDIOUT2 (USB2.0-MIDI)  ← USB-MIDI
```

这类线在 Windows 上常枚举出**两个**输出端口，到底哪个对应线上丝印为
`MIDI OUT` 的 DIN 头，各家驱动不一样。默认自动选第一个 USB 设备；
**如果没声音，先换 `--device 2` 试试**，或者直接用
`--all-devices` 两个一起发，排除"发错端口"这个变量。

### 5.2 先做链路自检
```powershell
python tools\ps5_play.py --selftest
```
它会依次发：GM System On → 钢琴音阶 → 鼓组节奏 → 三段换音色对比。
每一步都告诉你**应该听到什么**。全听到了再跑整曲。

### 5.3 跑整曲
```powershell
python tools\ps5_play.py                 # 完整 3 分 20 秒
python tools\ps5_play.py --from A --to B # 只放 A、B 两段
python tools\ps5_play.py --dry-run       # 不发 MIDI，只看时间表
python tools\ps5_play.py --all-devices   # 两个端口一起发
```

播放时会实时打印段落和进度：
```
▶ A  全乐队进入   [0:21.20]
    … 1:01.20 / 3:21.20
```

### 5.4 出问题之前，先跑一遍验证
```powershell
python tools\ps5_verify.py
```
四项检查，任意一项失败都说明工具链有问题而不是设备问题：

1. **乐曲结构自检** —— 挂音、复音数、音域、通道 10 是否只放打击乐
2. **SMF 生成并解析回来** —— 文件本身是否合法
3. **发送时序配对** —— 对全部 15 种 `--from/--to` 片段组合，
   逐一确认每个 note-on 都有配对的 note-off。**这项最要紧**：
   实时发送是"发一条算一条"，漏掉一个 note-off 在硬件上就是挂音。
4. **钢琴卷帘图** —— 画出来给人看一眼

当前状态：**全部通过**（全曲 6719 条消息，时长 201.4 秒，零挂音）。
Windows 自带的 MCI 音序器也确认这个 SMF 合法（`1281` 个歌曲指针单位
= 80 小节）。

---

## 6. 出问题怎么查

| 现象 | 先查这个 |
|---|---|
| **完全没声音** | ① 两个 DIN 头对调 ② 确认插的是 PS5 **MIDI IN** ③ TG + MASTER 推子 ④ 换 `--device 2` |
| 有声音但**只有钢琴** | `TgMode` 还在 `Pattern` → 改成 `SMFPlay`（Pattern 模式只收通道 1） |
| **某几轨不出声** | TG 菜单里看那几个声部是不是 `Mute=on`；核对 `Rx.Ch` |
| **有音一直挂着不停** | 按 `STOP`；工具结束时也会发 All Notes Off（CC123/CC120）。反复出现请跑 `ps5_check_piece.py` |
| **音色不对** | PS5 上手动在 TG 菜单改 `Instrument`；或检查有没有被 `KeyTrans` 移调 |
| SMF **在卡上找不到** | 文件是不是放在 `SMF` 文件夹；文件名是不是 8.3 + `.MID` |
| **USB MODE 连不上电脑** | 手册明确说 PS5 只支持到 Win XP / Mac OS 9，新系统可能不认。**这种情况下走路线 A（实时 MIDI），它不依赖 USB 存储** |

---

## 7. 改这首曲子

乐曲全部在 `tools/ps5/piece.py` 里，纯 Python 数据，改完两个工具都会跟着变：

- **速度和长度**：`BPM`（现在是 96）。80 小节 4/4，所以
  `时长 = 80 × 4 × 60 / BPM` 秒。想刚好 3 分钟整 → `BPM ≈ 106.7`。
- **和声**：`CHORDS` 和 `PROGRESSION`（现在是每小节一个 C–Am–F–G）。
- **曲式**：`SECTIONS` 里改每段的起止小节。
- **配器**：`SETUP` 一张表管音色/音量/声像/混响，实时发送和 SMF 共用。
- **旋律**：`MELODY_A_PHRASE` / `MELODY_B_PHRASE`，格式是
  `(起始拍, 时值, 音级偏移, 力度)`。

改完建议按顺序跑一遍：

```powershell
python tools\ps5_check_piece.py      # 结构自检，必须 0 问题
python tools\ps5_pianoroll.py        # 画图看一眼
python tools\ps5_make_smf.py --verify
```

### 写这首曲子时踩到的三个坑（改的时候注意）

1. **同音高重叠 = 挂音**。两个同通道同音高的事件在时间上重叠时，
   先到的 `note-off` 会把后一个音也关掉，留下一个永远关不掉的
   `note-on`。`_dedupe()` 会在最后兜底，但**最好在源头就别写出来**。
2. **旋律主题长 16 拍（4 小节）就必须每 4 小节放一次**。放得比这密
   （比如每 2 小节一次）会让同一句自我叠加 —— 这正是第一版 52 个
   挂音的来源。见 `MELODY_INTERVAL_BARS`。
3. **一个通道只能是一种乐器**。PS5 每个声部一个音色，所以铺底不要跟
   旋律共用通道；`pad_upper()` 特意省掉根音，就是让铺底待在中间层，
   不去撞旋律和贝斯的落音。

---

## 8. 参考

- TASCAM Pocketstudio 5 Reference Manual（本目录 `build/ps5_ref_clean.txt`
  是从官方 PDF 解密+抽取出来的纯文本，可以直接搜）
  - 第 4 章：Patterns（伴奏型、和弦进行、风格列表）
  - 第 5 章：Standard MIDI files and the Pocketstudio 5（SMF 全流程）
  - 第 7 章：Data, cards, etc.（卡的目录结构、USB MODE）
  - 第 8 章：MIDI Implementation Chart + Specifications
- 官方 PDF：<https://www.tascam.eu/en/docs/ps5_ref.pdf>
- Sound On Sound 评测：<https://www.soundonsound.com/reviews/tascam-pocketstudio-5>
