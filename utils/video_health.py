"""Qt-free video health analysis, using an explicit per-run ffprobe when supplied."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def _creationflags() -> int:
    """Windows 下隐藏控制台窗口。"""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# JSON 输出解析失败(如文件名含特殊字符被写进元数据)时的兜底探测：
# 用 csv 文本格式重新读编解码器，返回 (video_codec, audio_codec)。
_CSV_CODEC_REGEX = re.compile(r"^([a-zA-Z0-9_]+),\s*(video|audio)", re.MULTILINE)


def _probe_codecs_csv(ffprobe: str, file_path: str) -> tuple[str, str]:
    """用 csv 文本格式探测视频/音频编解码器，返回 (video_codec, audio_codec)。

    仅在 ffprobe 的 json 输出无法解析时调用（如文件名含反斜杠/乱码导致 JSON 非法）。
    任何异常都返回 ("", "")，绝不抛出。
    """
    if not ffprobe:
        return "", ""
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "csv=p=0",
                "-show_entries",
                "stream=codec_name,codec_type",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
            timeout=120,
        )
    except Exception:  # noqa: BLE001
        return "", ""
    vc = ac = ""
    if r.stdout:
        for codec, ctype in _CSV_CODEC_REGEX.findall(r.stdout):
            if ctype == "video" and not vc:
                vc = codec.lower()
            elif ctype == "audio" and not ac:
                ac = codec.lower()
    return vc, ac


# Jellyfin 认为"兼容"的编解码器
_COMPAT_VIDEO = ("h264", "hevc", "h265")
_COMPAT_AUDIO = ("aac", "ac3", "mp3", "eac3", "flac", "opus")


def analyze_video(file_path: str | Path, ffprobe: Optional[str] = None) -> dict[str, Any]:
    """分析单个视频文件的损坏状态与 Jellyfin 兼容性（纯函数，可同步调用）。

    Args:
        file_path: 视频文件路径

    Returns:
        dict: 包含 is_corrupted / is_compatible / video_codec / audio_codec /
              duration / width / height / file_size / error 等字段。
              任何异常都不会抛出，而是以 is_corrupted=True 返回。
    """
    fp = Path(file_path)
    if ffprobe is None:
        from utils.tools import resolve_tool

        ffprobe = resolve_tool("ffprobe")
    info: dict[str, Any] = {
        "is_corrupted": False,
        "is_compatible": True,
        "duration": 0.0,
        "video_codec": "",
        "audio_codec": "",
        "width": 0,
        "height": 0,
        "file_size": 0,
        "error": "",
    }

    if not ffprobe:
        info["is_corrupted"] = True
        info["error"] = "未找到 ffprobe，请安装 FFmpeg 并加入 PATH"
        return info

    try:
        info["file_size"] = fp.stat().st_size

        # 第一步：快速损坏检测（只读取 duration）
        check_cmd = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(fp),
        ]
        # encoding=utf-8 + errors=replace：无论 ffprobe 输出何种字节(含乱码文件名/非UTF-8流标记)，
        # stdout/stderr 永远是 str，避免 Windows(GBK) 下解码失败导致 stdout=None 被误判为损坏。
        r = subprocess.run(
            check_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
            timeout=60,
        )
        if r.returncode != 0:
            info["is_corrupted"] = True
            info["error"] = (r.stderr or "").strip()[:200]
            return info

        # 第二步：详细流分析
        cmd = [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(fp),
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
            timeout=120,
        )
        if r.returncode != 0:
            info["is_corrupted"] = True
            info["error"] = (r.stderr or "").strip()[:200]
            return info

        # stdout 为空(极端情况，如权限/管道问题) → 当"无法解析"处理，不误判为损坏
        if not r.stdout or not r.stdout.strip():
            info["is_corrupted"] = True
            info["error"] = "ffprobe 无输出（文件可能被占用或读取失败）"
            return info

        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            # 输出非合法 JSON（多见于含特殊字符与乱码的文件名被 ffprobe 写进元数据）
            # → 容错而非误判损坏：改用 csv 文本格式重新探测编解码器，拿到准确 codec
            info["error"] = f"元数据解析异常: {e}"
            vc, ac = _probe_codecs_csv(ffprobe, str(fp))
            info["video_codec"] = vc
            info["audio_codec"] = ac
            info["is_corrupted"] = False
            info["is_compatible"] = fp.suffix.lower() == ".mp4" and (vc in ("", *_COMPAT_VIDEO))
            return info
        for stream in data.get("streams", []):
            ct = stream.get("codec_type", "")
            cn = stream.get("codec_name", "").lower()
            if ct == "video":
                info["video_codec"] = cn
                info["width"] = stream.get("width", 0)
                info["height"] = stream.get("height", 0)
            elif ct == "audio":
                info["audio_codec"] = cn

        fmt = data.get("format", {})
        try:
            info["duration"] = float(fmt.get("duration", 0) or 0)
        except (TypeError, ValueError):
            info["duration"] = 0.0

        # Jellyfin 兼容性判断：必须是 MP4 容器 + 兼容编解码器
        vc = info["video_codec"]
        ac = info["audio_codec"]
        video_ok = (not vc) or vc in _COMPAT_VIDEO
        audio_ok = (not ac) or ac in _COMPAT_AUDIO
        info["is_compatible"] = fp.suffix.lower() == ".mp4" and video_ok and audio_ok

    except subprocess.TimeoutExpired:
        info["is_corrupted"] = True
        info["error"] = "分析超时（文件可能损坏或过大）"
    except Exception as e:  # noqa: BLE001 — 分析失败不应中断流程
        info["is_corrupted"] = True
        info["error"] = str(e)[:200]

    return info
