"""主题系统 — 颜色 token 定义与当前主题状态。

所有控件颜色一律通过 token 获取，禁止在页面代码中硬编码颜色值。
切换主题 = 切换 token 集 + 重新应用全局 QSS。
"""

from typing import Any

DARK: dict[str, Any] = {
    "name": "深色",
    # 主色
    "accent": "#4FC3F7",
    "accent_hover": "#29B6F6",
    "accent_dark": "#0288D1",
    "success": "#66BB6A",
    "warning": "#FFA726",
    "danger": "#EF5350",
    # 背景层级
    "bg_darkest": "#0D1117",   # 侧边栏
    "bg_dark": "#161B22",      # 主背景
    "bg_card": "#21262D",      # 卡片/面板
    "bg_hover": "#30363D",     # 悬停
    "bg_input": "#0D1117",     # 输入框
    "border": "#30363D",       # 边框
    # 文字
    "text_primary": "#E6EDF3",
    "text_secondary": "#8B949E",
    "text_muted": "#484F58",
    # 组件专属
    "title_color": "#4FC3F7",
    "logo_color": "#4FC3F7",
    "selection_text": "#0D1117",   # 强调按钮上的文字色
}

LIGHT: dict[str, Any] = {
    "name": "浅色",
    # 主色（GitHub Light 风格）
    "accent": "#0969DA",
    "accent_hover": "#0A56B0",
    "accent_dark": "#0550AE",
    "success": "#1A7F37",
    "warning": "#9A6700",
    "danger": "#CF222E",
    # 背景层级
    "bg_darkest": "#F6F8FA",   # 侧边栏
    "bg_dark": "#FFFFFF",      # 主背景
    "bg_card": "#F6F8FA",      # 卡片/面板
    "bg_hover": "#E8ECEF",     # 悬停
    "bg_input": "#FFFFFF",     # 输入框
    "border": "#D0D7DE",       # 边框
    # 文字
    "text_primary": "#1F2328",
    "text_secondary": "#59636E",
    "text_muted": "#8C959F",
    # 组件专属
    "title_color": "#0969DA",
    "logo_color": "#0969DA",
    "selection_text": "#FFFFFF",   # 强调按钮上的文字色
}

THEMES: dict[str, dict] = {"dark": DARK, "light": LIGHT}

# 当前主题（模块级状态）
_current = "dark"


def set_current(name: str) -> None:
    """设置当前主题名，非法值回退 dark。"""
    global _current
    _current = name if name in THEMES else "dark"


def get_theme_name() -> str:
    return _current


def theme(name: str | None = None) -> dict:
    """返回主题 token 字典。

    Args:
        name: 主题名；None 时返回当前主题

    Returns:
        主题 token 字典
    """
    if name:
        return THEMES.get(name, DARK)
    return THEMES[_current]


def color(key: str) -> str:
    """获取当前主题的指定 token 颜色值。"""
    return THEMES[_current][key]


def theme_names() -> list[str]:
    """可用主题名列表。"""
    return list(THEMES.keys())
