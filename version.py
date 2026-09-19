"""版本与项目元数据的唯一来源。

窗口标题、侧边栏版本号、exe 版本资源、构建脚本、README 全部引用这里，
避免"界面写 3.5、文件属性写 3.4"这类漂移。
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "VERSION_TUPLE",
    "APP_NAME",
    "APP_NAME_CN",
    "APP_ID",
    "APP_DESCRIPTION",
    "__author__",
    "__license__",
    "PROJECT_URL",
    "MIN_PYTHON",
    "REQUIRED_PYTHON_NOTE",
    "SUPPORTED_SERVERS",
    "window_title",
]

__version__ = "3.7.0"
VERSION_TUPLE = (3, 7, 0, 0)

APP_NAME = "JellyfinToolkit"
APP_NAME_CN = "Jellyfin Toolkit"
APP_ID = "JellyfinToolkit"          # %APPDATA% 下的目录名 / 便携模式标记用
APP_DESCRIPTION = ("Jellyfin / Emby 影视库管理工具箱 — "
                   "库概览 / 喜好画像 / 打标 / 去重 / 字幕 / 修复 / 分集合并")
__author__ = "coldeve2022"
__license__ = "MIT"
PROJECT_URL = "https://github.com/coldeve2022/JellyfinToolkit"

# 明确声明支持的媒体服务器；UI 文案与 README 都从这里取值，避免各处写歪
SUPPORTED_SERVERS = ("Jellyfin", "Emby")

MIN_PYTHON = (3, 9)
REQUIRED_PYTHON_NOTE = "需要 Python 3.9+（推荐 3.11/3.12/3.13）"


def window_title() -> str:
    """主窗口标题（含版本号，便于确认跑的是哪一版）。"""
    return f"{APP_NAME_CN} — 影视库管理工具箱 v{__version__}"
