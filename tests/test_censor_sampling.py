"""Exercise real child-process pipes, including incomplete output and cancellation."""

import sys
import threading
import time

import pytest

from censor.sampling import FrameSampler, SamplingError


def sampler(timeout=2):
    return FrameSampler(sys.executable, width=2, height=2, pixel_format="gray", timeout=timeout)


def read(code, *, timeout=2, stop=lambda: False, limit=1):
    return sampler(timeout)._read_frames([sys.executable, "-c", code], limit, 0, 1, stop)


def test_timeout_during_silent_pipe():
    start = time.monotonic()
    with pytest.raises(SamplingError, match="超时"):
        read("import time; time.sleep(5)", timeout=0.1)
    assert time.monotonic() - start < 2


def test_cancel_while_partial_frame_waits():
    start = time.monotonic()
    with pytest.raises(SamplingError, match="取消"):
        read(
            "import sys,time; sys.stdout.buffer.write(b'xx'); sys.stdout.flush(); time.sleep(5)",
            stop=lambda: time.monotonic() - start > 0.15,
        )
    assert time.monotonic() - start < 2
    assert not any(t.name.startswith("pixelation-pipe-") for t in threading.enumerate())


@pytest.mark.parametrize(
    "code",
    [
        "import sys; sys.stdout.buffer.write(b'xx')",
        "import sys; sys.exit(3)",
        "pass",
        "import sys; sys.stdout.buffer.write(b'1234'); sys.exit(3)",
    ],
)
def test_bad_output_is_not_success(code):
    with pytest.raises(SamplingError):
        read(code)


def test_split_writes_assemble_complete_frame():
    frames = read(
        "import sys,time; sys.stdout.buffer.write(b'12'); sys.stdout.flush(); "
        "time.sleep(.05); sys.stdout.buffer.write(b'34')"
    )
    assert frames[0].data == b"1234"


def test_short_but_complete_stream_is_valid():
    assert len(read("import sys; sys.stdout.buffer.write(b'1234')", limit=3)) == 1


@pytest.mark.parametrize("kwargs", [{"width": 0}, {"height": -2}, {"timeout": float("nan")}])
def test_invalid_sampler_parameters(kwargs):
    with pytest.raises((ValueError, SamplingError)):
        FrameSampler(sys.executable, **kwargs)


def test_controlled_command_cancels_silent_metadata_process():
    start = time.monotonic()
    with pytest.raises(SamplingError, match="取消"):
        sampler()._run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=4,
            should_stop=lambda: time.monotonic() - start > 0.1,
        )
    assert time.monotonic() - start < 2


def test_real_ffmpeg_sample_and_exact_thumbnail(tmp_path):
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg unavailable")
    video = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=5:duration=1",
            "-c:v",
            "libx264",
            "-g",
            "1",
            str(video),
        ],
        check=True,
        timeout=15,
    )
    tool = FrameSampler(ffmpeg, ffprobe, width=320, height=240)
    frames = tool.sample(video, count=2)
    assert frames and all(len(f.data) == 320 * 240 * 3 for f in frames)
    thumb = tmp_path / "thumb.jpg"
    assert tool.write_sampled_thumbnail(frames[0], thumb)
    assert thumb.read_bytes().startswith(b"\xff\xd8")


def test_cancel_saturated_queue_repeatedly_reaps_readers():
    for _ in range(3):
        start = time.monotonic()
        with pytest.raises(SamplingError, match="取消"):
            read(
                "import sys,time; sys.stdout.buffer.write(b'x'*100000); "
                "sys.stdout.flush(); time.sleep(5)",
                stop=lambda: time.monotonic() - start > 0.15,
                limit=1,
            )
        assert time.monotonic() - start < 2
        assert not any(t.name.startswith("pixelation-pipe-") for t in threading.enumerate())
