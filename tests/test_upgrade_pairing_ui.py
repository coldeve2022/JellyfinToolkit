"""版本管理的配对环节：ambiguous 行必须能选，否则整批永远卡住。

用户实测的卡点（截图）：
- 5 行 ready + 1 行 ambiguous
- ambiguous 那行只写"需要选择唯一版本并匹配封装格式"，界面上**没有任何可选的地方**
- 而且 `_sync_buttons` 要求**全部 ready** 才能「保存进度快照」→ 整批卡死
- 点「载入上次记录」还会直接抛 `[Errno 2] ... latest.json`

这里锁住两条：选完能变 ready；没快照时给中文提示而不是裸文件错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui.pages.replace import _UPGRADE_STATUS, apply_candidate_choice  # noqa: E402


def _row(status="ambiguous", candidates=None):
    return {
        "source": r"F:\破解视频\原\ABC-123.mp4",
        "target": "",
        "candidates": candidates or [r"F:\破解视频\破解后\ABC-123-1.mp4",
                                    r"F:\破解视频\破解后\ABC-123-2.mp4"],
        "status": status,
        "error": "需要选择唯一原文件并匹配封装格式",
    }


def test_choosing_a_candidate_makes_the_row_ready():
    rows = [_row()]
    assert apply_candidate_choice(rows, 0, rows[0]["candidates"][1]) is True
    assert rows[0]["status"] == "ready"
    assert rows[0]["target"] == rows[0]["candidates"][1]
    assert rows[0]["error"] == ""
    # 全部 ready 之后，"保存进度快照"才可能解锁
    assert all(r["status"] == "ready" for r in rows)


def test_choice_must_be_one_of_the_candidates():
    rows = [_row()]
    assert apply_candidate_choice(rows, 0, r"F:\别处\X.mp4") is False
    assert rows[0]["status"] == "ambiguous", "非法选择不能把状态改成 ready"


@pytest.mark.parametrize("index", [-1, 5])
def test_out_of_range_choice_is_rejected(index):
    assert apply_candidate_choice([_row()], index, "x") is False


def test_tolerates_non_list_input():
    assert apply_candidate_choice(None, 0, "x") is False
    assert apply_candidate_choice({"rows": []}, 0, "x") is False


def test_statuses_are_chinese():
    """状态列不该把 ready / ambiguous 直接丢给用户。"""
    assert _UPGRADE_STATUS["ready"] == "就绪"
    assert _UPGRADE_STATUS["ambiguous"] == "要选版本"
    assert _UPGRADE_STATUS["container_convert"] == "要转封装"
    assert _UPGRADE_STATUS["not_found"] == "没找到新版本"
    for value in _UPGRADE_STATUS.values():
        assert value.isascii() is False, "状态名要中文化"


def test_dropping_a_blocked_row_unblocks_the_rest():
    """一行无解（比如封装不同）也不能把整批卡死：移除后可继续。"""
    from ui.pages.replace import drop_plan_row

    rows = [
        _row("ready"),
        _row("container_convert", [r"F:/x/A.mkv"]),
    ]
    rows[0]["target"] = rows[0]["source"]
    assert drop_plan_row(rows, 1) is True
    assert len(rows) == 1
    assert all(r["status"] == "ready" for r in rows)


def test_drop_rejects_bad_index():
    from ui.pages.replace import drop_plan_row

    rows = [_row("ready")]
    assert drop_plan_row(rows, 3) is False
    assert drop_plan_row("nope", 0) is False
    assert len(rows) == 1




# ── 封装不同：转封装而不是拒绝 ──────────────────────────

def test_container_difference_is_convertible_not_blocked(tmp_path):
    """封装不同只是"盒子"不同，转封装后照样能替换，不该拦死。"""
    from utils.replacement import preview_release_replacements

    original = tmp_path / "ABC-123.mp4"
    original.write_bytes(b"old")
    candidate = tmp_path / "ABC-123-new.mkv"
    candidate.write_bytes(b"new")
    row = preview_release_replacements([str(candidate)], [str(original)])[0]
    assert row["status"] == "container_convert"
    assert row["convert_container"] is True, "执行时要据此先转封装"
    assert row["target"] == str(original), "路径仍是原文件，Jellyfin 条目不变"


def test_transcode_without_ffmpeg_says_what_to_do(monkeypatch, tmp_path):
    from utils import replacement

    monkeypatch.setattr("utils.tools.resolve_tool", lambda *a, **k: None)
    with pytest.raises(ValueError) as info:
        replacement.transcode_container(tmp_path / "a.mkv", ".mp4")
    assert "ffmpeg" in str(info.value)


@pytest.mark.skipif(__import__("utils.tools", fromlist=["resolve_tool"]).resolve_tool("ffmpeg") is None,
                    reason="本机没有 ffmpeg")
def test_transcode_really_converts(tmp_path):
    """真转一次：mkv → mp4，产物非空且能读出视频流。"""
    import subprocess

    from utils.replacement import transcode_container
    from utils.tools import resolve_tool

    ffmpeg = resolve_tool("ffmpeg")
    source = tmp_path / "tiny.mkv"
    subprocess.run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=160x120:rate=10",
                    "-c:v", "libx264", str(source)], check=True, capture_output=True)
    out = transcode_container(source, ".mp4")
    try:
        assert Path(out).exists() and Path(out).stat().st_size > 0
        probe = resolve_tool("ffprobe")
        if probe:
            done = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                                   "-show_entries", "stream=codec_name", "-of", "csv=p=0", out],
                                  capture_output=True, text=True)
            assert "h264" in done.stdout.lower()
    finally:
        Path(out).unlink(missing_ok=True)


# ── 封装不同：转封装而不是拒绝 ──────────────────────────


@pytest.mark.skipif(
    __import__("utils.tools", fromlist=["resolve_tool"]).resolve_tool("ffmpeg") is None,
    reason="本机没有 ffmpeg")
def test_container_convert_executes_and_keeps_the_original_path(tmp_path):
    """封装不同 → 自动转封装后替换；路径不变，所以 Jellyfin 条目/进度都还在。"""
    import subprocess

    from utils.replacement import execute_replacements, preview_release_replacements
    from utils.tools import resolve_tool

    ffmpeg = resolve_tool("ffmpeg")
    original, candidate = tmp_path / "ABC-123.mp4", tmp_path / "ABC-123-new.mkv"
    nfo = tmp_path / "ABC-123.nfo"

    def make(path, size):
        subprocess.run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"testsrc=duration=1:size={size}:rate=10",
                        "-c:v", "libx264", str(path)], check=True, capture_output=True)

    make(original, "160x120")
    make(candidate, "320x240")
    nfo.write_text("<movie/>", encoding="utf-8")

    rows = preview_release_replacements([str(candidate)], [str(original)])
    assert rows[0]["status"] == "container_convert"
    logs = []
    stats = execute_replacements(rows, log=logs.append)
    assert stats["ok"] == 1, logs
    assert original.exists(), "替换后必须还是原路径（换了名字 Jellyfin 会当新条目）"
    assert nfo.exists(), "附属文件不能被牵连"
    probe = resolve_tool("ffprobe")
    if probe:
        done = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=width", "-of", "csv=p=0",
                               str(original)], capture_output=True, text=True)
        assert "320" in done.stdout, "内容应换成新版本（宽 320）"
