"""FFmpeg 操作层 — 视频分析、修复、转码。

设计原则：
- `analyze_video()` 等核心逻辑为**纯函数**（不依赖 Qt），可单元测试；
- Worker 线程仅负责包装纯函数 + 信号发射；
- 修复/转码操作保证数据安全：先产出新文件，确认成功后才替换原文件。
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal


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
            [ffprobe, "-v", "quiet", "-print_format", "csv=p=0",
             "-show_entries", "stream=codec_name,codec_type", file_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_creationflags(), timeout=120)
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


# Jellyfin 认为"兼容"的编解码器
_COMPAT_VIDEO = ("h264", "hevc", "h265")
_COMPAT_AUDIO = ("aac", "ac3", "mp3", "eac3", "flac", "opus")


def analyze_video(file_path: str | Path) -> dict[str, Any]:
    """分析单个视频文件的损坏状态与 Jellyfin 兼容性（纯函数，可同步调用）。

    Args:
        file_path: 视频文件路径

    Returns:
        dict: 包含 is_corrupted / is_compatible / video_codec / audio_codec /
              duration / width / height / file_size / error 等字段。
              任何异常都不会抛出，而是以 is_corrupted=True 返回。
    """
    fp = Path(file_path)
    ffprobe = _ffprobe_path()
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
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(fp),
        ]
        # encoding=utf-8 + errors=replace：无论 ffprobe 输出何种字节(含乱码文件名/非UTF-8流标记)，
        # stdout/stderr 永远是 str，避免 Windows(GBK) 下解码失败导致 stdout=None 被误判为损坏。
        r = subprocess.run(check_cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=_creationflags(), timeout=60)
        if r.returncode != 0:
            info["is_corrupted"] = True
            info["error"] = (r.stderr or "").strip()[:200]
            return info

        # 第二步：详细流分析
        cmd = [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(fp),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=_creationflags(), timeout=120)
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
            info["is_compatible"] = fp.suffix.lower() == ".mp4" and (
                vc in ("", *_COMPAT_VIDEO))
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
        return True          # "不要硬解" 永远成立
    tool = ffmpeg or _ffmpeg_path()
    if not tool:
        return False
    return tools.probe_hwaccel(tool, accel)


# ───────────────────────── 分析 Worker ─────────────────────────

class FFprobeAnalyzer(QThread):
    """异步分析单个视频文件（包装 analyze_video 纯函数）。"""

    result = Signal(str, dict)   # 文件路径, 分析结果
    finished = Signal()

    def __init__(self, file_path: str | Path, parent=None):
        super().__init__(parent)
        self.file_path = str(file_path)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if self._stopped:
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
    progress = Signal(int)         # 进度百分比
    file_done = Signal(str, bool)  # 文件路径, 是否成功
    finished = Signal()

    def __init__(self, file_paths: list[str], task_type: str = "repair",
                 gpu_codec: str = "h264_nvenc", hw_accel: str = "cuda",
                 crf: int = 23, min_size_bytes: int = 0, parent=None,
                 precomputed: Optional[dict] = None, timeout: int = 900,
                 log_skips: bool = True):
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
        self._precomputed = {
            os.path.normpath(k): v for k, v in (precomputed or {}).items()
        }
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
            ffmpeg, self._gpu_codec, self._hw_accel, log=self.log.emit)
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
            self.log.emit("❌ 未找到 ffmpeg，请安装 FFmpeg 并加入系统 PATH，"
                          "或在「设置 → 外部工具」里指定 ffmpeg 可执行文件路径")
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
            if (self._task_type == "auto" and pre_info is not None
                    and not pre_info.get("is_corrupted")
                    and pre_info.get("is_compatible")
                    and not self._log_skips):
                stats["skipped"] += 1
                self.progress.emit(int((idx + 1) / total * 100))
                continue

            self.log.emit(f"[{idx+1}/{total}] 处理: {path.name}")

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
            self.log.emit(f"  🔄 不兼容 Jellyfin (编码: {info['video_codec'] or '未知'})，转码中...")
            self._do_transcode(path, stats)
        else:
            # 复用扫描结果 + 不刷跳过日志时，这里已经由 run() 顶部静默处理，只计数
            if self._log_skips or self._precomputed.get(os.path.normpath(str(path))) is None:
                self.log.emit("  ✅ 文件兼容且正常，跳过")
            stats["skipped"] += 1

    def _do_repair(self, path: Path, stats: dict) -> None:
        """GPU 加速修复（检测坏帧并跳过）。

        安全策略：先产出 temp 文件，确认成功后再 os.replace 覆盖原文件；
        失败或中断时只删除 temp，原文件不受影响。
        """
        temp = path.with_name(f"temp_repair_{path.name}")
        cmd = [
            self._ffmpeg_bin(), "-err_detect", "ignore_err",
            "-hwaccel", self._hw_accel,
            "-i", str(path),
            *self._encoder_args(),
            "-c:a", "copy",
            "-strict", "-2", "-y", str(temp),
        ]
        bad_frames = 0
        error_keywords = ["error while decoding", "failed", "corrupt",
                          "concealing", "invalid"]
        timed_out = False

        try:
            self._current_process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="ignore",
                creationflags=_creationflags(),
            )
            # 带超时读取：避免损坏文件让 ffmpeg 无限挂起卡死队列
            try:
                for line in self._current_process.stdout:
                    if self._stopped:
                        break
                    line_lower = line.lower()
                    if any(kw in line_lower for kw in error_keywords):
                        bad_frames += 1
                        if bad_frames <= 3:
                            self.log.emit(f"  [坏帧] {line.strip()[-120:]}")
                # 读完后等待进程结束，设置超时
                self._current_process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    self._current_process.kill()
                    self._current_process.wait(timeout=5)
                except Exception:  # noqa: BLE001 — kill 后 wait 可能再次超时/已被清理
                    pass
                self.log.emit(
                    f"  ⏱️ 修复超时（>{self._timeout}s），该文件已中止，保留原文件")

            if self._stopped:
                temp.unlink(missing_ok=True)
                return

            if (self._current_process.returncode == 0
                    and temp.exists() and temp.stat().st_size > 0 and not timed_out):
                self.log.emit(f"  共发现 {bad_frames} 处故障，正在覆盖原文件...")
                os.replace(str(temp), str(path))
                stats["repaired"] += 1
                self.file_done.emit(str(path), True)
            else:
                temp.unlink(missing_ok=True)
                stats["failed"] += 1
                self.log.emit("  ❌ FFmpeg 处理失败，已保留原文件")
                self.file_done.emit(str(path), False)
        except Exception as e:
            stats["failed"] += 1
            self.log.emit(f"  ❌ 异常: {e}")
            self.file_done.emit(str(path), False)
            temp.unlink(missing_ok=True)
        finally:
            self._current_process = None

    def _do_transcode(self, path: Path, stats: dict) -> None:
        """智能转码为 Jellyfin 兼容 MP4。

        安全策略：新文件已就绪后，先替换/落位目标文件，再删除旧格式源文件；
        任一步失败都不破坏原文件。
        """
        target = path.with_suffix(".mp4")
        temp = path.with_name(f"temp_transcode_{path.stem}.mp4")
        hwaccel_args = ["-hwaccel", self._hw_accel] if self._hw_accel not in ("none", "") else []
        cmd = [
            self._ffmpeg_bin(), "-v", "error", "-y",
            *hwaccel_args,
            "-i", str(path),
            *self._encoder_args(),
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(temp),
        ]
        try:
            self._current_process = subprocess.Popen(
                cmd, creationflags=_creationflags(),
            )
            timed_out = False
            try:
                # 带超时等待：不兼容/损坏文件可能让 ffmpeg 无限卡住
                self._current_process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    self._current_process.kill()
                    self._current_process.wait(timeout=5)
                except Exception:  # noqa: BLE001 — kill 后 wait 可能再次超时/已被清理
                    pass
                self.log.emit(
                    f"  ⏱️ 转码超时（>{self._timeout}s），该文件已中止，保留原文件")

            if self._stopped:
                temp.unlink(missing_ok=True)
                return

            if (self._current_process.returncode == 0
                    and temp.exists() and temp.stat().st_size > 0
                    and not timed_out):
                # 先落位新文件（覆盖同名的 .mp4），成功后再删除旧格式源文件
                os.replace(str(temp), str(target))
                if path != target and path.exists():
                    path.unlink()
                stats["transcoded"] += 1
                self.file_done.emit(str(target), True)
                self.log.emit(f"  ✅ 转码完成 → {target.name}")
            else:
                temp.unlink(missing_ok=True)
                stats["failed"] += 1
                self.log.emit("  ❌ 转码失败，已保留原文件")
                self.file_done.emit(str(path), False)
        except Exception as e:
            stats["failed"] += 1
            self.log.emit(f"  ❌ 转码异常: {e}")
            self.file_done.emit(str(path), False)
            temp.unlink(missing_ok=True)
        finally:
            self._current_process = None
