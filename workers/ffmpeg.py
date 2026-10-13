"""FFmpeg 操作层 — 视频分析、修复、转码。

设计原则：
- `analyze_video()` 等核心逻辑为**纯函数**（不依赖 Qt），可单元测试；
- Worker 线程仅负责包装纯函数 + 信号发射；
- 修复/转码操作保证数据安全：先产出新文件，确认成功后才替换原文件。
"""

from __future__ import annotations

import json  # noqa: F401 - legacy callers patch this shared module object
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal


def _creationflags() -> int:
    """Windows 下隐藏控制台窗口。"""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _probe_codecs_csv(ffprobe: str, file_path: str) -> tuple[str, str]:
    """Compatibility adapter for the shared Qt-free probe."""
    from utils.video_health import _probe_codecs_csv as probe

    return probe(ffprobe, file_path)


def find_binary(name: str, extra_dirs=()) -> Optional[str]:
    """在程序目录/内置子目录/PATH/各平台常见位置中查找可执行文件。

    实现在 :mod:`utils.tools`（多路兜底 + 用户配置优先），这里保留同名入口兼容旧调用。
    """
    from utils import tools

    return tools.find_binary(name, extra_dirs)


def _ffmpeg_path() -> Optional[str]:
    """当前生效的 ffmpeg 路径（每次调用重新解析，用户改配置后立即生效）。"""
    from utils import tools

    return tools.resolve_tool("ffmpeg")


def _ffprobe_path() -> Optional[str]:
    """当前生效的 ffprobe 路径。"""
    from utils import tools

    return tools.resolve_tool("ffprobe")


def __getattr__(name: str):
    """让 ``_FFMPEG`` / ``_FFPROBE`` 变成**动态**属性。

    旧实现是模块级 ``_FFMPEG = find_binary("ffmpeg")``（import 时固化），
    用户在设置里填了 ffmpeg 路径、或稍后安装 ffmpeg，都不会生效。
    """
    if name == "_FFMPEG":
        return _ffmpeg_path()
    if name == "_FFPROBE":
        return _ffprobe_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def analyze_video(file_path: str | Path, ffprobe: Optional[str] = None) -> dict[str, Any]:
    """Compatibility worker entry; shared analysis itself does not import Qt."""
    from utils.video_health import analyze_video as analyze

    return analyze(file_path, ffprobe=ffprobe if ffprobe is not None else _ffprobe_path())


def check_encoder_available(codec: str, ffmpeg: Optional[str] = None) -> bool:
    """检查指定编码器在**本机**是否真的能编出帧。

    注意：不能用 ``ffmpeg -encoders`` 判断。那条命令列的是这份构建**编译时带了什么**，
    官方构建会同时列出 h264_nvenc/h264_qsv/h264_amf —— 与机器上有没有对应显卡无关。
    在没有 Intel 核显的机器上选 qsv，执行时才报 MFX session 错误。

    Args:
        codec: 编码器名称，如 h264_nvenc / libx264
        ffmpeg: 可选的 ffmpeg 路径；缺省用当前解析到的路径

    Returns:
        实测可用返回 True；ffmpeg 缺失或实测失败返回 False
    """
    from utils import tools

    tool = ffmpeg or _ffmpeg_path()
    if not tool:
        return False
    return tools.probe_encoder(tool, codec)


def check_hwaccel_available(accel: str, ffmpeg: Optional[str] = None) -> bool:
    """检查指定硬解方式在**本机**是否真的能建立解码设备。

    同样不能用 ``ffmpeg -hwaccels``：它只反映编译期支持列表。
    """
    from utils import tools

    if accel in ("none", ""):
        return True  # "不要硬解" 永远成立
    tool = ffmpeg or _ffmpeg_path()
    if not tool:
        return False
    return tools.probe_hwaccel(tool, accel)


# ───────────────────────── 分析 Worker ─────────────────────────


class FFprobeAnalyzer(QThread):
    """异步分析单个视频文件（包装 analyze_video 纯函数）。"""

    result = Signal(str, dict)  # 文件路径, 分析结果
    finished = Signal()

    def __init__(self, file_path: str | Path, parent=None):
        super().__init__(parent)
        self.file_path = str(file_path)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if self._stopped:
            self.finished.emit()
            return
        info = analyze_video(self.file_path)
        self.result.emit(self.file_path, info)


# ───────────────────────── 修复/转码 Worker ─────────────────────────


class FFmpegRepairWorker(QThread):
    """批量视频修复/转码 Worker。

    task_type:
        - "auto":      逐文件分析后自动决策（损坏→修复，不兼容→转码，兼容→跳过）
        - "repair":    全部强制修复坏帧
        - "transcode": 全部强制转码为 MP4
    """

    log = Signal(str)
    progress = Signal(int)  # 进度百分比
    file_done = Signal(str, bool)  # 文件路径, 是否成功
    finished = Signal()

    def __init__(
        self,
        file_paths: list[str],
        task_type: str = "repair",
        gpu_codec: str = "h264_nvenc",
        hw_accel: str = "cuda",
        crf: int = 23,
        min_size_bytes: int = 0,
        parent=None,
        precomputed: Optional[dict] = None,
        timeout: int = 900,
        log_skips: bool = True,
    ):
        """批量视频修复/转码 Worker。

        precomputed: 可选，{文件路径: analyze_video() 结果 dict}。
        提供时 auto 模式直接复用扫描结果决策，不再重新 ffprobe 分析——
        解决"扫描一遍又修复时再扫一遍"的重复分析问题。

        timeout: 单文件 ffmpeg 处理超时（秒）。超过则终止该文件并保留原文件，
        避免损坏文件(如 contradiction STSC/STCO)让 ffmpeg 无限挂起卡死队列。

        log_skips: False 时，auto 模式下"兼容且正常→跳过"的文件**不逐条刷日志**。
        配合 precomputed（已扫描过）用，避免几千个正常文件刷屏、显得像又在扫描。
        """
        super().__init__(parent)
        self._files = file_paths
        self._task_type = task_type
        self._gpu_codec = gpu_codec
        self._hw_accel = hw_accel
        self._crf = crf
        self._min_size = min_size_bytes
        self._precomputed = {os.path.normpath(k): v for k, v in (precomputed or {}).items()}
        self._timeout = max(30, int(timeout or 900))
        self._log_skips = bool(log_skips)
        self._paused = False
        self._stopped = False
        self._current_process: Optional[subprocess.Popen] = None

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stopped = True
        self._paused = False
        if self._current_process:
            try:
                self._current_process.kill()
            except Exception:
                pass

    def _resolve_codec(self) -> None:
        """把配置里的编码器/硬解解析成本机**实测可用**的组合。

        支持 ``""``/``"auto"`` 表示自动挑最优；指定的编码器在本机不可用时
        回退 libx264 并明确提示，不让用户看到一句厂商 DLL 报错。
        """
        from utils import tools

        ffmpeg = _ffmpeg_path() or ""
        self._ffmpeg = ffmpeg
        codec, accel = tools.resolve_encode_settings(
            ffmpeg, self._gpu_codec, self._hw_accel, log=self.log.emit
        )
        self._gpu_codec = codec
        self._hw_accel = accel

    def _encoder_args(self) -> list[str]:
        """按编码器**厂商**生成参数（各家质量参数不通用，见 utils.tools.encoder_family）。"""
        from utils import tools

        return tools.encoder_args(self._gpu_codec, self._crf)

    def _ffmpeg_bin(self) -> Optional[str]:
        """本次运行使用的 ffmpeg 路径。"""
        return getattr(self, "_ffmpeg", None) or _ffmpeg_path()

    def run(self) -> None:
        if not _ffmpeg_path():
            self.log.emit(
                "❌ 未找到 ffmpeg，请安装 FFmpeg 并加入系统 PATH，"
                "或在「设置 → 外部工具」里指定 ffmpeg 可执行文件路径"
            )
            self.finished.emit()
            return

        self._resolve_codec()
        total = len(self._files)
        stats = {"repaired": 0, "transcoded": 0, "failed": 0, "skipped": 0}

        for idx, fp in enumerate(self._files):
            while self._paused and not self._stopped:
                self.msleep(200)
            if self._stopped:
                return

            path = Path(fp)

            # 小于阈值：跳过。已扫描(复用)模式下不逐条刷日志，避免刷屏
            if self._min_size > 0:
                try:
                    too_small = path.stat().st_size < self._min_size
                except OSError:
                    too_small = True
                if too_small:
                    if self._log_skips or not self._precomputed:
                        self.log.emit("  ⏭️ 文件过小，已跳过")
                    stats["skipped"] += 1
                    self.progress.emit(int((idx + 1) / total * 100))
                    continue

            # 已扫描 + 兼容正常 + 不刷日志 → 静默跳过（连"处理"行都不刷，避免像又在扫描）
            pre_info = self._precomputed.get(os.path.normpath(str(path)))
            if (
                self._task_type == "auto"
                and pre_info is not None
                and not pre_info.get("is_corrupted")
                and pre_info.get("is_compatible")
                and not self._log_skips
            ):
                stats["skipped"] += 1
                self.progress.emit(int((idx + 1) / total * 100))
                continue

            self.log.emit(f"[{idx + 1}/{total}] 处理: {path.name}")

            if self._task_type == "auto":
                self._auto_process(path, stats)
            elif self._task_type == "repair":
                self._do_repair(path, stats)
            elif self._task_type == "transcode":
                self._do_transcode(path, stats)

            self.progress.emit(int((idx + 1) / total * 100))

        self.log.emit("─" * 50)
        self.log.emit(
            f"处理完毕。修复: {stats['repaired']} | 转码: {stats['transcoded']} "
            f"| 跳过: {stats['skipped']} | 失败: {stats['failed']}"
        )
        self.finished.emit()

    def _auto_process(self, path: Path, stats: dict) -> None:
        """自动决策：损坏→修复；不兼容→转码；兼容→跳过。

        若扫描结果已缓存（precomputed），直接复用不再重新 ffprobe 分析。
        """
        info = self._precomputed.get(os.path.normpath(str(path)))
        if info is None:
            info = analyze_video(path)
        if info["is_corrupted"]:
            self.log.emit(f"  🔧 检测到损坏，尝试修复: {info.get('error') or '结构异常'}")
            self._do_repair(path, stats)
        elif not info["is_compatible"]:
            self.log.emit(
                f"  🔄 不兼容 Jellyfin (编码: {info['video_codec'] or '未知'})，转码中..."
            )
            self._do_transcode(path, stats)
        else:
            # 复用扫描结果 + 不刷跳过日志时，这里已经由 run() 顶部静默处理，只计数
            if self._log_skips or self._precomputed.get(os.path.normpath(str(path))) is None:
                self.log.emit("  ✅ 文件兼容且正常，跳过")
            stats["skipped"] += 1

    def _validate_output(self, source: Path, output: Path) -> tuple:
        from utils.merge import probe_part
        from utils.merge_repair import validate_video_stream

        probe = _ffprobe_path()
        if not probe:
            return False, "缺少 ffprobe，无法校验产物，已保留原文件"
        result = probe_part(probe, str(output))
        if not result.ok or not result.video_codec or result.duration <= 0:
            return False, "产物没有有效视频流/时长"
        original = probe_part(probe, str(source))
        if original.ok and original.duration > 0:
            if abs(result.duration - original.duration) > max(0.5, original.duration * 0.05):
                return False, "产物时长与原文件不符"
            for kind in ("audio", "subtitle"):
                before = sum(s.get("codec_type") == kind for s in original.streams)
                after = sum(s.get("codec_type") == kind for s in result.streams)
                if before != after:
                    return False, "产物丢失音轨/字幕轨"
        return validate_video_stream(self._ffmpeg_bin(), str(output), timeout=self._timeout)

    def _process_video(self, path: Path, target: Path, repair: bool, stats: dict) -> None:
        import tempfile
        from utils.proc import run_streaming
        from utils.file_ops import safe_replace

        if target != path and target.exists():
            stats["failed"] += 1
            self.log.emit("目标 MP4 已存在，拒绝覆盖，请先审阅版本")
            self.file_done.emit(str(path), False)
            return
        fd, temp_name = tempfile.mkstemp(
            prefix="temp_repair_" if repair else "temp_transcode_",
            suffix=target.suffix,
            dir=target.parent,
        )
        os.close(fd)
        temp = Path(temp_name)
        accel = ["-hwaccel", self._hw_accel] if self._hw_accel not in ("none", "") else []
        cmd = [self._ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
        if repair:
            cmd += ["-err_detect", "ignore_err"]
        cmd += [
            *accel,
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map_metadata",
            "0",
            *self._encoder_args(),
            "-c:a",
            "copy" if repair else "aac",
            "-c:s",
            "copy",
        ]
        if target.suffix.lower() == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(temp))
        try:
            result = run_streaming(
                cmd,
                timeout=self._timeout,
                should_stop=lambda: self._stopped,
                on_line=self.log.emit,
                on_started=lambda process: setattr(self, "_current_process", process),
            )
            if result["stopped"] or self._stopped:
                return
            if result["code"] != 0 or result["error"]:
                raise OSError(
                    result["error"] or "\n".join(result["lines"][-5:]) or "FFmpeg 返回非零退出码"
                )
            valid, why = self._validate_output(path, temp)
            if not valid:
                raise OSError(why)
            installed = safe_replace(
                temp,
                target,
                move_source=True,
                should_stop=lambda: self._stopped,
                allow_overwrite=target == path,
                kind="video_repair" if repair else "video_transcode",
            )
            if not installed["ok"]:
                if installed["state"] == "cancelled":
                    return
                raise OSError(installed["error"])
            # Keep old-format source until a separate reviewed quarantine operation.
            # Same-format originals are retained in the transaction backup.
            stats["repaired" if repair else "transcoded"] += 1
            self.file_done.emit(str(target), True)
            self.log.emit(f"完成（原件可恢复）：{target}")
        except Exception as exc:
            stats["failed"] += 1
            self.log.emit(f"失败，已保留原件：{exc}")
            self.file_done.emit(str(path), False)
        finally:
            self._current_process = None
            temp.unlink(missing_ok=True)

    def _do_repair(self, path: Path, stats: dict) -> None:
        self._process_video(path, path, True, stats)

    def _do_transcode(self, path: Path, stats: dict) -> None:
        self._process_video(path, path.with_suffix(".mp4"), False, stats)
