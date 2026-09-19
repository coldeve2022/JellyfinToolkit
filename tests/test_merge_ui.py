import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from config import ToolkitConfig
from ui.pages.merge import MergePage
from utils.merge import EpisodeGroup, MergePlan, PartFile, PartProbe


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    yield app


def _probe(duration=100.0, size=1024, **kw):
    vals = dict(ok=True, duration=duration, width=1280, height=720, fps=30.0,
                pixel_format="yuv420p", video_codec="h264", audio_codec="aac",
                sample_rate=44100, channels=2, video_stream_index=0,
                audio_stream_index=1, has_attached_pic=False, size=size,
                container="mp4", video_time_base="1/90000")
    vals.update(kw)
    return PartProbe(**vals)


def _part(path, index, kind="cd", number="ABC-123", title=""):
    return PartFile(path=path, name=Path(path).name, stem=Path(path).stem,
                    number=number, part_index=index, part_kind=kind,
                    probe=_probe(), title=title)


def _page(monkeypatch, tmp_path):
    monkeypatch.setattr("ui.pages.merge.DATA_DIR", tmp_path)
    cfg = ToolkitConfig()
    page = MergePage(cfg)
    yield page
    page.shutdown()


def test_merge_page_filter_preserves_checks_and_conflict_delete(app, monkeypatch, tmp_path):
    gen = _page(monkeypatch, tmp_path)
    page = next(gen)
    group_ready = EpisodeGroup(number="READY001", status="ready", parts=[
        _part(str(tmp_path / "READY001-cd1.mp4"), 1, number="READY001", title="Ready Title"),
        _part(str(tmp_path / "READY001-cd2.mp4"), 2, number="READY001"),
    ])
    group_conflict = EpisodeGroup(number="CONFLICT001", status="conflict", parts=[
        _part(str(tmp_path / "CONFLICT001-cd1.mp4"), 1, number="CONFLICT001"),
        _part(str(tmp_path / "CONFLICT001-cd2.mp4"), 1, number="CONFLICT001"),
    ])
    page._plan = MergePlan(groups=[group_ready, group_conflict], unrecognized=[], stats={})
    page._group_map = {"READY001": group_ready, "CONFLICT001": group_conflict}
    page._current_filter = None
    page._populate_tree()

    ready_top = page._group_items["READY001"]
    assert ready_top.text(0).startswith("READY001 — Ready Title")
    assert ready_top.child(0).checkState(0) == Qt.Checked
    ready_top.child(0).setCheckState(0, Qt.Unchecked)
    ready_top.child(1).setCheckState(0, Qt.Checked)

    page.combo_filter.setCurrentIndex(page.combo_filter.findData("conflict"))
    assert set(page._group_items) == {"CONFLICT001"}
    page.combo_filter.setCurrentIndex(0)
    ready_top = page._group_items["READY001"]
    assert ready_top.child(0).checkState(0) == Qt.Unchecked
    assert ready_top.child(1).checkState(0) == Qt.Checked

    conflict_top = page._group_items["CONFLICT001"]
    conflict_top.child(0).setCheckState(0, Qt.Checked)
    deleted = page._checked_delete_items()
    assert any(path.endswith("CONFLICT001-cd1.mp4") for _, path in deleted)
    next(gen, None)


def test_merge_page_parent_checkbox_toggles_all_children(app, monkeypatch, tmp_path):
    gen = _page(monkeypatch, tmp_path)
    page = next(gen)
    group = EpisodeGroup(number="ABC-123", status="ready", parts=[
        _part(str(tmp_path / "ABC-123-cd1.mp4"), 1),
        _part(str(tmp_path / "ABC-123-cd2.mp4"), 2),
        _part(str(tmp_path / "ABC-123-cd3.mp4"), 3),
    ])
    page._plan = MergePlan(groups=[group], unrecognized=[], stats={})
    page._group_map = {"ABC-123": group}
    page._populate_tree()
    top = page._group_items["ABC-123"]
    assert top.checkState(0) == Qt.Checked
    page._set_group_checked(group, False)
    assert all(top.child(i).checkState(0) == Qt.Unchecked for i in range(3))
    assert top.checkState(0) == Qt.Unchecked
    page._set_group_checked(group, True)
    assert all(top.child(i).checkState(0) == Qt.Checked for i in range(3))
    next(gen, None)
