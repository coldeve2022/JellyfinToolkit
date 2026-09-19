"""界面字体探测。

写死 ``QFont("Microsoft YaHei")`` 在没有该字体的机器（Linux / 精简版 Windows /
英文版 Windows）上，Qt 会**静默回退**，中文可能渲染成方块，且没有任何报错。
所以这里按平台给候选列表，并用 ``QFontDatabase`` **校验字体真实存在**。
"""

from __future__ import annotations

import sys

# 候选顺序：优先平台原生中文字体，最后兜底 Qt 默认
_CANDIDATES = {
    "win32": (
        "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
        "Segoe UI", "SimHei", "SimSun", "Noto Sans CJK SC", "sans-serif",
    ),
    "darwin": (
        "PingFang SC", "Hiragino Sans GB", "Heiti SC",
        "Songti SC", "Noto Sans CJK SC", "sans-serif",
    ),
    "linux": (
        "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
        "WenQuanYi Zen Hei", "Microsoft YaHei", "DejaVu Sans", "sans-serif",
    ),
}

_MONO_CANDIDATES = {
    "win32": ("Cascadia Mono", "Consolas", "Courier New", "monospace"),
    "darwin": ("SF Mono", "Menlo", "Monaco", "Courier New", "monospace"),
    "linux": ("JetBrains Mono", "DejaVu Sans Mono", "Noto Sans Mono",
              "Liberation Mono", "monospace"),
}

_cache: dict[str, str] = {}


def _family_set() -> set[str]:
    """当前 Qt 已注册的字体族名集合；Qt 未就绪时返回空集合。

    注意：``QFontDatabase.families()`` 必须在 QApplication/QGuiApplication 之后
    才能调用，否则 Qt 会直接报 "Must construct a QGuiApplication before..."。
    """
    try:
        from PySide6.QtGui import QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return set()
    if QApplication.instance() is None:
        return set()
    try:
        return set(QFontDatabase.families())
    except BaseException:  # noqa: BLE001 — 任何 Qt 异常都退化为"没有字体信息"
        return set()


def _pick(candidates: tuple[str, ...], cache_key: str) -> str:
    if cache_key in _cache:
        return _cache[cache_key]
    available = _family_set()
    chosen = ""
    for name in candidates:
        if name in available:
            chosen = name
            break
    if not chosen:
        # 没有 Qt 或一个候选都没有 → 交给 Qt 自己决定
        chosen = candidates[-1] if candidates else ""
    _cache[cache_key] = chosen
    return chosen


def ui_family() -> str:
    """界面正文字体族名。"""
    return _pick(_CANDIDATES.get(sys.platform, _CANDIDATES["linux"]), "ui")


def mono_family() -> str:
    """等宽字体族名（日志/代码展示用）。"""
    return _pick(_MONO_CANDIDATES.get(sys.platform, _MONO_CANDIDATES["linux"]), "mono")


def ui_font(point_size: int = 10, bold: bool = False):
    """返回一个适合当前系统的 QFont；无 Qt 时返回 None。"""
    try:
        from PySide6.QtGui import QFont
    except ImportError:
        return None
    font = QFont(ui_family(), point_size)
    if bold:
        font.setBold(True)
    return font
