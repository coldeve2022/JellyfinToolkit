"""GPU 显存查询与阈值推荐。

为什么需要"推荐"而不是写死默认值
--------------------------------
显存门控的阈值（占用超过多少就暂缓新任务）**完全取决于显卡容量**：
同一个 10.5 GB 在 12 GB 卡上是"快满了"，在 24 GB 卡上却只是"刚开始用"。
把它写死等于只对某一张卡有效。

所以这里只做两件事：

1. 读本机显存（走 ``nvidia-smi`` —— 装了 NVIDIA 驱动就有，不额外增加依赖）；
2. 按容量**推算**一组合适的阈值，并说明推算依据，让用户自己决定用不用。

原实现的 10.5 / 8.5（12 GB 卡）换算成比例是 **87% / 71%**，本模块沿用这两个比例。
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

__all__ = ["VRAM_HIGH_RATIO", "VRAM_LOW_RATIO", "vram_query",
           "vram_used_gb", "vram_total_gb", "recommend_vram_thresholds",
           "describe_thresholds"]

#: 高水位比例：占用超过它就不再启动新任务
VRAM_HIGH_RATIO = 0.87

#: 低水位比例：占用低于它才恢复启动
VRAM_LOW_RATIO = 0.71

_NO_WINDOW = 0x08000000


def vram_query(timeout: int = 20) -> Optional[list]:
    """查所有 GPU 的 ``(总显存GB, 已用显存GB)``，读不到返回 ``None``。

    用 ``nvidia-smi`` 而不是 ``pynvml``/``torch``：只要装了 NVIDIA 驱动就有它，
    不给用户增加依赖。读不到（非 N 卡 / 无驱动 / 命令被拦）时返回 ``None``，
    调用方**应当放行**而不是把队列卡死。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=(_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None

    out = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            total, used = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        out.append((total / 1024.0, used / 1024.0))
    return out or None


def vram_used_gb() -> float:
    """当前第一块 GPU 的已用显存（GB）；读不到返回 ``-1``。"""
    data = vram_query()
    return data[0][1] if data else -1.0


def vram_total_gb() -> float:
    """当前第一块 GPU 的总显存（GB）；读不到返回 ``-1``。"""
    data = vram_query()
    return data[0][0] if data else -1.0


def recommend_vram_thresholds(total_gb: float) -> tuple:
    """按显存容量推算 ``(高水位, 低水位)``（GB），保留一位小数。

    比例沿用"12 GB 卡上用 10.5 / 8.5"这组实测值。同时保证：

    - 低水位至少比高水位低 1 GB（否则会疯狂抖动：刚暂停又立刻恢复）；
    - 高水位至少留 1 GB 余量（全占满时新任务必然 OOM）。
    """
    try:
        total = float(total_gb)
    except (TypeError, ValueError):
        return (0.0, 0.0)
    if total <= 0:
        return (0.0, 0.0)

    high = round(total * VRAM_HIGH_RATIO, 1)
    low = round(total * VRAM_LOW_RATIO, 1)
    high = min(high, max(total - 1.0, 0.5))
    low = min(low, max(high - 1.0, 0.1))
    return (high, low)


def describe_thresholds(high: float, low: float, total: float = -1.0) -> str:
    """给用户看的一句话说明（含比例），便于他判断要不要改。"""
    if high <= 0 or low <= 0:
        return "未设置阈值"
    base = f"占用超过 {high:.1f} GB 时暂缓新任务，低于 {low:.1f} GB 时继续"
    if total and total > 0:
        base += (f"（按你显卡的 {total:.0f} GB 推算："
                 f"{high / total * 100:.0f}% / {low / total * 100:.0f}%）")
    return base
