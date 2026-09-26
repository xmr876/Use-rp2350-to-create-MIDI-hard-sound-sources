#!/usr/bin/env python3
"""
ps5_verify.py — 一键跑完全部验证。

三道验证，任何一道失败都说明曲子或工具链有问题：
  1. 结构自检    tools/ps5_check_piece.py   —— 挂音/复音/音域/通道合法性
  2. SMF 生成    写出 .MID 并解析回来       —— 文件本身是否合法
  3. 发送时序    模拟播放的调度逻辑         —— 有没有 note-on 配不上 note-off
  4. 钢琴卷帘图  画图                       —— 人能看一眼

第 3 项是关键：实时发送是"发一条算一条"，如果某个 note-on 因为
片段裁剪被发出去、对应的 note-off 却被切掉了，硬件上就是挂音。
这里用和 play() 一样的裁剪逻辑，检查配对是否完整。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = r"D:\音源"
PY = sys.executable

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def run(title: str, args: list[str], tail: int = 12) -> bool:
    print(f"\n{'=' * 72}\n== {title}\n{'=' * 72}")
    r = subprocess.run([PY] + args, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    lines = [l for l in out.splitlines() if l.strip()]
    for l in lines[-tail:]:
        print("  " + l)
    ok = r.returncode == 0
    print(f"  --> {'✅ 通过' if ok else f'❌ 失败 (exit {r.returncode})'}")
    return ok


def check_send_pairing(piece_name: str = "piece") -> bool:
    """对每一个可能的片段组合，检查 note-on / note-off 是否完全配对。

    这是最关键的一项：实时发送是"发一条算一条"，如果某个 note-on 被
    发出去、对应的 note-off 却被切掉了，硬件音源上就是一个永远响着的
    挂音。这里直接调曲子的 slice_events —— 和播放器用的是同一份逻辑。
    """
    print(f"\n{'=' * 72}\n== 3. 发送时序：note-on / note-off 配对（{piece_name}）\n{'=' * 72}")
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import importlib
    P = importlib.import_module(f"ps5.{piece_name}")   # noqa: N806

    names = [s[0] for s in P.SECTIONS]
    all_ok = True
    for i0 in range(len(names)):
        for i1 in range(i0, len(names)):
            kept, _off = P.slice_events(i0, i1)
            pending: dict[tuple[int, int], int] = {}
            for _t, msg, _l in kept:
                if len(msg) == 3 and (msg[0] & 0xF0) == 0x90:
                    k = (msg[0] & 0x0F, msg[1])
                    pending[k] = pending.get(k, 0) + (1 if msg[2] > 0 else -1)
            stuck = {k: v for k, v in pending.items() if v > 0}
            label = f"{names[i0]}→{names[i1]}"
            if stuck:
                print(f"  ❌ {label}: {len(stuck)} 个音没关 → {sorted(stuck)[:5]}")
                all_ok = False
            else:
                print(f"  ✅ {label}: {len(kept):>5} 条消息，"
                      f"时长 {kept[-1][0]:.1f}s，全部配对")
    print()
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser(description="PS5 工具链验证")
    ap.add_argument("--piece", default="piece",
                    help="piece / jrock / serene / solister / jpop / all")
    args = ap.parse_args()

    names = (["piece", "jrock", "serene", "solister", "jpop"]
             if args.piece == "all" else [args.piece])
    print(f"PS5 工具链验证 — {'、'.join(names)}")
    results = []

    for n in names:
        results.append(run(f"1. 乐曲结构自检（{n}）",
                           ["tools/ps5_check_piece.py", "--piece", n], tail=6))
    for n in names:
        results.append(run(f"2. 生成并解析 SMF（{n}）",
                           ["tools/ps5_make_smf.py", "--piece", n, "--verify"],
                           tail=4))
    for n in names:
        results.append(check_send_pairing(n))
    for n in names:
        results.append(run(f"4. 钢琴卷帘图（{n}）",
                           ["tools/ps5_pianoroll.py", "--piece", n], tail=2))

    print(f"\n{'=' * 72}")
    if all(results):
        print("== 全部通过 ✅")
        print("==")
        print("== 下一步：")
        print("==   python tools/ps5_play.py --piece serene        # 悠扬 4 分 51 秒")
        print("==   python tools/ps5_play.py --piece jrock         # 日摇 2 分 27 秒")
        print("==   python tools/ps5_play.py                       # 流行曲 3 分 20 秒")
        print("==   python tools/ps5_play.py                        # 流行曲 3 分 20 秒")
        return 0
    print(f"== {results.count(False)} 项失败 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
