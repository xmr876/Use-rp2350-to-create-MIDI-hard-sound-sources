#!/usr/bin/env python3
"""
slicing.py — 多首曲子共用的两件事：去重叠 + 片段切片。

抽出来的原因：这两块逻辑跟"曲子长什么样"无关，只跟"怎么把事件安全地
发给硬件音源"有关。两个曲风（piece.py 的流行曲、jrock.py 的日摇）
都必须用同一份，否则修了一边漏一边。

★ 这两件事为什么必须做，见各自函数的注释 —— 在硬件音源上写错就是挂音。
"""
from __future__ import annotations


def dedupe(events, score_cls):
    """去掉「同通道 + 同音高 + 时间重叠」的音。

    为什么必须做：两个同音高的事件重叠时，**先到的那个 note-off 会把
    后一个音也关掉**，剩下一个永远关不掉的 note-on —— 在硬件音源上
    就是挂音（stuck note）。这里让先开始的音优先，后来的重叠音直接丢。
    """
    out = score_cls()
    last_end: dict[tuple[int, int], float] = {}
    dropped = 0
    for e in sorted(events, key=lambda x: (x.beat, x.ch, x.pitch)):
        k = (e.ch, e.pitch)
        if e.beat < last_end.get(k, -1e9) - 1e-9:
            dropped += 1
            continue
        last_end[k] = e.beat + e.dur
        out.events.append(e)
    out.dropped = dropped
    return out


def slice_events(evs, sections, beats_per_bar, bpm, i0, i1,
                 lead: float = 1.2, tail_sec: float = 2.5):
    """把实时事件切成 [段 i0 .. 段 i1] 的片段。

    evs 是 (秒, MIDI 字节, 说明) 的列表，秒从曲子开头算（含 lead 前导）。

    返回 (事件序列, offset)，offset 是片段里时间 0 对应的原曲时刻。

    ★ 关键是**收尾要配对闭合**，不能只按时间硬切：
      如果切在某个音的 note-on 之后、note-off 之前，那个音就永远关不掉
      （硬件音源上就是挂音）。做法和 DAW 里剪切一个 MIDI 片段一样：
      片段末尾统一给所有仍在按着的音补一个 note-off。

      为什么不去"找回每个音真实的余音"：长音垫子的真实 note-off 可能落在
      几百秒之后（下一段），而主体之后的 note-on 又绝对不能带进来（那属于
      下一段）。两者一混就会出现"补了 A 音的 off、却漏了 B 音的 off"这种
      边界 bug。与其绕，不如在片段末尾统一关掉：数学上保证干净。
    """
    spb = 60.0 / bpm
    t_lo = sections[i0][1] * beats_per_bar * spb + lead
    t_hi = sections[i1][2] * beats_per_bar * spb + lead
    offset = t_lo - lead

    # 初始化消息永远保留，并挪到最前面
    out = [(0.05, msg, lbl) for t, msg, lbl in evs if t < lead + 0.5]

    # 主体
    out.extend((t - offset, msg, lbl) for t, msg, lbl in evs
               if lead + 0.5 <= t <= t_hi)
    # 尾部自然收尾（最后一个音的余韵）
    out.extend((t - offset, msg, lbl) for t, msg, lbl in evs
               if t_hi < t <= t_hi + tail_sec)

    # 统一收尾：给还按着的音补 note-off
    bal: dict[tuple[int, int], int] = {}
    for _t, msg, _lbl in out:
        if len(msg) == 3 and (msg[0] & 0xF0) == 0x90:
            k = (msg[0] & 0x0F, msg[1])
            bal[k] = bal.get(k, 0) + (1 if msg[2] > 0 else -1)

    end_t = max((t for t, _m, _l in out), default=0.0) + 0.05
    for (ch, pitch), cnt in sorted(bal.items()):
        for i in range(max(0, cnt)):
            out.append((end_t + i * 0.002, (0x90 | ch, pitch, 0),
                        "片段收尾：关掉仍在按着的音"))

    out.sort(key=lambda x: x[0])
    return out, offset
