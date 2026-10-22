"""Cancellable video sampling and exact-pixel evidence thumbnails.

The default sampler uses stratified accurate seeks across the full duration.
The legacy keyframe/fps path remains available explicitly for comparison only;
it can duplicate keyframes and is not used for detection.
"""

from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["SampledFrame", "FrameSampler", "SamplingError"]


class SamplingError(RuntimeError):
    """抽帧失败（缺 ffmpeg、文件损坏、超时…）。"""


class SamplingCancelled(SamplingError):
    """用户取消了抽帧。"""


class SamplingTimeout(SamplingError):
    """抽帧超过时间预算。"""


def _never_stop() -> bool:
    return False


@dataclass(frozen=True)
class SampledFrame:
    """一帧原始画面。``data`` 是 ``height * width * channels`` 字节。

    ``channels == 1`` 为灰度，``channels == 3`` 为 RGB（兼容彩色输入）。
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
        if self.width < 2 or self.height < 2:
            raise SamplingError("抽帧尺寸必须至少为 2×2")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise SamplingError("抽帧超时必须为有限正数")
        self.last_sampled_count = 0
        self.last_duration = 0.0

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * self.channels

    # ── 元信息 ──
    def _run_command(self, cmd, *, timeout, should_stop=_never_stop, input_data=None):
        """Poll process lifecycle without blocking on stdin/stdout pipe buffers."""
        with (
            tempfile.TemporaryFile() as source,
            tempfile.TemporaryFile() as output,
            tempfile.TemporaryFile() as errors,
        ):
            if input_data is not None:
                source.write(input_data)
                source.seek(0)
            if should_stop():
                raise SamplingCancelled("视频处理已取消")
            proc = subprocess.Popen(
                cmd,
                stdin=source,
                stdout=output,
                stderr=errors,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    if should_stop():
                        raise SamplingCancelled("视频处理已取消")
                    if time.monotonic() >= deadline:
                        raise SamplingTimeout(f"视频处理超时（{timeout:g}s）")
                    time.sleep(0.02)
                output.seek(0)
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 8192))
                return subprocess.CompletedProcess(
                    cmd, proc.returncode, output.read(), errors.read()
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def duration(self, path: str | Path, *, should_stop=_never_stop) -> float:
        """读时长（秒）。失败返回 0，调用方自行决定要不要跳过。"""
        exe = self.ffprobe or self.ffmpeg.replace("ffmpeg", "ffprobe")
        if not Path(exe).exists():
            return 0.0
        try:
            done = self._run_command(
                [exe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                timeout=min(60, self.timeout),
                should_stop=should_stop,
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
        self, path: str | Path, count: int = 60, *, duration: float = 0.,
        should_stop: Callable[[], bool] = _never_stop,
        log: Optional[Callable[[str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> list[SampledFrame]:
        """Stratified accurate seeks; source keyframes are decoded, never duplicated by fps."""
        if count < 1:
            raise SamplingError('抽帧数量必须为正数')
        dur = duration or self.duration(path, should_stop=should_stop)
        if not math.isfinite(dur) or dur <= 0:
            raise SamplingError('读不到有效视频时长')
        self.last_duration = dur
        self.last_sampled_count = 0
        deadline = time.monotonic() + self.timeout
        frames = []
        for index in range(count):
            if should_stop():
                raise SamplingCancelled('抽帧已取消')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SamplingTimeout('抽帧总时间超时')
            # Deterministic within-stratum jitter avoids repeating an exact
            # period while preserving coverage of the whole duration.
            offset = .35 + .3 * ((index * .61803398875) % 1.)
            timestamp = min(dur - .001, (index + offset) * dur / count)
            frame = self.frame_at(path, max(0., timestamp), should_stop=should_stop,
                                  timeout=min(60., remaining))
            if frame is None:
                # Container duration may extend past the last presentation
                # timestamp. Retry earlier within this same stratum.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SamplingTimeout('抽帧总时间超时')
                earlier = max(index * dur / count, timestamp - min(1., dur / count / 2))
                frame = self.frame_at(path, earlier, should_stop=should_stop,
                                      timeout=min(60., remaining))
            if frame is not None:
                frames.append(frame)
                self.last_sampled_count = len(frames)
            if log and (index % 10 == 0 or index == count - 1):
                log(f'精确取样 {index + 1}/{count}，有效 {len(frames)} 帧')
            if progress:
                progress(index + 1, count)
        if not frames:
            raise SamplingError('抽帧未输出任何完整帧')
        return frames

    def sample_keyframes(
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
        if count < 1:
            raise SamplingError("抽帧数量必须为正数")
        dur = duration or self.duration(path, should_stop=should_stop)
        if not math.isfinite(dur) or dur <= 0:
            raise SamplingError("读不到视频时长，可能文件损坏或不是视频")
        self.last_duration = dur

        interval = max(dur / max(count, 1), 0.04)
        vf = f"fps=1/{interval:.4f},{self._scale_filter()}"
        cmd = [
            self.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-skip_frame",
            "nokey",
            "-i",
            str(path),
            "-vf",
            vf,
            "-vsync",
            "0",
            "-frames:v",
            str(count),
            "-f",
            "rawvideo",
            "-pix_fmt",
            self.pixel_format,
            "pipe:1",
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
        self.last_sampled_count = 0
        if limit < 1:
            raise SamplingError("抽帧数量必须为正数")
        if should_stop():
            raise SamplingCancelled("抽帧已取消")
        messages: queue.Queue = queue.Queue(maxsize=2)
        stopped = threading.Event()
        stderr_tail = bytearray()
        proc = None
        readers = []
        deadline = time.monotonic() + self.timeout

        def send(value):
            while not stopped.is_set():
                try:
                    messages.put(value, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_stdout():
            try:
                pending = bytearray()
                while not stopped.is_set():
                    chunk = proc.stdout.read(self.frame_bytes - len(pending))
                    if not chunk:
                        if pending:
                            send(SamplingError("抽帧输出包含不完整帧"))
                        send(None)
                        return
                    pending.extend(chunk)
                    if len(pending) == self.frame_bytes:
                        send(bytes(pending))
                        pending.clear()
            except (OSError, ValueError) as exc:
                send(SamplingError(f"读取视频数据失败: {exc}"))

        def read_stderr():
            try:
                while not stopped.is_set():
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-8192]
            except (OSError, ValueError):
                pass

        def check_control():
            if should_stop():
                raise SamplingCancelled("抽帧已取消")
            if time.monotonic() >= deadline:
                raise SamplingTimeout(f"抽帧超时（{self.timeout:g}s）")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for suffix, target in [("stdout", read_stdout), ("stderr", read_stderr)]:
                thread = threading.Thread(
                    target=target, name=f"pixelation-pipe-{suffix}", daemon=True
                )
                readers.append(thread)
                thread.start()
            while len(out) < limit:
                check_control()
                try:
                    buf = messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(buf, Exception):
                    raise buf
                if buf is None:
                    break
                out.append(
                    SampledFrame(
                        timestamp=offset + len(out) * interval,
                        width=self.width,
                        height=self.height,
                        data=buf,
                        channels=self.channels,
                    )
                )
                self.last_sampled_count = len(out)
            while proc.poll() is None:
                check_control()
                stopped.wait(0.02)
            if proc.returncode != 0:
                raise SamplingError(
                    f"FFmpeg 退出码 {proc.returncode}: "
                    + stderr_tail.decode("utf-8", "replace")[-2000:]
                )
            if not out:
                raise SamplingError("抽帧未输出任何完整帧")
        except (OSError, subprocess.SubprocessError) as exc:
            raise SamplingError(f"抽帧失败: {exc}") from exc
        finally:
            if proc is not None:
                stopped.set()
                try:
                    if proc.poll() is None:
                        proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.SubprocessError:
                    pass
                for thread in readers:
                    thread.join(timeout=1)
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        return out

    # ── 单帧 / 缩略图 ──
    def write_sampled_thumbnail(
        self,
        frame: SampledFrame,
        out_path: str | Path,
        *,
        width: int = 480,
        should_stop=_never_stop,
        box=None,
        crop: bool = False,
    ) -> bool:
        """Encode the exact analyzed pixels, avoiding a second approximate seek."""
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fmt = "gray" if frame.channels == 1 else "rgb24"
        filters = []
        if box:
            x0, y0, x1, y1 = map(int, box)
            x0, y0 = max(0, min(x0, frame.width - 1)), max(0, min(y0, frame.height - 1))
            x1, y1 = max(x0 + 1, min(x1, frame.width)), max(y0 + 1, min(y1, frame.height))
            filters.append(f'crop={x1-x0}:{y1-y0}:{x0}:{y0}' if crop else
                           f'drawbox=x={x0}:y={y0}:w={x1-x0}:h={y1-y0}:color=red:t=3')
        filters.append(f'scale={width}:-2')
        done = self._run_command(
            [
                self.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                fmt,
                "-s",
                f"{frame.width}x{frame.height}",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                ','.join(filters),
                "-q:v",
                "4",
                str(out),
            ],
            timeout=min(90, self.timeout),
            should_stop=should_stop,
            input_data=frame.data,
        )
        return done.returncode == 0 and out.exists() and out.stat().st_size > 0

    def frame_at(self, path: str | Path, timestamp: float, *,
                  should_stop=_never_stop, timeout=60.) -> Optional[SampledFrame]:
        """精确跳到某一秒取一帧（给复核界面做缩略图用）。"""
        cmd = [
            self.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{max(timestamp, 0):.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            self._scale_filter(),
            "-f",
            "rawvideo",
            "-pix_fmt",
            self.pixel_format,
            "pipe:1",
        ]
        try:
            done = self._run_command(cmd, timeout=timeout, should_stop=should_stop)
        except (OSError, subprocess.SubprocessError):
            return None
        data = done.stdout
        if done.returncode != 0:
            raise SamplingError(f'单帧解码失败（退出码 {done.returncode}）：'
                                + done.stderr.decode('utf-8', 'replace')[-1000:])
        frame_bytes = self.frame_bytes
        if len(data) < frame_bytes:
            return None
        return SampledFrame(
            timestamp=timestamp,
            width=self.width,
            height=self.height,
            data=data[:frame_bytes],
            channels=self.channels,
        )

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
            self.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{max(timestamp, 0):.2f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            str(quality),
            str(out),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return out.exists() and out.stat().st_size > 0
