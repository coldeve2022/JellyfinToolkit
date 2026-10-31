"""Generic review predicates, controls and snapshot changes share one scope."""
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt


@pytest.mark.parametrize('item,state', [({}, 'unknown'), ({'played': None, 'playback_ticks': 9}, 'unknown'),
    ({'played': False}, 'unplayed'), ({'played': False, 'playback_ticks': 9}, 'progress'),
    ({'played': True, 'has_progress': True}, 'played')])
def test_playback_unknown_is_not_unplayed_or_in_progress(item, state):
    from utils.media_review_query import playback_state
    assert playback_state(item) == state


def test_query_intersects_terms_user_state_and_conclusion():
    from utils.media_review_query import ReviewQuery
    query = ReviewQuery.from_values('SYNTHETIC keep', 'unknown', 'favorite', 'progress')
    metadata = dict(name='Synthetic', tags=['Keep'], is_favorite=True, played=False, playback_ticks=9)
    assert query.matches('/media/one.mp4', '', 'unknown', '', metadata)
    assert not query.matches('/media/one.mp4', '', 'unknown', '', dict(metadata, played=None))
    assert not query.matches('/media/one.mp4', '', 'other', '', metadata)


@pytest.fixture
def page(qapp, qtbot):
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from censor.models import VideoVerdict
    widget = CensorshipPage(ToolkitConfig(jellyfin_user_id='viewer', library_source='api',
        jellyfin_server='http://fixture.invalid', censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    widget._cancel_startup()
    widget.library_review.timer.stop()
    qtbot.addWidget(widget)
    for name in ('one', 'two', 'three'):
        widget._on_item(VideoVerdict(path=f'C:/synthetic/{name}.mp4'))
    yield widget
    widget.shutdown()


def choose(control, value):
    control.setCurrentIndex(control.findData(value))


def test_clear_scope_preserves_view_order_and_saved_results(page, qtbot):
    from censor.review_route import WorkflowView
    choose(page.combo_filter, WorkflowView.ALL.value)
    choose(page.combo_library_sort, 'favorite_first')
    choose(page.combo_favorite, 'favorite')
    choose(page.combo_playback, 'played')
    choose(page.combo_conclusion, 'uncensored')
    page.search.setText('missing')
    before = deepcopy(page._results)
    qtbot.mouseClick(page.btn_clear_filters, Qt.LeftButton)
    assert page.search.text() == ''
    assert page.combo_conclusion.currentData() == ''
    assert page.combo_favorite.currentData() == page.combo_playback.currentData() == 'all'
    assert page.combo_filter.currentData() == WorkflowView.ALL.value
    assert page.combo_library_sort.currentData() == 'favorite_first'
    assert all(not page.table.isRowHidden(i) for i in range(3))
    assert page._results == before


def test_manual_header_sort_exits_preset_without_reverting_user_order(page):
    choose(page.combo_library_sort, 'favorite_first')
    page.table.sortItems(0, Qt.AscendingOrder)
    assert page.combo_library_sort.currentData() == 'normal'
    assert page.table.horizontalHeader().sortIndicatorSection() == 0
    assert [page.table.item(i, 0).text() for i in range(3)] == ['one.mp4', 'three.mp4', 'two.mp4']


def test_progress_sort_uses_state_rank_and_preserves_selection(page):
    controller = page.library_review
    controller.items = {controller.normalize_path('C:/synthetic/one.mp4'): {'played': True},
        controller.normalize_path('C:/synthetic/two.mp4'): {'played': False, 'has_progress': True},
        controller.normalize_path('C:/synthetic/three.mp4'): {'played': False}}
    controller.repaint()
    row = page._table_presenter.row_for('C:/synthetic/one.mp4')
    page.table.selectRow(row)
    assert page.combo_library_sort.findData('progress_first') >= 0
    choose(page.combo_library_sort, 'progress_first')
    assert page.table.item(0, 0).text() == 'two.mp4'
    assert page._selected_visible_paths() == ['C:/synthetic/one.mp4']


def test_snapshot_read_reason_is_visible_in_controller(page):
    assert '尚无' in page.library_review.last_status
    assert page.library_review.snapshot.status == 'missing'


def test_snapshot_event_updates_other_generic_controller_but_not_other_context(page, qtbot):
    from ui.pages.censorship import CensorshipPage
    from utils.user_state_store import UserStateStore
    other = CensorshipPage(deepcopy(page.cfg))
    other._cancel_startup()
    other.library_review.timer.stop()
    qtbot.addWidget(other)
    service = page.library_review.service()
    path = 'C:/synthetic/one.mp4'
    data = {service.normalize_path(path): dict(path=path, item_id='one', remote_path='/media/one.mp4',
        user_id='viewer', mapped=True, is_favorite=True, played=False, has_progress=False,
        playback_ticks=0, play_count=0, tags=[], genres=[])}
    try:
        UserStateStore(service._db_path).publish(service._snapshot_key(), data)
        page.library_review.accept_snapshot(dict(context_key=service.context_key, items=data, summary={'ok': True}))
        assert other.library_review.item(path)['is_favorite'] is True
        other.cfg.jellyfin_user_id = 'other'
        other.library_review.reload()
        page.library_review.accept_snapshot(dict(context_key=service.context_key, items=data, summary={'ok': True}))
        assert not other.library_review.items
    finally:
        other.shutdown()


def test_resolved_source_is_retained_only_for_same_profile_and_context(page):
    page.cfg.library_source = 'auto'
    page.cfg.jellyfin_db_path = ''
    controller = page.library_review
    original = controller.service()
    original._resolved_identity = 'db:synthetic'
    assert controller.service() is original
    assert controller.service()._snapshot_key() is not None
    page.cfg.jellyfin_user_id = 'another'
    assert controller.service() is not original
    assert controller.service()._snapshot_key() is None
