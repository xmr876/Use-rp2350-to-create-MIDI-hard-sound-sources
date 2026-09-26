#!/usr/bin/env python3
"""
ps5 — 让 TASCAM Pocketstudio 5 出声的工具包。

  gmnames.py  GM 音色表 + PS5 相关 MIDI 常量
  smf.py      SMF 文件写出/读回（纯标准库）
  piece.py    3 分钟乐曲（实时发送和存 SMF 共用同一份数据）

两个入口：
  tools/ps5_play.py      走 USB 转 MIDI 线，实时推给 PS5 的内藏音源
  tools/ps5_make_smf.py  生成 .MID 文件，拷进卡的 SMF 文件夹让它自己放
"""
