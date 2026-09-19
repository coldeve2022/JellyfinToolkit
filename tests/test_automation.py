"""自动化联动流水线单元测试（无界面、纯逻辑路径）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import ToolkitConfig  # noqa: E402
from utils.merge import EpisodeGroup, PartFile, PartProbe  # noqa: E402
from automation import pipeline as ap  # noqa: E402


def _probe():
    return PartProbe(ok=True, video_codec="h264", duration=100.0,
                     width=1280, height=720, fps=30.0, r_fps=30.0,
                     audio_codec="aac", container="mp4",
                     video_time_base="1/90000")


def _part(path, index):
    return PartFile(
        path=path, name=Path(path).name, stem=Path(path).stem,
        number="MIAA-743", part_index=index, part_kind="cd",
        probe=_probe(),
    )


def test_plan_merges_empty_inputs():
    plan = ap.plan_merges([], "", "", {}, log=lambda _: None)
    assert plan.groups == []
    assert plan.unrecognized == []


def test_merge_group_ffmpeg_rejects_duplicate_part_index():
    group = EpisodeGroup(
        number="MIAA743",
        parts=[
            _part("D:/x/a-cd1.mp4", 1),
            _part("D:/x/b-cd1.mp4", 1),
        ],
        selected_paths=["D:/x/a-cd1.mp4", "D:/x/b-cd1.mp4"],
    )
    ok, detail = ap.merge_group_ffmpeg(group, "ffmpeg", "ffprobe", {}, log=lambda _: None)
    assert not ok
    assert "相同分集序号" in detail


def test_run_pipeline_dry_run_with_missing_roots():
    report = ap.run_pipeline(
        ToolkitConfig(),
        roots=["Z:/no_such_automation_dir"],
        ffmpeg="", ffprobe="",
        check_old=False,
        log=lambda _: None,
    )
    assert report["paths"] == 0
    assert report["merge"]["total_groups"] == 0
    assert report["merge"]["executed"] == 0
    assert report["apply_merge"] is False
