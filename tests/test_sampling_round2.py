"""Synthetic-only regressions for accurate frame timestamps and thumbnail lifecycle."""

import shutil
import subprocess
import sys
import time

import pytest

from censor.sampling import FrameSampler, SamplingCancelled, SamplingError, SamplingTimeout


@pytest.fixture
def binaries():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg unavailable")
    return ffmpeg, ffprobe


def synthetic_video(tmp_path, binaries, *, offset=0, vfr=False, rate="2"):
    """Five known gray frames, only the first being an H.264 keyframe."""
    video = tmp_path / "synthetic.mp4"
    cmd = [binaries[0], "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray",
           "-s", "32x32", "-r", rate, "-i", "pipe:0"]
    if vfr:
        # Hand-derived PTS: 0, 0.5, 1.5, 3, 5 seconds.
        cmd += ["-vf", "setpts=N*(N+1)/(4*TB)"]
    cmd += ["-fps_mode", "vfr", "-c:v", "libx264", "-qp", "0", "-g", "100",
            "-sc_threshold", "0", "-output_ts_offset", str(offset), str(video)]
    subprocess.run(cmd, input=b"".join(bytes([v]) * 1024 for v in (20, 60, 100, 140, 180)),
                   capture_output=True, check=True, timeout=20)
    return video


@pytest.mark.parametrize("offset", [0, 7])
@pytest.mark.parametrize("vfr, expected", [(False, 1.0), (True, 1.5)])
def test_actual_presentation_time_matches_decoded_non_keyframe(tmp_path, binaries, offset, vfr, expected):
    video = synthetic_video(tmp_path, binaries, offset=offset, vfr=vfr)
    tool = FrameSampler(*binaries, width=32, height=32, pixel_format="gray")
    frame = tool.frame_at(video, 0.7)
    assert frame is not None
    assert frame.data == bytes([100]) * 1024
    assert frame.timestamp == pytest.approx(expected, abs=1e-7)
    # Seeking to evidence time must reproduce the very same analyzed frame.
    assert tool.frame_at(video, frame.timestamp).data == frame.data


def test_presentation_time_uses_integer_pts_precision(tmp_path, binaries):
    video = synthetic_video(tmp_path, binaries, rate="30000/1001")
    frame = FrameSampler(*binaries, width=32, height=32).frame_at(video, 0.05)
    assert frame.timestamp == pytest.approx(2002 / 30000, abs=1e-10)


def test_large_frame_metadata_does_not_hide_presentation_time(tmp_path, binaries):
    source = synthetic_video(tmp_path, binaries)
    tagged = tmp_path / "synthetic_metadata.mp4"
    subprocess.run([binaries[0], "-v", "error", "-i", str(source), "-c:v", "copy",
                    "-bsf:v", "h264_metadata=sei_user_data=0123456789abcdef0123456789abcdef+"
                    + "x" * 12000, str(tagged)],
                   capture_output=True, check=True, timeout=20)
    frame = FrameSampler(*binaries, width=32, height=32, pixel_format="gray").frame_at(tagged, 0)
    assert frame is not None
    assert frame.timestamp == 0
    assert frame.data == bytes([20]) * 1024


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), "bad", None])
def test_frame_at_rejects_invalid_timestamp_with_sampling_error(timestamp):
    with pytest.raises(SamplingError, match="时间"):
        FrameSampler(sys.executable, width=2, height=2).frame_at("unused", timestamp)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), "bad"])
def test_frame_at_rejects_invalid_timeout_with_sampling_error(timeout):
    with pytest.raises(SamplingError, match="超时"):
        FrameSampler(sys.executable, width=2, height=2).frame_at("unused", 0, timeout=timeout)


@pytest.mark.parametrize("at_eof", [False, True])
def test_thumbnail_failure_does_not_report_stale_output_as_success(tmp_path, binaries, at_eof):
    tool = FrameSampler(*binaries)
    source = synthetic_video(tmp_path, binaries) if at_eof else tmp_path / "missing.mp4"
    target = tmp_path / "thumbnail.jpg"
    target.write_bytes(b"previous evidence")
    assert tool.write_thumbnail(source, 100 if at_eof else 0, target) is False
    assert target.read_bytes() == b"previous evidence"


def test_thumbnail_success_atomically_replaces_existing_output(tmp_path, binaries):
    source = synthetic_video(tmp_path, binaries)
    target = tmp_path / "thumbnail.jpg"
    target.write_bytes(b"previous evidence")
    assert FrameSampler(*binaries).write_thumbnail(source, 0.7, target)
    assert target.read_bytes().startswith(b"\xff\xd8")


def slow_command(tool, monkeypatch):
    """Retain the real cancellable subprocess runner with a deterministic silent child."""
    run = tool._run_command
    monkeypatch.setattr(tool, "_run_command", lambda cmd, **kwargs: run(
        [sys.executable, "-c", "import time; time.sleep(5)"], **kwargs))


def test_thumbnail_cancellation_reaps_silent_child(tmp_path, monkeypatch):
    tool = FrameSampler(sys.executable, timeout=2)
    slow_command(tool, monkeypatch)
    start = time.monotonic()
    with pytest.raises(SamplingCancelled):
        tool.write_thumbnail("unused", 0, tmp_path / "out.jpg",
                             should_stop=lambda: time.monotonic() - start > 0.1)
    assert time.monotonic() - start < 2
    assert not list(tmp_path.iterdir())


def test_thumbnail_uses_sampler_timeout(tmp_path, monkeypatch):
    tool = FrameSampler(sys.executable, timeout=0.1)
    slow_command(tool, monkeypatch)
    start = time.monotonic()
    with pytest.raises(SamplingTimeout):
        tool.write_thumbnail("unused", 0, tmp_path / "out.jpg")
    assert time.monotonic() - start < 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kwargs", [{"timestamp": float("nan")}, {"width": 0},
                                    {"quality": "bad"}, {"timeout": float("inf")}])
def test_thumbnail_rejects_invalid_arguments(tmp_path, kwargs):
    arguments = {"timestamp": 0, **kwargs}
    with pytest.raises(SamplingError):
        FrameSampler(sys.executable).write_thumbnail("unused", out_path=tmp_path / "out.jpg",
                                                    **arguments)
