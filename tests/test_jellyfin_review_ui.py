"""Library preferences intersect ordinary review filters and table shortcuts."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from censor.models import VideoVerdict, Verdict, CensorState
from censor.review_route import WorkflowView
from config import ToolkitConfig


def make_page(qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(release_auto_lookup=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    page._on_item(VideoVerdict(path='C:/library/one.mp4', verdict=Verdict(state=CensorState.CENSORED)))
    page._on_item(VideoVerdict(path='C:/library/two.mp4', verdict=Verdict(state=CensorState.UNCENSORED)))
    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.ALL.value))
    return page


def row_for(page, path):
    return next(row for row in range(page.table.rowCount())
                if page.table.item(row, 0).data(Qt.UserRole) == path)


def test_favorites_combine_with_conclusion_search_and_unknown_scope(qapp, qtbot):
    page = make_page(qtbot)
    service = page.library_review
    service.items = {service.normalize_path('C:/library/one.mp4'):
                     {'is_favorite': True, 'played': False, 'has_progress': True}}
    page.combo_favorite.setCurrentIndex(page.combo_favorite.findData('favorite'))
    assert not page.table.isRowHidden(row_for(page, 'C:/library/one.mp4'))
    assert page.table.isRowHidden(row_for(page, 'C:/library/two.mp4'))
    page.combo_conclusion.setCurrentIndex(page.combo_conclusion.findData('uncensored'))
    assert all(page.table.isRowHidden(row) for row in range(2))
    page.combo_conclusion.setCurrentIndex(0)
    page.search.setText('two')
    assert all(page.table.isRowHidden(row) for row in range(2))
    page.search.clear()
    page.combo_favorite.setCurrentIndex(page.combo_favorite.findData('not_favorite'))
    assert all(page.table.isRowHidden(row) for row in range(2))
    page.combo_favorite.setCurrentIndex(page.combo_favorite.findData('unknown'))
    assert not page.table.isRowHidden(row_for(page, 'C:/library/two.mp4'))
    page.shutdown()


def test_stale_user_context_result_is_not_accepted(qapp, qtbot):
    page = make_page(qtbot)
    old_context = page.library_review.context_key
    page.cfg.jellyfin_user_id = 'different-user'
    page.library_review.reload()
    page.library_review.accept_snapshot({'context_key': old_context, 'items': {'wrong': {'is_favorite': True}},
                                         'summary': {'ok': True}})
    assert 'wrong' not in page.library_review.items
    page.shutdown()


def test_selected_visible_rows_handoff_to_release_lookup(qapp, qtbot):
    """检查页只把选中项送去「找无码版」核查，不再直接跳替换。

    流程是"先确认有没有无码版，再决定替不替代"；替换入口在找无码页。
    """
    from ui.services import events
    page = make_page(qtbot)
    page.show()
    qapp.processEvents()
    page.search.setText('one')
    page.table.selectAll()
    received = []
    events.release_lookup_requested.connect(received.append)
    try:
        page._send_to_lookup()
        assert received == [['C:/library/one.mp4']]
    finally:
        events.release_lookup_requested.disconnect(received.append)
    page.shutdown()


def test_table_copy_and_search_shortcuts_work_without_editing_records(qapp, qtbot):
    page = make_page(qtbot)
    page.show()
    page.activateWindow()
    qapp.processEvents()
    page.table.setFocus()
    qapp.processEvents()
    page.table.selectAll()
    qtbot.keyClick(page.table, Qt.Key_C, modifier=Qt.ControlModifier)
    assert 'one.mp4' in QApplication.clipboard().text()
    assert 'two.mp4' in QApplication.clipboard().text()
    qtbot.keyClick(page.table, Qt.Key_F, modifier=Qt.ControlModifier)
    assert page.search.hasFocus()
    page.shutdown()


def test_review_shortcut_does_not_confirm_while_typing(qapp, qtbot, monkeypatch):
    page = make_page(qtbot)
    labels = []
    monkeypatch.setattr(page, '_label', labels.append)
    page.show()
    page.activateWindow()
    qapp.processEvents()
    page.search.setFocus()
    qapp.processEvents()
    qtbot.keyClick(page.search, Qt.Key_1, modifier=Qt.AltModifier)
    assert labels == []
    page.table.setFocus()
    qapp.processEvents()
    qtbot.keyClick(page.table, Qt.Key_1, modifier=Qt.AltModifier)
    assert labels == [CensorState.CENSORED]
    page.shutdown()


def test_user_list_result_cannot_switch_the_new_source_account(qapp, qtbot, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    page = make_page(qtbot)
    old_context = page.library_review.context_key
    page.cfg.jellyfin_server = 'http://different.invalid'
    def forbidden(*args, **kwargs):
        raise AssertionError('Stale source must not open an account chooser')
    monkeypatch.setattr(QInputDialog, 'getItem', forbidden)
    page.library_review._choose_loaded_user({'context_key': old_context,
        'users': [{'id': 'foreign-user', 'name': 'other'}]})
    assert not page.cfg.jellyfin_user_id
    page.shutdown()


def test_filtering_current_row_preserves_other_visible_selected_rows(qapp, qtbot):
    page = make_page(qtbot)
    page.show()
    qapp.processEvents()
    page.table.selectAll()
    page._current = page._results['C:/library/one.mp4']
    page.library_review.items = {page.library_review.normalize_path('C:/library/two.mp4'):
                                {'is_favorite': True}}
    page.combo_favorite.setCurrentIndex(page.combo_favorite.findData('favorite'))
    assert page._selected_visible_paths() == ['C:/library/two.mp4']
    assert page._current and page._current.path == 'C:/library/two.mp4'
    page.shutdown()


def test_favorite_shortcut_loads_confirmed_history_not_just_pending_rows(qapp, qtbot):
    page = make_page(qtbot)
    path = 'C:/library/saved.mp4'
    page._service.store.put(VideoVerdict(path=path, verdict=Verdict(state=CensorState.CENSORED)))
    page.library_review.items = {page.library_review.normalize_path(path): {'is_favorite': True}}
    page._show_favorite_censored()
    qtbot.waitUntil(lambda: path in page._results, timeout=5000)
    qtbot.waitUntil(lambda: not page._history_worker.isRunning())
    assert not page.table.isRowHidden(row_for(page, path))
    page.shutdown()
