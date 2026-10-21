"""抽帧 —— 把"从视频里取几帧"这件事做对，做快，做可中断。

为什么不直接 ``fps=1/N`` 全片解码
--------------------------------
实测一部 3.6 小时、6.8 GB 的片子，``-vf fps=1/120`` 会**解码每一帧**，
单部要十几分钟 —— 700 多部片就是按天算。加上 ``-skip_frame nokey``
只解关键帧后，同样的全片覆盖只要 **16.6 秒**。

所以这里的策略是：

1. ``-skip_frame nokey`` 只解 I 帧；
2. 再用 ``fps`` 滤镜按 ``时长/帧数`` 均匀稀释到 N 帧；
3. ``scale+pad`` 输出固定尺寸灰度图 —— 尺寸固定才能直接按字节切片，
   不需要额外依赖去读图；
4. 逐帧读取，**每读一帧检查一次是否要取消**，用户点停止能立刻停。

尺寸固定为 W×H 还有一个好处：不同分辨率的片子拿到的特征可比。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["SampledFrame", "FrameSampler", "SamplingError"]


class SamplingError(RuntimeError):
    """抽帧失败（缺 ffmpeg、文件损坏、超时…）。"""


def _never_stop() -> bool:
    return False


@dataclass(frozen=True)
class SampledFrame:
    """一帧原始画面。``data`` 是 ``height * width * channels`` 字节。

    ``channels == 1`` 为灰度，``channels == 3`` 为 RGB（用来算肤色占比）。
    """

    timestamp: float
    width: int
    height: int
    data: bytes
    channels: int = 1

    @property
    def hms(self) -> str:
        total = max(int(self.timestamp), 0)
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class FrameSampler:
    """按固定尺寸抽帧。**不依赖 numpy**，只吐字节。"""

    def __init__(
        self,
        ffmpeg: str,
        ffprobe: str = "",
        *,
        width: int = 1280,
        height: int = 720,
        pixel_format: str = "rgb24",
        timeout: float = 900.0,
    ) -> None:
        if not ffmpeg or not Path(ffmpeg).exists():
            raise SamplingError(f"找不到 ffmpeg: {ffmpeg!r}")
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe or ""
        self.width = int(width) - int(width) % 2
        self.height = int(height) - int(height) % 2
        self.pixel_format = pixel_format if pixel_format in {"gray", "rgb24"} else "rgb24"
        self.channels = 1 if self.pixel_format == "gray" else 3
        self.timeout = float(timeout)

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * self.channels

    # ── 元信息 ──
    def duration(self, path: str | Path) -> float:
        """读时长（秒）。失败返回 0，调用方自行决定要不要跳过。"""
        exe = self.ffprobe or self.ffmpeg.replace("ffmpeg", "ffprobe")
        if not Path(exe).exists():
            return 0.0
        try:
            done = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(path)],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return 0.0
        try:
            return float(json.loads(done.stdout or "{}")["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            return 0.0

    # ── 抽帧 ──
    def _scale_filter(self) -> str:
        return (
            f"scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,"
            f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2"
        )

    def sample(
        self,
        path: str | Path,
        count: int = 60,
        *,
        duration: float = 0.0,
        should_stop: Callable[[], bool] = _never_stop,
        log: Optional[Callable[[str], None]] = None,
    ) -> list[SampledFrame]:
        """全片均匀抽 ``count`` 帧，随时可取消。

        速度上的实测结论（3.6 小时 / 6.8 GB 片源，机械盘）：

        =========================  ==========
        做法                        耗时
        =========================  ==========
        ``-vf fps=1/N`` 全帧解码    十几分钟（不可用）
        ``-skip_frame nokey`` 单遍  约 31 秒
        逐点 ``-ss`` seek（60 次）  约 26 秒
        =========================  ==========

        也就是说瓶颈是"关键帧解码"本身（约 9 秒/小时片长），seek 也快不到
        哪里去 —— 在机械盘上随机 seek 反而更慢。所以这里选**单遍 + 可取消**，
        整库速度靠上层多路并行解决，而不是在这里抠 ffmpeg 参数。
        """
        dur = duration or self.duration(path)
        if dur <= 0:
            raise SamplingError("读不到视频时长，可能文件损坏或不是视频")

        interval = max(dur / max(count, 1), 0.04)
        vf = f"fps=1/{interval:.4f},{self._scale_filter()}"
        cmd = [
            self.ffmpeg, "-v", "error", "-nostdin",
            "-skip_frame", "nokey", "-i", str(path),
            "-vf", vf, "-vsync", "0",
            "-frames:v", str(count),
            "-f", "rawvideo", "-pix_fmt", self.pixel_format, "pipe:1",
        ]
        frames = self._read_frames(cmd, count, 0.0, interval, should_stop)
        if log:
            log(f"抽帧完成 {len(frames)}/{count} 帧（间隔 {interval:.1f}s）")
        return frames

    def _read_frames(
        self,
        cmd: list[str],
        limit: int,
        offset: float,
        interval: float,
        should_stop: Callable[[], bool],
    ) -> list[SampledFrame]:
        """跑一次 ffmpeg，按固定帧长逐帧读取，随时可取消。"""
        out: list[SampledFrame] = []
        frame_bytes = self.frame_bytes
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            assert proc.stdout is not None
            while len(out) < limit:
                if should_stop():
                    break
                buf = proc.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                out.append(SampledFrame(
                    timestamp=offset + len(out) * interval,
                    width=self.width, height=self.height,
                    data=buf, channels=self.channels,
                ))
        except (OSError, subprocess.SubprocessError) as exc:
            raise SamplingError(f"抽帧失败: {exc}") from exc
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.SubprocessError:
                    pass
        return out

    # ── 单帧 / 缩略图 ──
    def frame_at(self, path: str | Path, timestamp: float) -> Optional[SampledFrame]:
        """精确跳到某一秒取一帧（给复核界面做缩略图用）。"""
        cmd = [
            self.ffmpeg, "-v", "error", "-nostdin",
            "-ss", f"{max(timestamp, 0):.2f}", "-i", str(path),
            "-frames:v", "1", "-vf", self._scale_filter(),
            "-f", "rawvideo", "-pix_fmt", self.pixel_format, "pipe:1",
        ]
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        data = done.stdout
        frame_bytes = self.frame_bytes
        if len(data) < frame_bytes:
            return None
        return SampledFrame(timestamp=timestamp, width=self.width,
                            height=self.height, data=data[:frame_bytes],
                            channels=self.channels)

    def write_thumbnail(
        self,
        path: str | Path,
        timestamp: float,
        out_path: str | Path,
        *,
        width: int = 480,
        quality: int = 4,
    ) -> bool:
        """直接让 ffmpeg 写 jpg 缩略图 —— 不经过 Python 图像库。"""
        out = Path(out_path)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        cmd = [
            self.ffmpeg, "-v", "error", "-nostdin", "-y",
            "-ss", f"{max(timestamp, 0):.2f}", "-i", str(path),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", str(quality), str(out),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return out.exists() and out.stat().st_size > 0
