"""Real short-media regression cases, plus isolated CLI failure boundaries."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from config import ToolkitConfig
from utils import tools, merge


@pytest.fixture
def binaries():
    ffmpeg, ffprobe = tools.resolve_tool("ffmpeg"), tools.resolve_tool("ffprobe")
    assert ffmpeg and ffprobe, "controlled FFmpeg validation requires installed tools"
    return ffmpeg, ffprobe


def clip(ffmpeg, path, *, width=64, codec="libx264", two_audio=False, subtitle=None):
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={width}x64:r=10:d=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
    ]
    if two_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=660:duration=1"]
    if subtitle:
        cmd += ["-i", str(subtitle)]
    cmd += ["-map", "0:v", "-map", "1:a"]
    if two_audio:
        cmd += ["-map", "2:a"]
    if subtitle:
        cmd += ["-map", str(3 if two_audio else 2) + ":s"]
    cmd += ["-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", "aac"]
    if subtitle:
        cmd += ["-c:s", "mov_text" if path.suffix.lower() == ".mp4" else "srt"]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True, timeout=45)
    return path


def group(paths, ffprobe):
    parts = merge.group_by_number([str(p) for p in paths])[0]["ABC123"]
    for part in parts:
        part.probe = merge.probe_part(ffprobe, part.path)
    return merge.EpisodeGroup("ABC123", parts, selected_paths=[p.path for p in parts])


def test_real_transcode_preserves_existing_target_and_source(tmp_path, binaries):
    from workers.ffmpeg import FFmpegRepairWorker

    ffmpeg, _ = binaries
    src = clip(ffmpeg, tmp_path / "movie.mkv")
    dst = tmp_path / "movie.mp4"
    dst.write_bytes(b"keep existing version")
    w = FFmpegRepairWorker([str(src)], task_type="transcode", gpu_codec="libx264", hw_accel="none")
    w._ffmpeg = ffmpeg
    stats = dict(transcoded=0, failed=0)
    w._do_transcode(src, stats)
    assert dst.read_bytes() == b"keep existing version"
    assert src.exists()
    assert stats["failed"] == 1


def test_silent_repair_timeout_is_absolute(tmp_path, monkeypatch):
    from workers import ffmpeg as worker

    src = tmp_path / "movie.mp4"
    src.write_bytes(b"original")
    w = worker.FFmpegRepairWorker([str(src)], gpu_codec="libx264", hw_accel="none")
    w._ffmpeg = "fake"
    w._timeout = 0.2
    actual = subprocess.Popen

    def launch(*args, **kwargs):
        if args[0][0] == "fake":
            return actual([sys.executable, "-c", "import time;time.sleep(1.3)"], **kwargs)
        return actual(*args, **kwargs)

    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    start = time.monotonic()
    stats = {"repaired": 0, "failed": 0}
    w._do_repair(src, stats)
    assert time.monotonic() - start < 1
    assert src.read_bytes() == b"original" and stats["failed"] == 1


def test_real_merge_preserves_two_audio_tracks_and_external_offsets(tmp_path, binaries):
    from workers.merge import MergeWorker

    ffmpeg, ffprobe = binaries
    paths = []
    for index in (1, 2):
        path = clip(ffmpeg, tmp_path / f"ABC-123-cd{index}.mkv", two_audio=True)
        path.with_suffix(".eng.srt").write_text(f"1\n00:00:00,100 --> 00:00:00,500\npart {index}\n")
        paths.append(path)
    worker = MergeWorker(
        [group(paths, ffprobe)], ffmpeg, {"merge_output_container": "mkv"}, ffprobe
    )
    ok, output = worker._merge_group(group(paths, ffprobe))
    assert ok, output
    meta = subprocess.run(
        [ffprobe, "-v", "quiet", "-show_streams", "-of", "json", output],
        capture_output=True,
        text=True,
        check=True,
    )
    import json

    assert sum(s["codec_type"] == "audio" for s in json.loads(meta.stdout)["streams"]) == 2
    subtitles = Path(output).with_suffix(".eng.srt").read_text()
    assert "part 1" in subtitles and "part 2" in subtitles
    cues = __import__("utils.subtitle_clean", fromlist=["parse_srt"]).parse_srt(subtitles)
    assert cues[1]["start"] > 1 and cues[0]["start"] == pytest.approx(0.1)


def test_real_hevc_normalization_validates_new_h264_target(tmp_path, binaries):
    from workers.merge import MergeWorker

    ffmpeg, ffprobe = binaries
    paths = [
        clip(ffmpeg, tmp_path / f"ABC-123-cd{i}.mkv", width=width, codec="libx265")
        for i, width in [(1, 64), (2, 96)]
    ]
    g = group(paths, ffprobe)
    w = MergeWorker([g], ffmpeg, {"merge_allow_reencode_fallback": True}, ffprobe)
    ok, output = w._merge_group(g)
    assert ok, output
    assert merge.probe_part(ffprobe, output).video_codec == "h264"


def test_lada_nonzero_exit_never_accepts_stale_output(tmp_path, monkeypatch):
    from workers.lada_restore import LadaRestoreWorker
    from utils import proc

    src = tmp_path / "movie.mp4"
    src.write_bytes(b"video")
    (tmp_path / "movie.restored.mp4").write_bytes(b"old")
    cfg = ToolkitConfig()
    cfg.lada_validate_output = False
    monkeypatch.setattr(
        proc,
        "run_streaming",
        lambda *a, **kw: dict(
            code=2, error="", stopped=False, timed_out=False, lines=["bad argument"]
        ),
    )
    ok, _ = LadaRestoreWorker(cfg, [str(src)], "fake")._one(str(src), "lada-1")
    assert not ok


def test_whisper_failed_launch_does_not_clean_existing_subtitles(tmp_path, monkeypatch):
    from workers.whisper_gen import WhisperGenWorker
    from utils import whisper_tool

    src = tmp_path / "movie.mp4"
    src.write_bytes(b"video")
    sub = tmp_path / "movie.srt"
    content = "1\n00:00:01,000 --> 00:00:02,000\nold\n"
    sub.write_text(content)
    monkeypatch.setattr(
        whisper_tool,
        "run_one",
        lambda *a, **kw: dict(code=None, error="cannot start", stopped=False, lines=[]),
    )
    ok, _ = WhisperGenWorker(ToolkitConfig(), [str(src)], "fake")._one(str(src))
    assert not ok and sub.read_text() == content


def test_selected_gpu_query_uses_device_one(monkeypatch):
    from utils import gpu

    monkeypatch.setattr(gpu, "vram_query", lambda *a, **k: [(24, 2), (12, 11)])
    try:
        used = gpu.vram_used_gb("cuda:1")
    except TypeError:
        used = None
    assert used == 11


def test_gpu_gate_keeps_waiting_between_high_and_low():
    from utils import gpu

    assert hasattr(gpu, "VramGate"), "missing hysteresis gate"
    gate = gpu.VramGate(10, 8)
    assert not gate.allow(11)
    assert not gate.allow(9)
    assert gate.allow(7)


def test_newer_userdata_survives_transfer(tmp_path):
    from workers.database import transfer_userdata
    from tests.test_database import _make_db, _read_userdata

    old, new = tmp_path / "old.db", tmp_path / "new.db"

    def row(played, count, date):
        return ("user", None, 0, 1, date, 0, count, 0, played, None, 0, None)

    _make_db(old, ["/movie.ts"], userdata={"/movie.ts": [row(0, 1, "2020-01-01")]})
    ids = _make_db(
        new, [], mp4_paths=["/movie.mp4"], userdata={"/movie.mp4": [row(1, 9, "2026-01-01")]}
    )
    transfer_userdata(old, new)
    got = _read_userdata(new, ids["/movie.mp4"])[0]
    assert got[7] == 9 and got[9] == 1


def test_whisper_failure_after_partial_write_preserves_existing_caption(tmp_path, monkeypatch):
    from workers.whisper_gen import WhisperGenWorker
    from utils import whisper_tool

    src = tmp_path / "movie.mp4"
    src.write_bytes(b"video")
    sub = tmp_path / "movie.srt"
    sub.write_text("old valid caption")

    def fail(exe, path, **options):
        destination = Path(options["output_dir"]) if options["output_dir"] else Path(path).parent
        (destination / "movie.srt").write_text("partial broken output")
        return dict(code=1, error="engine failed", stopped=False, lines=[])

    monkeypatch.setattr(whisper_tool, "run_one", fail)
    ok, _ = WhisperGenWorker(ToolkitConfig(), [str(src)], "fake", {"--output_dir"})._one(str(src))
    assert not ok and sub.read_text() == "old valid caption"


def test_bad_lada_output_is_not_left_as_active_video(tmp_path, monkeypatch):
    from workers.lada_restore import LadaRestoreWorker
    from utils import lada_tool

    src = tmp_path / "movie.mp4"
    src.write_bytes(b"video")

    def fail(cli, path, out_dir, **kw):
        (Path(out_dir) / "movie.restored.mp4").write_bytes(b"partial bad output")
        return dict(code=1, error="failed", lines=[])

    monkeypatch.setattr(lada_tool, "run_one", fail)
    cfg = ToolkitConfig()
    cfg.lada_validate_output = False
    ok, _ = LadaRestoreWorker(cfg, [str(src)], "fake")._one(str(src), "lada-1")
    assert not ok and not (tmp_path / "movie.restored.mp4").exists()


def test_real_embedded_subtitle_survives_merge(tmp_path, binaries):
    from workers.merge import MergeWorker

    ffmpeg, ffprobe = binaries
    caption = tmp_path / "caption.srt"
    caption.write_text("1\n00:00:00,100 --> 00:00:00,500\nHello\n")
    paths = [clip(ffmpeg, tmp_path / f"ABC-123-cd{i}.mkv", subtitle=caption) for i in (1, 2)]
    g = group(paths, ffprobe)
    ok, output = MergeWorker([g], ffmpeg, {}, ffprobe)._merge_group(g)
    assert ok, output
    assert (
        sum(s["codec_type"] == "subtitle" for s in merge.probe_part(ffprobe, output).streams) == 1
    )


def test_planned_merge_output_collision_rejects_without_writes(tmp_path, binaries):
    from workers.merge import MergeWorker

    ffmpeg, ffprobe = binaries
    paths = [clip(ffmpeg, tmp_path / f"ABC-123-cd{i}.mp4") for i in (1, 2)]
    planned = tmp_path / "reviewed.mp4"
    planned.write_bytes(b"new independent user version")
    g = group(paths, ffprobe)
    ok, _ = MergeWorker([g], ffmpeg, {"planned_output": str(planned)}, ffprobe)._merge_group(g)
    assert not ok and planned.read_bytes() == b"new independent user version"


def test_bad_restored_video_never_replaces_real_original(tmp_path, binaries):
    from utils.replacement import preview_replacements, execute_replacements

    ffmpeg, _ = binaries
    target = clip(ffmpeg, tmp_path / "movie.mp4")
    before = target.read_bytes()
    source = tmp_path / "movie.restored.mp4"
    source.write_bytes(b"not a video")
    plan = preview_replacements([str(source)], [str(target)])
    stats = execute_replacements(plan, log=lambda _: None)
    assert (
        stats["ok"] == 0 and target.read_bytes() == before and source.read_bytes() == b"not a video"
    )


def test_real_restored_replacement_is_validated_and_reversible(tmp_path, binaries):
    from utils.replacement import preview_replacements, execute_replacements
    from utils.file_ops import OperationJournal

    ffmpeg, _ = binaries
    target = clip(ffmpeg, tmp_path / "movie.mp4")
    before = target.read_bytes()
    source = clip(ffmpeg, tmp_path / "movie.restored.mp4")
    journal = OperationJournal(tmp_path / "operations.db")
    stats = execute_replacements(
        preview_replacements([str(source)], [str(target)]), journal=journal, log=lambda _: None
    )
    assert stats["ok"] == 1 and not source.exists()
    restored = journal.restore(stats["operations"][0]["operation_id"])
    assert restored["ok"] and target.read_bytes() == before and source.exists()


def test_duplicate_names_have_distinct_subtitles_and_remain_discoverable(tmp_path, monkeypatch):
    from workers.whisper_gen import WhisperGenWorker
    from utils import whisper_tool, subtitle_clean

    paths = []
    for name in ("first", "second"):
        d = tmp_path / name
        d.mkdir()
        p = d / "movie.mp4"
        p.write_bytes(b"video")
        paths.append(str(p))

    def generate(exe, path, **options):
        text = f"1\n00:00:01,000 --> 00:00:02,000\n{Path(path).parent.name}\n"
        (Path(options["output_dir"]) / "movie.srt").write_text(text)
        return dict(code=0, error="", lines=[], stopped=False)

    monkeypatch.setattr(whisper_tool, "run_one", generate)
    cfg = ToolkitConfig()
    cfg.whisper_output_dir = str(tmp_path / "captions")
    cfg.whisper_cleanup = False
    worker = WhisperGenWorker(cfg, paths, "fake")
    for path in paths:
        assert worker._one(path)[0]
    first = subtitle_clean.find_subtitles(paths[0], ["srt"], cfg.whisper_output_dir)
    second = subtitle_clean.find_subtitles(paths[1], ["srt"], cfg.whisper_output_dir)
    assert len(first) == len(second) == 1 and first[0] != second[0]
    assert "first" in first[0].read_text() and "second" in second[0].read_text()


def test_separate_subtitle_jobs_do_not_share_same_basename_output(tmp_path, monkeypatch):
    from workers.whisper_gen import WhisperGenWorker
    from utils import whisper_tool, subtitle_clean

    def generate(exe, path, **options):
        (Path(options["output_dir"]) / "movie.srt").write_text(
            f"1\n00:00:01,000 --> 00:00:02,000\n{Path(path).parent.name}\n"
        )
        return dict(code=0, error="", lines=[], stopped=False)

    monkeypatch.setattr(whisper_tool, "run_one", generate)
    cfg = ToolkitConfig()
    cfg.whisper_output_dir = str(tmp_path / "captions")
    cfg.whisper_cleanup = False
    results = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        path = directory / "movie.mp4"
        path.write_bytes(b"video")
        assert WhisperGenWorker(cfg, [str(path)], "fake")._one(str(path))[0]
        results.append(subtitle_clean.find_subtitles(str(path), ["srt"], cfg.whisper_output_dir)[0])
    assert results[0] != results[1]
    assert "first" in results[0].read_text() and "second" in results[1].read_text()


def test_mov_text_keeps_mp4_container_and_caption_codec(tmp_path, binaries):
    from workers.merge import MergeWorker

    ffmpeg, ffprobe = binaries
    caption = tmp_path / "caption.srt"
    caption.write_text("1\n00:00:00,100 --> 00:00:00,500\nHello\n")
    paths = [clip(ffmpeg, tmp_path / f"ABC-123-cd{i}.mp4", subtitle=caption) for i in (1, 2)]
    g = group(paths, ffprobe)
    target = tmp_path / "planned.mp4"
    ok, output = MergeWorker([g], ffmpeg, {"planned_output": str(target)}, ffprobe)._merge_group(g)
    assert ok, output
    assert output == str(target)
    assert [
        s["codec_name"]
        for s in merge.probe_part(ffprobe, output).streams
        if s["codec_type"] == "subtitle"
    ] == ["mov_text"]


@pytest.mark.parametrize("change", ["modify", "add"])
def test_reviewed_merge_freezes_associated_subtitle_inputs(tmp_path, binaries, change):
    from utils.file_ops import file_hash
    from utils.merge_executor import preview_merge_inputs, merge_group_ffmpeg

    ffmpeg, ffprobe = binaries
    paths = [clip(ffmpeg, tmp_path / f"ABC-123-cd{i}.mp4") for i in (1, 2)]
    caption = paths[0].with_suffix(".eng.srt")
    caption.write_text("1\n00:00:00,100 --> 00:00:00,500\nreviewed\n")
    g = group(paths, ffprobe)
    inputs = preview_merge_inputs(g.parts)
    assert str(caption) in inputs
    hashes = {path: file_hash(path) for path in inputs}
    if change == "modify":
        caption.write_text("1\n00:00:00,100 --> 00:00:00,500\nchanged\n")
    else:
        paths[1].with_suffix(".eng.srt").write_text("1\n00:00:00,100 --> 00:00:00,500\nadded\n")
    target = tmp_path / "reviewed.mp4"
    ok, detail = merge_group_ffmpeg(
        g, ffmpeg, ffprobe, {"source_hashes": hashes, "planned_output": str(target)}
    )
    assert not ok and "改变" in detail
    assert not target.exists() and all(path.exists() for path in paths)


def test_invalid_second_subtitle_format_rolls_back_first_install(tmp_path, monkeypatch):
    from workers.whisper_gen import WhisperGenWorker
    from utils import whisper_tool

    path = tmp_path / "movie.mp4"
    path.write_bytes(b"video")
    caption = path.with_suffix(".srt")
    old = "1\n00:00:01,000 --> 00:00:02,000\nold\n"
    caption.write_text(old)

    def generate(exe, source, **options):
        destination = Path(options["output_dir"])
        (destination / "movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nnew\n")
        (destination / "movie.vtt").write_text("WEBVTT\n\ninvalid new caption")
        return dict(code=0, error="", lines=[], stopped=False)

    monkeypatch.setattr(whisper_tool, "run_one", generate)
    cfg = ToolkitConfig()
    cfg.whisper_sub_formats = "srt,vtt"
    cfg.whisper_cleanup = False
    assert not WhisperGenWorker(cfg, [str(path)], "fake")._one(str(path))[0]
    assert caption.read_text() == old


def test_video_health_import_does_not_load_qt():
    command = "from utils.video_health import analyze_video; import sys; assert not any(name.startswith(('PySide6', 'PyQt')) for name in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-B", "-c", command], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_video_health_uses_explicit_probe_without_global_configuration(
    tmp_path, binaries, monkeypatch
):
    from utils.video_health import analyze_video

    ffmpeg, ffprobe = binaries
    path = clip(ffmpeg, tmp_path / "healthy.mp4")

    def resolve(*args):
        raise AssertionError("explicit ffprobe must not resolve global tool configuration")

    monkeypatch.setattr(tools, "resolve_tool", resolve)
    info = analyze_video(path, ffprobe=ffprobe)
    assert not info["is_corrupted"] and info["video_codec"] == "h264"
    assert info["duration"] > 0 and info["file_size"] == path.stat().st_size
