"""找无码版的筛选与批量操作。

用户诉求：
1. 这里也要能筛"我喜欢的视频"（以前只有关键词 + 检索状态）；
2. 列表要能多选、右键、拖出文件（比如批量拖进 Lada）。

这里守三件事：
- 一个番号对应多个文件时，喜欢/播放怎么聚合（任一 vs 全部）；
- 被筛掉的行不能混进"选中"和"拖出"；
- 没同步 Jellyfin 时，筛选器必须禁用且不生效（不能把整张表隐藏成空）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases.models import normalize_number  # noqa: E402
from ui.pages.censor_replace import NumberRow  # noqa: E402


@pytest.fixture
def page(qapp):
    from config import ToolkitConfig
    from ui.pages.censor_replace import CensorReplacePage

    cfg = ToolkitConfig.load()
    widget = CensorReplacePage(cfg)
    widget.refresh = lambda: None          # 不真的去读库
    widget._rows = {}
    return widget


def _norm(path):
    from utils.jellyfin_review import JellyfinReviewService

    return JellyfinReviewService.normalize_path(path)


def test_favorite_is_any_file(qapp, page):
    """同一番号有多个副本，任一个被标喜欢就算这部片喜欢。"""
    row = NumberRow(number="A-1", paths=[r"C:\l\a1.mp4", r"C:\l\a1-c.mp4"])
    page._review_items = {
        _norm(r"C:\l\a1.mp4"): {"is_favorite": True},
        _norm(r"C:\l\a1-c.mp4"): {"is_favorite": False},
    }
    assert page._favorite_matches(row, "favorite") is True
    assert page._favorite_matches(row, "not_favorite") is False, "有一个喜欢就不算未喜欢"
    assert page._favorite_matches(row, "unknown") is False


def test_favorite_not_favorite_requires_all_false(qapp, page):
    row = NumberRow(number="A-2", paths=[r"C:\l\a2.mp4", r"C:\l\a2b.mp4"])
    page._review_items = {
        _norm(r"C:\l\a2.mp4"): {"is_favorite": False},
        _norm(r"C:\l\a2b.mp4"): {"is_favorite": False},
    }
    assert page._favorite_matches(row, "not_favorite") is True


def test_favorite_unknown_when_unmatched(qapp, page):
    row = NumberRow(number="A-3", paths=[r"C:\l\unknown.mp4"])
    page._review_items = {}
    assert page._favorite_matches(row, "unknown") is True


def test_playback_semantics(qapp, page):
    row = NumberRow(number="B-1", paths=[r"C:\l\b1.mp4", r"C:\l\b1b.mp4"])
    page._review_items = {
        _norm(r"C:\l\b1.mp4"): {"played": True, "has_progress": False},
        _norm(r"C:\l\b1b.mp4"): {"played": True, "has_progress": False},
    }
    assert page._playback_matches(row, "played") is True
    assert page._playback_matches(row, "unplayed") is False

    page._review_items = {
        _norm(r"C:\l\b1.mp4"): {"played": False, "has_progress": True},
        _norm(r"C:\l\b1b.mp4"): {"played": False, "has_progress": False},
    }
    assert page._playback_matches(row, "progress") is True, "有一个在看就算播放中"
    assert page._playback_matches(row, "unplayed") is False, "有一个在看就不算未播放"


def test_selected_rows_skips_hidden(qapp, page):
    """被筛掉的行不能算进选中 —— 否则批量操作会作用到看不见的东西上。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem

    rows = {f"X{i}": NumberRow(number=f"N-{i}", paths=[rf"C:\l\n{i}.mp4"]) for i in range(3)}
    page._rows = rows
    page.table.setRowCount(3)
    for index, (key, row) in enumerate(rows.items()):
        item = QTableWidgetItem(row.number)
        item.setData(Qt.UserRole, key)
        page.table.setItem(index, 0, item)
    page.table.hideRow(1)
    page.table.selectAll()

    picked = page._selected_rows()
    assert len(picked) == 2, picked
    assert all(r.number != "N-1" for r in picked)

    paths = page._drag_paths()
    assert len(paths) == 2, "拖出的文件也要排除被筛掉的行"


def test_drag_paths_dedupes(qapp, page):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem

    rows = {
        "A": NumberRow(number="A-1", paths=[r"C:\l\same.mp4"]),
        "B": NumberRow(number="A-1", paths=[r"C:\l\same.mp4"]),
    }
    page._rows = rows
    page.table.setRowCount(2)
    for index, (key, row) in enumerate(rows.items()):
        item = QTableWidgetItem(row.number)
        item.setData(Qt.UserRole, key)
        page.table.setItem(index, 0, item)
    page.table.selectAll()
    assert page._drag_paths() == [r"C:\l\same.mp4"], "同一文件只拖一次"


def test_disabled_filters_do_not_hide_everything(qapp, page):
    """没同步 Jellyfin 时下拉是禁用的；此时必须当作"全部"，不能把表清空。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem

    rows = {f"K{i}": NumberRow(number=f"M-{i}", paths=[rf"C:\l\m{i}.mp4"]) for i in range(4)}
    page._rows = rows
    page.table.setRowCount(4)
    for index, (key, row) in enumerate(rows.items()):
        item = QTableWidgetItem(row.number)
        item.setData(Qt.UserRole, key)
        page.table.setItem(index, 0, item)

    page.combo_favorite.setEnabled(False)
    page.combo_playback.setEnabled(False)
    page._review_items = {}
    page._filter()
    visible = sum(1 for i in range(page.table.rowCount()) if not page.table.isRowHidden(i))
    assert visible == 4
