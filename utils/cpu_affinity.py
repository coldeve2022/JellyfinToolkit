"""CPU 亲和性：找出 P 核（性能核）逻辑线程，供长任务绑定使用。

为什么需要
----------
Windows 的调度器会把后台进程往 **E 核（能效核）** 上甩以省电。破解这类任务要跑
几十分钟，绑不绑核的差距很明显。但"哪些核是 P 核"在不同 CPU 上完全不同，
**不能写死**：

- Intel 12 代及以后的混合架构（如 13600K：6P+8E）：P 核支持超线程、E 核不支持，
  所以"有 2 个逻辑线程的物理核"就是 P 核 —— 这是 :func:`detect_p_core_threads`
  的首选判据，走 Windows API 拿到的拓扑最准；
- 纯大核 CPU（没有 E 核）/ AMD / 老 Intel：没有混合架构，绑核没有意义，
  此时返回全部核心即可；
- 非 Windows 或 API 调用失败：退回 :func:`psutil` 的启发式，再退回"全部核心"。

因此本模块**不只返回结果，还返回来源**（``source``），界面据此如实告诉用户
"这是怎么判出来的" —— 判错了用户才能自己改（见 :func:`parse_core_spec` 允许手填）。
"""

from __future__ import annotations

import re
import sys
from typing import Optional

__all__ = [
    "detect_p_core_threads", "detect_affinity", "parse_core_spec",
    "format_core_spec", "AFFINITY_MODES", "describe_affinity",
]

#: 绑核模式（配置项 ``lada_cpu_affinity`` 的取值）
AFFINITY_MODES = ("auto", "off", "custom")

#: ``auto`` 模式判定时的来源说明
_SRC_API = "Windows API 拓扑"
_SRC_HEURISTIC = "psutil 启发式"
_SRC_ALL = "无法判断，用全部核心"


def _all_logical_threads() -> list:
    try:
        import psutil

        n = psutil.cpu_count(logical=True) or 0
    except Exception:  # noqa: BLE001
        n = 0
    if not n:
        import os

        n = os.cpu_count() or 1
    return list(range(n))


def _detect_via_windows_api() -> Optional[list]:
    """用 ``GetLogicalProcessorInformationEx`` 读拓扑，返回 P 核逻辑线程号。

    判据：**一个物理核带 2 个逻辑线程 = 支持超线程 = P 核**（Intel 混合架构上
    E 核不支持超线程）。拿不到就返回 ``None``，交给上层回退。

    移植自自研 lada_gui 里的实现 —— 那段是实测可用的，不要凭印象重写。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt
        from ctypes import POINTER, Structure, byref

        class SLPIE(Structure):
            _fields_ = [("Relationship", wt.ULONG), ("Size", wt.ULONG),
                        ("data", ctypes.c_byte * 1)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.GetLogicalProcessorInformationEx
        fn.argtypes = [wt.DWORD, POINTER(SLPIE), POINTER(wt.DWORD)]
        fn.restype = wt.BOOL

        length = wt.DWORD(0)
        fn(0, None, byref(length))       # 第一次调用只为取所需缓冲区大小
        if not length.value:
            return None
        buf = ctypes.create_string_buffer(length.value)
        if not fn(0, ctypes.cast(buf, POINTER(SLPIE)), byref(length)):
            return None

        p_threads: list = []
        offset = 0
        while offset < length.value:
            rel = ctypes.cast(ctypes.byref(buf, offset), POINTER(SLPIE)).contents
            size = rel.Size
            if not size:
                break
            if rel.Relationship == 0:        # RelationProcessorCore
                data_off = offset + 8
                mask = int.from_bytes(buf[data_off + 24:data_off + 32], "little")
                lps = [i for i in range(64) if mask & (1 << i)]
                if len(lps) == 2:            # 有超线程 → P 核
                    p_threads.extend(lps)
            offset += size
        return sorted(p_threads) or None
    except Exception:  # noqa: BLE001  探测失败不该影响主流程
        return None


def _detect_via_heuristic() -> Optional[list]:
    """回退判据：逻辑核数 = 物理核数 × 2 时，认为前一半是带超线程的 P 核。

    这只在"混合架构且 P 核都支持超线程"时成立。判错的风险由界面暴露出来
    （会显示来源），用户可以改成自定义核心列表。
    """
    try:
        import psutil

        logical = psutil.cpu_count(logical=True) or 0
        physical = psutil.cpu_count(logical=False) or 0
    except Exception:  # noqa: BLE001
        return None
    if not logical:
        return None
    if physical and logical == physical * 2:
        return list(range(logical))
    return None


def detect_p_core_threads() -> list:
    """P 核逻辑线程号列表（判定不出来就是全部核心）。"""
    return detect_affinity("auto")["cores"]


def detect_affinity(mode: str = "auto", custom: str = "") -> dict:
    """按模式给出绑核用的核心列表。

    ``mode``
        - ``auto``：自动探测 P 核（API → 启发式 → 全部核心）
        - ``off``：不绑核（交回系统调度）
        - ``custom``：用 ``custom`` 里的写法，如 ``"0-7,12-15"``

    返回 ``{"mode", "cores", "source", "reason"}``；``reason`` 是给人看的一句话，
    **界面要把它显示出来** —— 用户才知道程序到底做了什么判断，也才有依据去改。
    """
    mode = (mode or "auto").strip().lower()
    if mode not in AFFINITY_MODES:
        mode = "auto"

    if mode == "off":
        return {"mode": "off", "cores": [], "source": "已关闭",
                "reason": "没有绑定核心，由系统调度器自行安排。"}

    if mode == "custom":
        cores = parse_core_spec(custom)
        if cores:
            return {"mode": "custom", "cores": cores, "source": "自定义",
                    "reason": f"按你的设置绑定到 {format_core_spec(cores)}。"}
        mode = "auto"      # 自定义写错了就回退，别让任务跑不起来

    cores = _detect_via_windows_api()
    if cores:
        return {"mode": "auto", "cores": cores, "source": _SRC_API,
                "reason": f"从系统拓扑读出 P 核线程 {format_core_spec(cores)}"
                          f"（带超线程的物理核视为 P 核）。"}
    cores = _detect_via_heuristic()
    if cores:
        return {"mode": "auto", "cores": cores, "source": _SRC_HEURISTIC,
                "reason": f"用逻辑核数与物理核数推算 P 核线程 "
                          f"{format_core_spec(cores)}；如果不对，请在设置里改为自定义。"}
    cores = _all_logical_threads()
    return {"mode": "auto", "cores": cores, "source": _SRC_ALL,
            "reason": f"无法判断 P 核（可能是全大核或非 Intel 混合架构），"
                      f"改用全部 {len(cores)} 个逻辑核心。"}


# ── 自定义核心列表的写法 ────────────────────────────────────

_SPEC_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def parse_core_spec(spec: str) -> list:
    """解析 ``"0-7,12-15"`` / ``"0,2,4"`` / ``"0-3"`` 这类写法为线程号列表。

    非法输入返回空列表（调用方据此回退），解析时忽略超出范围与重复项。
    """
    out: list = []
    limit = len(_all_logical_threads())
    for chunk in str(spec or "").replace("，", ",").split(","):
        if not chunk.strip():
            continue
        m = _SPEC_RE.match(chunk)
        if not m:
            continue
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        if end < start:
            start, end = end, start
        for i in range(start, min(end, limit - 1 if limit else end) + 1):
            if i not in out:
                out.append(i)
    return sorted(out)


def format_core_spec(cores) -> str:
    """把线程号列表压成 ``"0-7,12-15"`` 这种好读的写法（用于回填到输入框）。"""
    if not cores:
        return ""
    cores = sorted(set(int(c) for c in cores))
    groups = []
    start = prev = cores[0]
    for c in cores[1:]:
        if c == prev + 1:
            prev = c
            continue
        groups.append((start, prev))
        start = prev = c
    groups.append((start, prev))
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in groups)


def describe_affinity(info: dict) -> str:
    """一句话描述绑核结果（供界面直接显示）。"""
    if not info:
        return "未检测"
    if info.get("mode") == "off" or not info.get("cores"):
        return f"不绑核 —— {info.get('reason', '')}".strip(" ——")
    return (f"{format_core_spec(info['cores'])}"
            f"（{len(info['cores'])} 个线程，来源：{info.get('source', '未知')}）")
