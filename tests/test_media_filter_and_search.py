"""附属文件过滤、工具搜索、以及"清单梳理"语义的测试。

这三个都是**用户实际报出来的问题**，对应三条守则：

1. 预告片 / 主题视频不是正片 —— 不该生成字幕，也不该出现在缺字幕清单里；
   但**不能误伤**：《Trailer Park Boys》这种片名必须仍然算正片。
2. 自动检测必须能找到工具 —— 用户把 exe 解压在带版本号与平台后缀的目录里
   （``faster_whisper_transwithai_windows_cu122-chickenrice``），
   只认固定目录名的实现必然失败，用户看到的是"点了只弹一个窗口"。
3. 「挑出库里缺字幕的视频」的语义是**梳理清单**（移走已有字幕的），
   而不是往清单上继续堆 —— 否则表格里始终是那上万个文件。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import tool_search  # noqa: E402
from utils.library import is_junk_attachment_path  # noqa: E402


# ── 1. 附属文件判定 ─────────────────────────────────────────

@pytest.mark.parametrize("path", [
    r"D:\m\ABC-001\trailers\ABC-001-trailer.mp4",
    r"D:\m\ABC-001\trailer.mp4",
    r"D:\m\ABC-001\ABC-001-trailer.mp4",
    r"D:\m\ABC-001\trailers\任意名.mp4",           # 目录本身就是预告片目录
    r"D:\m\ABC-001\theme.mp4",
    r"D:\m\ABC-001\theme_video.mp4",
    r"D:\m\ABC-001\ABC-001-theme_video.mp4",       # 带前缀的写法
    r"D:\m\ABC-001\theme-video-1.mp4",
    r"D:\m\ABC-001\backdrops\theme.mp4",
    r"D:\m\ABC-001\extras\behind the scenes.mp4",
    r"D:\m\ABC-001\sample.mkv",
    r"D:\m\ABC-001\fanart1.jpg",
    r"D:\m\ABC-001\poster.jpg",
])
def test_auxiliary_detected(path):
    assert is_junk_attachment_path(path) is True


@pytest.mark.parametrize("path", [
    r"D:\m\ABC-001\ABC-001.mp4",                   # 正片
    r"D:\m\ABC-001\ABC-001-CD1.mp4",               # 分集
    r"D:\m\Trailer Park Boys S01E01.mp4",          # 片名里含 trailer，但不是预告片
    r"D:\m\The Theme of Love.mp4",
    r"D:\m\sample-this-movie\movie.mp4",           # 目录名含 sample，但文件是正片
    r"D:\m\ABC-001\subs\ABC-001.srt",
])
def test_normal_files_not_treated_as_auxiliary(path):
    assert is_junk_attachment_path(path) is False


def test_auxiliary_detection_handles_posix_style_paths():
    """库里的路径可能是另一个平台写的（反斜杠/正斜杠混用）。"""
    assert is_junk_attachment_path("/media/m/ABC-001/trailers/x.mp4") is True
    assert is_junk_attachment_path("/media/m/ABC-001/ABC-001.mp4") is False


# ── 2. 工具搜索 ─────────────────────────────────────────────

def test_exe_in_dir_accepts_file_and_nested_dir(tmp_path):
    """用户可能填 exe、填它所在目录、或填解压后的顶层目录 —— 三种都要认。"""
    exe = tmp_path / "infer.exe"
    exe.write_bytes(b"")
    assert tool_search.exe_in_dir(exe, ("infer.exe",)) == exe
    assert tool_search.exe_in_dir(tmp_path, ("infer.exe",)) == exe
    # 解压多套一层
    outer = tmp_path / "解压后"
    (outer / "faster-whisper-x").mkdir(parents=True)
    nested = outer / "faster-whisper-x" / "infer.exe"
    nested.write_bytes(b"")
    assert tool_search.exe_in_dir(outer, ("infer.exe",)) == nested


def test_find_executable_prefers_configured(tmp_path):
    exe = tmp_path / "infer.exe"
    exe.write_bytes(b"")
    r = tool_search.find_executable(("infer.exe",), ("whisper",),
                                    configured=str(exe))
    assert r["path"] == exe and "设置" in r["source"]


def test_find_executable_finds_by_keyword_in_subdir(tmp_path, monkeypatch):
    """**关键守则**：目录名带版本号/平台后缀时也要能找到。

    用户的真实目录是 ``E:\\faster_whisper_transwithai_windows_cu122-chickenrice``，
    按固定目录名去猜一定落空。
    """
    fake_drive = tmp_path / "E"
    target = fake_drive / "faster_whisper_transwithai_windows_cu122-chickenrice"
    target.mkdir(parents=True)
    exe = target / "infer.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(tool_search, "drives", lambda: [fake_drive])

    r = tool_search.find_executable(("infer.exe",), ("whisper", "transwithai"),
                                    time_budget=5)
    assert r["path"] == exe
    assert "名称" in r["source"] or "faster_whisper" in r["source"]


def test_find_executable_reports_process_when_not_found(tmp_path, monkeypatch):
    """找不到时也要给出过程数据（界面据此说明"搜了哪儿"），而不是静默失败。"""
    fake_drive = tmp_path / "D"
    (fake_drive / "一些无关目录").mkdir(parents=True)
    monkeypatch.setattr(tool_search, "drives", lambda: [fake_drive])

    r = tool_search.find_executable(("infer.exe",), ("whisper",), time_budget=5)
    assert r["path"] is None
    assert r["scanned"] >= 1
    assert r["elapsed"] >= 0


def test_find_executable_skips_system_dirs(tmp_path, monkeypatch):
    """系统目录不该被翻（慢且没意义）。"""
    fake_drive = tmp_path / "C"
    (fake_drive / "Windows" / "System32").mkdir(parents=True)
    (fake_drive / "Windows" / "System32" / "infer.exe").write_bytes(b"")
    monkeypatch.setattr(tool_search, "drives", lambda: [fake_drive])
    r = tool_search.find_executable(("infer.exe",), ("whisper",), time_budget=5)
    assert r["path"] is None


# ── 3. "清单梳理"语义 ───────────────────────────────────────

def test_scan_filters_out_items_that_already_have_subtitles(qapp, tmp_path):
    """**用户报的核心问题**：点「挑出库里缺字幕的视频」后，表格里应该只剩缺字幕的。

    按用户的实际操作顺序还原：
    1. 先「添加目录」——那时还没有字幕，3 个视频都进了清单；
    2. 之后其中 2 个有了字幕（Jellyfin 刮削/别人补的）；
    3. 这时点「挑出库里缺字幕的视频」，清单里应只剩真正缺字幕的那 1 个。
    """
    from ui.pages.subtitle_gen import SubtitleGenPage

    a = tmp_path / "A-001.mp4"
    b = tmp_path / "A-002.mp4"
    without = tmp_path / "A-003.mp4"
    for f in (a, b, without):
        f.write_bytes(b"x")

    page = SubtitleGenPage(_cfg())
    page._add_paths([tmp_path], quiet=True)
    assert page.list_files.count() == 3, "此时都还没字幕，应全部加入"

    # 其中两个后来有了字幕
    (tmp_path / "A-001.srt").write_text("1\n", encoding="utf-8")
    (tmp_path / "A-002.srt").write_text("1\n", encoding="utf-8")

    # 媒体库扫描的结果里只有 A-003 缺字幕
    page._on_scan_done([str(without)])

    names = sorted(page.list_files.item(i).text() for i in range(page.list_files.count()))
    assert names == [str(without)], names


def test_add_paths_skips_auxiliary_videos(qapp, tmp_path):
    """添加目录时，预告片 / 主题视频不该进清单，且要如实汇报跳过数。"""
    from ui.pages.subtitle_gen import SubtitleGenPage

    (tmp_path / "A-001.mp4").write_bytes(b"x")
    (tmp_path / "theme.mp4").write_bytes(b"x")
    (tmp_path / "trailers").mkdir()
    (tmp_path / "trailers" / "A-001-trailer.mp4").write_bytes(b"x")

    page = SubtitleGenPage(_cfg())
    stats = page._add_paths([tmp_path], quiet=True)
    assert stats["added"] == 1
    assert stats["aux"] == 2
    assert page.list_files.count() == 1


def test_add_paths_skips_videos_that_already_have_subtitles(qapp, tmp_path):
    """「只加入缺字幕的」勾选时（默认），已有字幕的不进清单。"""
    from ui.pages.subtitle_gen import SubtitleGenPage

    (tmp_path / "A-001.mp4").write_bytes(b"x")
    (tmp_path / "A-001.srt").write_text("1\n", encoding="utf-8")
    (tmp_path / "A-002.mp4").write_bytes(b"x")

    page = SubtitleGenPage(_cfg())
    assert page.chk_only_missing.isChecked(), "默认应勾选"
    stats = page._add_paths([tmp_path], quiet=True)
    assert stats["added"] == 1 and stats["has_sub"] == 1

    # 取消勾选后应全部加入（用户想重做已有字幕时）
    page.chk_only_missing.setChecked(False)
    page2 = SubtitleGenPage(_cfg())
    page2.chk_only_missing.setChecked(False)
    stats2 = page2._add_paths([tmp_path], quiet=True)
    assert stats2["added"] == 2


def test_scan_keeps_auxiliary_out_of_filtering(qapp, tmp_path):
    """扫描结果里若混进预告片，也不该被加进清单。"""
    from ui.pages.subtitle_gen import SubtitleGenPage

    trailer = tmp_path / "trailers" / "A-001-trailer.mp4"
    trailer.parent.mkdir(parents=True)
    trailer.write_bytes(b"x")

    page = SubtitleGenPage(_cfg())
    page._on_scan_done([str(trailer)])
    assert page.list_files.count() == 0


def _cfg():
    """测试用配置：输出目录留空（就地判断字幕），避免依赖外部环境。"""
    from config import ToolkitConfig

    cfg = ToolkitConfig()
    cfg.whisper_output_dir = ""
    cfg.video_extensions = [".mp4", ".mkv"]
    return cfg
