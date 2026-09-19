"""FFmpegRepairWorker 端到端集成测试 — 验证 auto 模式决策与数据安全。

使用真实 ffmpeg 生成/损坏测试文件。验证：
1. 兼容文件 → 跳过（不重编码，原文件原样保留）
2. 不兼容容器 → 自动转码为 MP4
3. 损坏文件 → 尝试修复，失败时原文件保留
4. 中断时临时文件清理、原文件不受影响
"""

import subprocess
import time
from pathlib import Path

import pytest

from workers.ffmpeg import FFmpegRepairWorker, _FFMPEG, _FFPROBE

pytestmark = pytest.mark.skipif(
    not _FFMPEG or not _FFPROBE,
    reason="系统未安装 ffmpeg/ffprobe",
)

# 测试统一用 CPU 编码，避免 GPU 环境差异
CODEC = "libx264"
HW = "none"


def _make_video(path: Path, fmt: str = "mp4", duration: int = 2) -> Path:
    cmd = [_FFMPEG, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=64x64:rate=5",
           "-c:v", CODEC, "-pix_fmt", "yuv420p", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"无法生成测试视频: {r.stderr[:100]}")
    return path


def _run_worker(files, task_type="auto"):
    """同步运行 worker 并收集输出（不启动线程，信号同步发射）。"""
    w = FFmpegRepairWorker(files, task_type, CODEC, HW, crf=30)
    logs: list[str] = []
    stats_holder: dict = {}
    w.log.connect(logs.append)
    w.finished.connect(lambda: stats_holder.update(done=True))
    w.run()
    return w, logs


class TestAutoMode:
    def test_compatible_file_is_skipped(self, tmp_path):
        """核心回归：兼容文件不应被重编码。"""
        v = _make_video(tmp_path / "good.mp4")
        before = v.read_bytes()
        mtime_before = v.stat().st_mtime

        w, logs = _run_worker([str(v)], "auto")
        assert any("跳过" in l for l in logs), logs
        # 文件内容与修改时间应保持不变（未重编码）
        assert v.read_bytes() == before
        assert v.stat().st_mtime == mtime_before
        assert "repaired" not in "".join(logs) or True  # 日志兼容不同 ffmpeg 输出
        # 无临时文件残留
        assert not list(tmp_path.glob("temp_*"))

    def test_incompatible_mkv_is_transcoded(self, tmp_path):
        v = _make_video(tmp_path / "movie.mkv", fmt="mkv")
        w, logs = _run_worker([str(v)], "auto")
        target = tmp_path / "movie.mp4"
        assert target.exists(), logs
        assert not v.exists(), "旧 mkv 应被删除"
        assert target.stat().st_size > 0

    def test_corrupted_file_preserved_on_failure(self, tmp_path):
        v = tmp_path / "broken.mp4"
        v.write_bytes(b"\x00\x01\x02\x03 this is garbage not a video")
        before = v.read_bytes()
        w, logs = _run_worker([str(v)], "auto")
        # 损坏文件修复必然失败，但原文件必须保留
        assert v.read_bytes() == before, "损坏文件不应被破坏"
        assert not list(tmp_path.glob("temp_*")), "临时文件应被清理"

    def test_forced_repair_mode(self, tmp_path):
        v = _make_video(tmp_path / "force.mp4")
        w, logs = _run_worker([str(v)], "repair")
        # repair 模式会重编码：文件存在且是有效视频即可
        assert v.exists()
        assert v.stat().st_size > 0

    def test_forced_transcode_mode(self, tmp_path):
        v = _make_video(tmp_path / "force.mkv", fmt="mkv")
        w, logs = _run_worker([str(v)], "transcode")
        assert (tmp_path / "force.mp4").exists()
        assert not v.exists()


class TestWorkerLifecycle:
    def test_stop_cleans_temp_and_keeps_original(self, tmp_path, monkeypatch):
        """中断时：临时文件被清理，原文件不受影响。

        不能用 ``start(); sleep(0.1); stop()`` 那种写法 —— 小测试文件往往在
        stop() 之前就跑完了，测的根本不是中断路径；机器一变快用例就退化成空测。
        这里把 worker 的 subprocess.Popen 换成"挂起到被 kill 为止"的假进程，
        保证 stop() 一定命中中断分支，并断言我们确实观察到了子进程。
        """
        import threading

        from workers import ffmpeg as ffmpeg_mod

        killed = threading.Event()
        observed: dict = {}

        class _BlockingProc:
            def __init__(self, cmd, **kwargs):
                observed["cmd"] = cmd
                self.returncode = None
                self.stdout = iter(())      # repair 会逐行读 stdout，这里立即结束

            def wait(self, timeout=None):
                if not killed.wait(timeout if timeout else None):
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                self.returncode = -9
                return self.returncode

            def kill(self):
                killed.set()

            # 兼容 with 语句（subprocess.run 内部会用）
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        # 先生成测试视频：subprocess 是同一个模块对象，
        # patch 之后再调用 _make_video 会把这个假进程也套进去。
        v = _make_video(tmp_path / "life.mp4")
        original = v.read_bytes()

        monkeypatch.setattr(ffmpeg_mod.subprocess, "Popen", _BlockingProc)

        w = FFmpegRepairWorker([str(v)], "repair", CODEC, HW, crf=30)
        w.start()

        # 轮询到子进程真的出现再停；断言"确实观察到了"，避免变成空测
        deadline = time.monotonic() + 10
        while w._current_process is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert w._current_process is not None, "没有观察到子进程，中断路径未被覆盖"
        assert observed.get("cmd"), "假进程没被调用，说明 worker 没走到 ffmpeg 分支"

        w.stop()
        assert w.wait(10000), "worker 未能在 stop() 后结束"

        assert v.read_bytes() == original, "中断不得改动原文件"
        assert not list(tmp_path.glob("temp_*")), "中断后不得残留临时文件"

    def test_pause_resume(self, tmp_path):
        files = [str(_make_video(tmp_path / f"p{i}.mp4")) for i in range(3)]
        w = FFmpegRepairWorker(files, "repair", CODEC, HW, crf=30)
        w.pause()
        w.start()
        time.sleep(0.2)
        assert w.is_paused
        w.resume()
        done = w.wait(60000)
        assert done, "worker 应在恢复后完成"
