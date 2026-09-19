"""FFmpeg 分析逻辑测试（需要真实 ffmpeg/ffprobe，缺失时自动跳过）。

用 ffmpeg 的 lavfi 虚拟输入生成真实测试视频，验证 analyze_video 的判定逻辑。
"""

import subprocess
from pathlib import Path

import pytest

from workers.ffmpeg import (
    analyze_video, check_encoder_available, _FFMPEG, _FFPROBE,
)

pytestmark = pytest.mark.skipif(
    not _FFMPEG or not _FFPROBE,
    reason="系统未安装 ffmpeg/ffprobe",
)


def _make_video(path: Path, codec: str = "libx264", fmt: str = "mp4",
                duration: int = 1, audio: bool = False) -> Path:
    """用 ffmpeg 生成测试视频。"""
    cmd = [_FFMPEG, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=64x64:rate=5"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac"]
    cmd += [str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"无法生成测试视频（{codec} 不可用）: {r.stderr[:100]}")
    return path


class TestAnalyzeVideo:
    def test_healthy_mp4_h264(self, tmp_path):
        v = _make_video(tmp_path / "good.mp4", codec="libx264")
        info = analyze_video(v)
        assert info["is_corrupted"] is False
        assert info["is_compatible"] is True
        assert info["video_codec"] == "h264"

    def test_healthy_mp4_hevc(self, tmp_path):
        try:
            v = _make_video(tmp_path / "good_hevc.mp4", codec="libx265")
        except pytest.skip.Exception:
            pytest.skip("libx265 编码器不可用")
        info = analyze_video(v)
        assert info["is_corrupted"] is False
        assert info["video_codec"] in ("hevc", "h265")

    def test_incompatible_mkv_h264(self, tmp_path):
        # mkv 容器虽然编码兼容，但按工具规则（仅 .mp4 兼容）判定为不兼容
        v = _make_video(tmp_path / "movie.mkv", fmt="mkv")
        info = analyze_video(v)
        assert info["is_corrupted"] is False
        assert info["is_compatible"] is False

    def test_missing_file(self, tmp_path):
        info = analyze_video(tmp_path / "not_exist.mp4")
        assert info["is_corrupted"] is True

    def test_corrupted_file(self, tmp_path):
        p = tmp_path / "broken.mp4"
        p.write_bytes(b"\x00\x01\x02\x03 not a real video file at all")
        info = analyze_video(p)
        assert info["is_corrupted"] is True

    def test_duration_and_size(self, tmp_path):
        v = _make_video(tmp_path / "dur.mp4", duration=2)
        info = analyze_video(v)
        assert info["duration"] > 1.0
        assert info["file_size"] > 0


class TestEncoderCheck:
    def test_libx264_always_available(self):
        assert check_encoder_available("libx264") is True

    def test_bogus_encoder(self):
        assert check_encoder_available("h264_totally_bogus_xyz") is False


class TestJsonTolerance:
    """修复：ffprobe 输出含特殊字符(文件名乱码/反斜杠)导致 JSON 解析失败时，
    不应误判健康视频为损坏，而走 csv 兜底拿到 codec。"""

    def test_json_decode_break_falls_back_not_corrupt(self, tmp_path, monkeypatch):
        """JSON 解析抛异常时，回退 csv 探测，健康视频不被误判损坏。"""
        import json as _json
        from workers import ffmpeg as F
        v = _make_video(tmp_path / "good.mp4", codec="libx264")
        def _boom(data):
            raise _json.JSONDecodeError("bad escape", data, 0)
        monkeypatch.setattr(F.json, "loads", _boom)
        info = F.analyze_video(v)
        # 虽 JSON 解析失败，但 csv 兜底应能拿到 codec，不误判损坏
        assert info["is_corrupted"] is False
        assert info["video_codec"] == "h264"
        assert "解析异常" in info["error"] or info["error"]

    def test_probe_codecs_csv_healthy(self, tmp_path):
        from workers.ffmpeg import _probe_codecs_csv, _FFPROBE
        v = _make_video(tmp_path / "good.mp4", codec="libx264")
        vc, ac = _probe_codecs_csv(_FFPROBE, str(v))
        assert vc == "h264"

    def test_probe_codecs_csv_missing(self, tmp_path):
        from workers.ffmpeg import _probe_codecs_csv, _FFPROBE
        vc, ac = _probe_codecs_csv(_FFPROBE, str(tmp_path / "nope.mp4"))
        assert vc == "" and ac == ""
