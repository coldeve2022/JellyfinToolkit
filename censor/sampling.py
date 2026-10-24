"""Cancellable video sampling and exact-pixel evidence thumbnails.

The default sampler uses stratified accurate seeks across the full duration.
The legacy keyframe/fps path remains available explicitly for comparison only;
it can duplicate keyframes and is not used for detection.
"""

from __future__ import annotations

import json
import math
import queue
import re
import subprocess
import threading
import tempfile
import time
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

__all__ = ["SampledFrame", "FrameSampler", "SamplingError"]


class SamplingError(RuntimeError):
    """抽帧失败（缺 ffmpeg、文件损坏、超时…）。"""


class SamplingCancelled(SamplingError):
    """用户取消了抽帧。"""


class SamplingTimeout(SamplingError):
    """抽帧超过时间预算。"""


def _never_stop() -> bool:
    return False


_FRAME_TIME_BASE = re.compile(
    rb'\[showinfo@sampling[^\]]*\]\s+config in time_base:\s*(\d+)/(\d+)'
)
_FRAME_PTS = re.compile(
    rb'\[showinfo@sampling[^\]]*\]\s+n:\s*0\s+pts:\s*(-?\d+)\s'
)


def _finite_time(value, label: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SamplingError(f"{label}必须为有限数值") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise SamplingError(f"{label}必须为有限{'正数' if positive else '数值'}")
    return number


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
    def _run_command(self, cmd, *, timeout, should_stop=_never_stop, input_data=None,
                     stderr_patterns=()):
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
                # Keep small, explicitly requested metadata records even if
                # large SEI or container metadata displaces them from the tail.
                records = {}
                if stderr_patterns:
                    errors.seek(0)
                    for line in iter(lambda: errors.readline(65536), b''):
                        for pattern in stderr_patterns:
                            if pattern not in records:
                                match = pattern.search(line)
                                if match:
                                    records[pattern] = match[0] + b'\n'
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 8192))
                return subprocess.CompletedProcess(
                    cmd, proc.returncode, output.read(), b''.join(records.values()) + errors.read()
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
        return list(self.iter_samples(path, count, duration=duration, should_stop=should_stop,
                                      log=log, progress=progress))

    def iter_samples(
        self, path: str | Path, count: int = 60, *, duration: float = 0.,
        should_stop: Callable[[], bool] = _never_stop,
        log: Optional[Callable[[str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        sample_indices: Optional[Sequence[int]] = None,
        deadline: Optional[float] = None,
    ):
        """Stratified accurate seeks; source keyframes are decoded, never duplicated by fps."""
        if count < 1:
            raise SamplingError('抽帧数量必须为正数')
        indices = list(range(count)) if sample_indices is None else list(sample_indices)
        if (not indices or any(not isinstance(index, int) or isinstance(index, bool)
                               or not 0 <= index < count for index in indices)
                or len(indices) != len(set(indices))):
            raise SamplingError('抽帧索引必须为计划范围内不重复的整数')
        dur = duration or self.duration(path, should_stop=should_stop)
        if not math.isfinite(dur) or dur <= 0:
            raise SamplingError('读不到有效视频时长')
        self.last_duration = dur
        self.last_sampled_count = 0
        deadline = min(time.monotonic() + self.timeout, deadline) if deadline is not None else time.monotonic() + self.timeout
        for completed, index in enumerate(indices, 1):
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
                self.last_sampled_count += 1
                yield frame
            if log and (completed % 10 == 1 or completed == len(indices)):
                log(f'精确取样 {completed}/{len(indices)}，有效 {self.last_sampled_count} 帧')
            if progress:
                progress(completed, len(indices))
        if not self.last_sampled_count:
            raise SamplingError('抽帧未输出任何完整帧')

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
        if out.suffix.lower() == '.png':
            if should_stop():
                raise SamplingCancelled('证据图生成已取消')
            return self._write_png(frame, out, box=box, crop=crop)
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

    @staticmethod
    def _write_png(frame, out, *, box=None, crop=False):
        """Lossless evidence from exact pixels using only the standard library."""
        w, h, channels = frame.width, frame.height, frame.channels
        data = frame.data
        if len(data) != w * h * channels or channels not in (1, 3):
            raise SamplingError('证据图像素长度或通道无效')
        if box:
            x0, y0, x1, y1 = map(int, box)
            x0, y0 = max(0, min(w - 1, x0)), max(0, min(h - 1, y0))
            x1, y1 = max(x0 + 1, min(w, x1)), max(y0 + 1, min(h, y1))
            if crop:
                data = b''.join(data[(y * w + x0) * channels:(y * w + x1) * channels]
                                for y in range(y0, y1))
                w, h = x1 - x0, y1 - y0
            else:
                if channels == 1:
                    rgb = bytearray(w * h * 3)
                    rgb[0::3] = rgb[1::3] = rgb[2::3] = data
                    data, channels = rgb, 3
                else:
                    data = bytearray(data)
                for y in range(y0, y1):
                    for x in (range(x0, x1) if y < y0 + 3 or y >= y1 - 3
                              else tuple(range(x0, min(x0 + 3, x1))) + tuple(range(max(x0, x1 - 3), x1))):
                        pos = (y * w + x) * 3
                        data[pos:pos + 3] = b'\xff\x00\x00'
        stride = w * channels
        raw = b''.join(b'\x00' + bytes(data[y * stride:(y + 1) * stride]) for y in range(h))
        def chunk(kind, payload):
            return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
        payload = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8,
                    0 if channels == 1 else 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 3)) + chunk(b'IEND', b''))
        out.write_bytes(payload)
        return True

    def frame_at(self, path: str | Path, timestamp: float, *,
                  should_stop=_never_stop, timeout=60.) -> Optional[SampledFrame]:
        """Accurate seek, returning the decoded frame's presentation time from playback start."""
        timestamp = max(0., _finite_time(timestamp, '取样时间'))
        timeout = min(self.timeout, _finite_time(timeout, '单帧超时', positive=True))
        cmd = [
            self.ffmpeg,
            "-hide_banner", "-nostats", "-v", "info",
            "-nostdin",
            # Keep the frame's PTS, normalized by the input start_time rather
            # than the seek point. No extra ffprobe process is needed.
            "-copyts", "-start_at_zero",
            "-ss",
            f"{timestamp:.9f}",
            "-accurate_seek",
            "-threads", "1",
            "-i",
            str(path),
            "-map", "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            self._scale_filter() + ",trim=end_frame=1,showinfo@sampling=checksum=0",
            "-fps_mode", "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            self.pixel_format,
            "pipe:1",
        ]
        try:
            done = self._run_command(cmd, timeout=timeout, should_stop=should_stop,
                                     stderr_patterns=(_FRAME_TIME_BASE, _FRAME_PTS))
        except (OSError, subprocess.SubprocessError):
            return None
        data = done.stdout
        if done.returncode != 0:
            raise SamplingError(f'单帧解码失败（退出码 {done.returncode}）：'
                                + done.stderr.decode('utf-8', 'replace')[-1000:])
        frame_bytes = self.frame_bytes
        if not data:
            return None
        if len(data) != frame_bytes:
            raise SamplingError('单帧解码输出包含不完整或多余的像素')
        # pts_time is printed with limited significant digits. Integer PTS
        # and the input time base preserve precision even for long videos.
        time_base = _FRAME_TIME_BASE.search(done.stderr)
        pts = _FRAME_PTS.search(done.stderr)
        if not time_base or not pts or not all(int(v) > 0 for v in time_base.groups()):
            raise SamplingError('单帧解码未提供有效显示时间戳')
        actual_timestamp = int(pts[1]) * int(time_base[1]) / int(time_base[2])
        if not math.isfinite(actual_timestamp) or actual_timestamp < 0:
            raise SamplingError('单帧解码显示时间戳无效')
        return SampledFrame(
            timestamp=actual_timestamp,
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
        should_stop: Callable[[], bool] = _never_stop,
        timeout: Optional[float] = None,
    ) -> bool:
        """Write a fresh thumbnail with the common cancellation/timeout lifecycle."""
        timestamp = max(0., _finite_time(timestamp, '缩略图时间'))
        budget = min(90., self.timeout) if timeout is None else min(
            self.timeout, _finite_time(timeout, '缩略图超时', positive=True))
        try:
            valid_width, valid_quality = int(width), int(quality)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SamplingError('缩略图宽度或质量必须为有效整数') from exc
        if (valid_width != width or valid_width < 2
                or valid_quality != quality or not 1 <= valid_quality <= 31):
            raise SamplingError('缩略图宽度必须至少为2，质量必须为1到31的整数')
        out = Path(out_path)
        temporary = None
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            # FFmpeg can exit successfully without a frame (e.g. seek past
            # EOF). A separate empty output cannot be mistaken for an old file.
            with tempfile.NamedTemporaryFile(prefix=f'.{out.stem}-', suffix=out.suffix,
                                             dir=out.parent, delete=False) as stream:
                temporary = Path(stream.name)
            cmd = [
                self.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                f"{timestamp:.9f}",
                "-accurate_seek",
                "-i",
                str(path),
                "-map", "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                f"scale={valid_width}:-2",
                "-q:v",
                str(valid_quality),
                str(temporary),
            ]
            done = self._run_command(cmd, timeout=budget, should_stop=should_stop)
            if done.returncode != 0 or not temporary.exists() or not temporary.stat().st_size:
                return False
            temporary.replace(out)
            return True
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
