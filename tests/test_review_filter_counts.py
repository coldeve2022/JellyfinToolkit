"""Counts share the table predicates but ignore the selected view dimension."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy

from censor.fusion import fuse
from censor.models import VideoVerdict, Evidence, EvidenceSource, CensorState, ProcessingStatus
from censor.review_route import WorkflowView
from config import ToolkitConfig


@pytest.fixture
def page(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    widget = CensorshipPage(ToolkitConfig(censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    widget._cancel_startup()
    widget.library_review.shutdown()
    qtbot.addWidget(widget)
    specs = [('a.mp4', 'todo', True, False), ('b.mp4', 'todo', False, False),
             ('c.mp4', 'clean', True, True), ('d.mp4', 'low', True, True),
             ('e uncensored.mp4', 'deferred', False, False),
             ('f.mp4', 'failed', None, None), ('g.mp4', 'positive', False, True)]
    widget.fixture_paths = {}
    for name, kind, favorite, played in specs:
        path = 'C:/synthetic/' + name
        widget.fixture_paths[name] = path
        evidence = []
        if kind in ('clean', 'positive'):
            evidence = [Evidence(EvidenceSource.MANUAL,
                CensorState.UNCENSORED if kind == 'clean' else CensorState.CENSORED, 1.)]
        elif kind == 'low':
            evidence = [Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0., review_required=False)]
        row = VideoVerdict(path=path, verdict=fuse(evidence))
        if kind == 'failed':
            row.status = ProcessingStatus.PARTIAL
        widget.library_review.items[widget.library_review.normalize_path(path)] = {
            'is_favorite': favorite, 'played': played, 'tags': ['collection'] if name == 'c.mp4' else []}
        widget._on_item(row)
    yield widget
    widget.shutdown()
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(widget), timeout=5000)


def choose(control, value):
    control.setCurrentIndex(control.findData(value))


def count_text(page, view):
    return page.combo_filter.itemText(page.combo_filter.findData(view.value))


def visible_paths(page):
    return {page.table.item(row, 0).data(Qt.UserRole) for row in range(page.table.rowCount())
            if not page.table.isRowHidden(row)}


def test_favorite_counts_update_and_keep_real_view_totals(page):
    assert count_text(page, WorkflowView.ALL) == '全部记录 (7)'
    choose(page.combo_favorite, 'favorite')
    assert count_text(page, WorkflowView.ALL) == '全部记录 (3 / 7)'
    assert count_text(page, WorkflowView.TODO) == '待我处理 (1 / 3)'
    assert count_text(page, WorkflowView.DONE) == '已确认 (1 / 2)'
    assert count_text(page, WorkflowView.PARKED) == '暂不处理 (1 / 2)'
    assert len(visible_paths(page)) == 3


def test_view_switch_does_not_restrict_other_view_counts(page):
    choose(page.combo_favorite, 'favorite')
    before = [page.combo_filter.itemText(i) for i in range(page.combo_filter.count())]
    choose(page.combo_filter, WorkflowView.PARKED.value)
    assert visible_paths(page) == {page.fixture_paths['d.mp4']}
    assert [page.combo_filter.itemText(i) for i in range(page.combo_filter.count())] == before


@pytest.mark.parametrize('control,value,expected', [
    ('combo_favorite', 'unknown', 1), ('combo_favorite', 'not_favorite', 3),
    ('combo_playback', 'played', 3), ('combo_playback', 'unknown', 1),
    ('combo_conclusion', CensorState.UNCENSORED.value, 1)])
def test_each_scope_filter_updates_the_all_view_count(page, control, value, expected):
    choose(getattr(page, control), value)
    assert count_text(page, WorkflowView.ALL) == f'全部记录 ({expected} / 7)'
    assert len(visible_paths(page)) == expected


def test_combined_playback_conclusion_and_metadata_search_counts(page):
    choose(page.combo_favorite, 'favorite')
    choose(page.combo_playback, 'played')
    assert count_text(page, WorkflowView.ALL) == '全部记录 (2 / 7)'
    choose(page.combo_conclusion, CensorState.UNCENSORED.value)
    page.search.setText('collection')
    assert visible_paths(page) == {page.fixture_paths['c.mp4']}
    assert count_text(page, WorkflowView.ALL) == '全部记录 (1 / 7)'
    assert count_text(page, WorkflowView.DONE) == '已确认 (1 / 2)'
    page.search.setText('no-such-file')
    assert count_text(page, WorkflowView.ALL) == '全部记录 (0 / 7)'
    assert not visible_paths(page)


def test_clearing_filters_restores_dynamic_totals_without_changing_view(page):
    choose(page.combo_filter, WorkflowView.TODO.value)
    spy = QSignalSpy(page.combo_filter.currentIndexChanged)
    choose(page.combo_favorite, 'favorite')
    page.search.setText('no-such-file')
    choose(page.combo_favorite, 'all')
    page.search.clear()
    assert count_text(page, WorkflowView.ALL) == '全部记录 (7)'
    assert count_text(page, WorkflowView.TODO) == '待我处理 (3)'
    assert page.combo_filter.currentData() == WorkflowView.TODO.value
    assert spy.count() == 0


def test_library_snapshot_refresh_recalculates_counts(page):
    choose(page.combo_favorite, 'favorite')
    items = dict(page.library_review.items)
    key = page.library_review.normalize_path(page.fixture_paths['a.mp4'])
    items[key] = dict(items[key], is_favorite=False)
    page.library_review.accept_snapshot(dict(context_key=page.library_review.service().context_key,
        summary={'ok': True, 'count': 7}, items=items))
    assert count_text(page, WorkflowView.ALL) == '全部记录 (2 / 7)'
    assert count_text(page, WorkflowView.TODO) == '待我处理 (0 / 3)'


def test_result_change_updates_filtered_and_global_totals(page):
    choose(page.combo_favorite, 'favorite')
    path = page.fixture_paths['a.mp4']
    page._on_item(VideoVerdict(path=path,
        verdict=fuse([Evidence(EvidenceSource.MANUAL, CensorState.UNCENSORED, 1.)])))
    assert count_text(page, WorkflowView.TODO) == '待我处理 (0 / 2)'
    assert count_text(page, WorkflowView.DONE) == '已确认 (2 / 3)'
    assert count_text(page, WorkflowView.ALL) == '全部记录 (3 / 7)'


def test_footer_distinguishes_loaded_scope_and_visible_counts(page):
    choose(page.combo_favorite, 'favorite')
    choose(page.combo_filter, WorkflowView.PARKED.value)
    text = page._stats_label.text()
    assert '列表 7' in text
    assert '筛选匹配 3' in text
    assert '当前显示 1' in text
