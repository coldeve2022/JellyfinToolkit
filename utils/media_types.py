"""媒体类型判定 —— **扩展名与"什么是视频/字幕/封面"的唯一事实来源**。

为什么要有这个模块
------------------
之前"哪些扩展名算视频/字幕/封面"在仓库里有 **5 份各自的定义**：
`config.py`（用户可配）、`utils/merge.py:VIDEO_EXTENSIONS`、
`utils/merge_archive.py:SIDECAR_EXTENSIONS / SUBTITLE_EXTENSIONS`、
`utils/library.py` 的封面扩展名、`utils/whisper_tool.py` 的音频扩展名（已删）。

后果是：用户在设置里加了 `.webm`，**只有走配置的那一半功能跟着变**，
另一半仍然不认 —— 表现为"有的页面认、有的页面不认"。
而且每份清单还会各自漂移，出问题只能一处一处修。

现在统一到这里：

- **默认值**就在这里，但判定函数**优先接受调用方传入的配置**；
- 其它模块不再自己存常量，改为从本模块引用（保留同名别名以便兼容）。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "DEFAULT_VIDEO_EXTENSIONS", "DEFAULT_SUBTITLE_EXTENSIONS",
    "DEFAULT_SIDECAR_EXTENSIONS", "COVER_EXTENSIONS",
    "normalize_exts", "is_video", "is_subtitle", "is_cover", "is_sidecar",
    "split_path_name",
]

#: 视频扩展名默认值。**事实来源是配置** `video_extensions`，这里只是兜底默认值。
DEFAULT_VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".ts", ".rmvb",
    ".webm", ".mpg", ".mpeg", ".m2ts", ".iso",
)

#: 字幕扩展名默认值（配置 `subtitle_extensions` 为事实来源）
DEFAULT_SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".sub", ".vtt", ".lrc")

#: 与视频同目录的"附属文件"（NFO/字幕/封面…）—— 合并文件时跟着一起走
DEFAULT_SIDECAR_EXTENSIONS = (
    ".nfo", ".srt", ".ass", ".ssa", ".sub", ".vtt", ".jpg", ".jpeg",
    ".png", ".webp", ".json",
)

#: 封面/图片
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def normalize_exts(values) -> set:
    """把 ``(".mp4", "mkv")`` 这类写法统一成 ``{".mp4", ".mkv"}``。

    用户在设置里可能写带点或不带点、大小写不一，这里全部吸收。
    """
    out = set()
    for v in values or ():
        s = str(v).strip().lower()
        if not s:
            continue
        out.add(s if s.startswith(".") else "." + s)
    return out


def _ext_of(path) -> str:
    return Path(str(path)).suffix.lower()


def is_video(path, video_exts=None) -> bool:
    """是否视频文件。``video_exts`` 传入时以它为准（通常来自配置）。"""
    exts = normalize_exts(video_exts) if video_exts is not None \
        else set(DEFAULT_VIDEO_EXTENSIONS)
    return _ext_of(path) in exts


def is_subtitle(path, subtitle_exts=None) -> bool:
    """是否字幕文件。"""
    exts = normalize_exts(subtitle_exts) if subtitle_exts is not None \
        else set(DEFAULT_SUBTITLE_EXTENSIONS)
    return _ext_of(path) in exts


def is_cover(path) -> bool:
    """是否封面/图片。"""
    return _ext_of(path) in set(COVER_EXTENSIONS)


def is_sidecar(path, sidecar_exts=None) -> bool:
    """是否附属文件（NFO / 字幕 / 封面等）。"""
    exts = normalize_exts(sidecar_exts) if sidecar_exts is not None \
        else set(DEFAULT_SIDECAR_EXTENSIONS)
    return _ext_of(path) in exts


def split_path_name(p: str) -> str:
    """取路径末端的文件名 —— **同时认 ``/`` 和 ``\\``**。

    Jellyfin / Emby 库里存的路径可能是另一个平台写下的（库在 Windows 上刮削、
    之后服务迁到 Linux，库里存的仍是反斜杠分隔的路径），而 POSIX 的
    ``os.path.basename`` 不把反斜杠当分隔符，会把整串当成文件名 ——
    于是附属文件判定、封面查找全部失效。这是实测出来的，不是理论问题。

    **全仓库统一用这个**，不要再用 ``os.path.basename`` 处理"库里存的路径"。
    """
    return str(p).replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1]
