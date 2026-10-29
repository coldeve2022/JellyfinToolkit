"""Visible ordering, explicit selection and consistent review entry points."""
import pytest
from PySide6.QtCore import Qt, QItemSelectionModel

from censor import CensorOptions, CensorState
from censor.fusion import fuse
from censor.models import Evidence, EvidenceSource
from censor.review_route import WorkflowView
from config import ToolkitConfig


@pytest.fixture
def review(qapp, qtbot, tmp_path, monkeypatch):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    monkeypatch.setattr(page.metadata_controller, 'schedule', lambda: None)
    page.workspace.state['auto_next'] = True
    def add(name, *, manual=False, low_risk=False):
        path = tmp_path / (name + '.mp4')
        path.write_bytes(b'ordinary synthetic fixture')
        result = page._service.probe(path, CensorOptions(detect_visual=False))
        if manual:
            result = page._service.label(path, CensorState.UNCENSORED, base=result)
        elif low_risk:
            result.verdict = fuse([Evidence(EvidenceSource.VISUAL,
                CensorState.UNKNOWN, 0., review_required=False)])
        page._on_item(result)
        return str(path)
    page.add_fixture = add
    yield page
    page.shutdown()
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(page), timeout=5000)


def paths(page, *, visible=False):
    return [page.table.item(row, 0).data(Qt.UserRole)
            for row in range(page.table.rowCount())
            if not visible or not page.table.isRowHidden(row)]


def select(page, path):
    page.table.selectRow(page._table_presenter.row_for(path))
    assert page._current.path == path


def test_header_sort_keeps_filter_attached_to_video(review):
    a = review.add_fixture('a')
    review.add_fixture('b', manual=True)
    c = review.add_fixture('c')
    review.add_fixture('d', manual=True)
    review.combo_filter.setCurrentIndex(review.combo_filter.findData(WorkflowView.TODO.value))
    assert set(paths(review, visible=True)) == {a, c}
    review.table.sortItems(0, Qt.DescendingOrder)
    assert paths(review, visible=True) == [c, a]


def test_clear_selection_does_not_allow_button_to_reconfirm_old_focus(review):
    a = review.add_fixture('a')
    review.add_fixture('b')
    select(review, a)
    review.table.clearSelection()
    assert review._current is None
    review.btn_uncensored.click()
    assert review._service.store.get_label(a, file_identity=review._results[a].file_identity) is None
    assert review._current is None


@pytest.mark.parametrize('entry', ['mouse', 'shortcut'])
def test_multiple_selection_uses_same_explicit_batch_confirmation(review, monkeypatch, entry):
    a, b = review.add_fixture('a'), review.add_fixture('b')
    select(review, a)
    row = review._table_presenter.row_for(b)
    review.table.selectionModel().select(review.table.model().index(row, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    called = []
    monkeypatch.setattr(review, '_batch_label', lambda state: called.append(('batch', state)))
    monkeypatch.setattr(review, '_label', lambda state: called.append(('single', state)))
    if entry == 'mouse':
        review.btn_uncensored.click()
    else:
        review.table.setFocus()
        review._run_review_shortcut('uncensored')
    assert called == [('batch', CensorState.UNCENSORED)]


def test_manual_next_starts_after_table_focus_when_no_selection(review):
    a, b, c = (review.add_fixture(name) for name in 'abc')
    review.table.sortItems(0, Qt.AscendingOrder)
    select(review, b)
    review.table.clearSelection()
    review.btn_next.click()
    assert review._current.path == c
    assert review._current.path != a


@pytest.mark.parametrize('column', [0, 2, 3])
@pytest.mark.parametrize('order', [Qt.AscendingOrder, Qt.DescendingOrder])
@pytest.mark.parametrize('view', [WorkflowView.ALL, WorkflowView.TODO])
def test_repeated_confirmation_walks_pre_click_visible_order(review, column, order, view):
    for name in 'abcd':
        review.add_fixture(name)
    review.combo_filter.setCurrentIndex(review.combo_filter.findData(view.value))
    review.table.sortItems(column, order)
    initial = paths(review, visible=True)
    expected = initial[1:] + initial[:1]
    select(review, expected[0])
    visited = []
    for index in range(4):
        assert review._current is not None
        visited.append(review._current.path)
        (review.btn_censored if index % 2 else review.btn_uncensored).click()
    assert visited == expected
    assert review._current is None
    assert review._selected_visible_paths() == []


@pytest.mark.parametrize('column', [0, 2, 3])
def test_auto_next_disabled_keeps_same_visible_video_selected_after_sort(review, column):
    for name in 'abc':
        review.add_fixture(name)
    review.table.sortItems(column, Qt.AscendingOrder)
    selected = paths(review)[1]
    select(review, selected)
    review.workspace.state['auto_next'] = False
    review.btn_uncensored.click()
    assert review._current.path == selected
    assert review._selected_visible_paths() == [selected]
    assert review._table_presenter.active_path() == selected


def test_auto_next_can_continue_visible_low_risk_rows(review):
    a, b = (review.add_fixture(name, low_risk=True) for name in 'ab')
    review.combo_filter.setCurrentIndex(review.combo_filter.findData(WorkflowView.PARKED.value))
    review.table.sortItems(0, Qt.AscendingOrder)
    select(review, a)
    review.btn_uncensored.click()
    assert review._current is not None and review._current.path == b


def test_marking_finished_list_again_does_not_silently_mark_another_row(review):
    a = review.add_fixture('a')
    select(review, a)
    review.btn_uncensored.click()
    assert review._current is None
    review.btn_censored.click()
    assert review._results[a].state is CensorState.UNCENSORED


def test_label_failure_keeps_target_and_order(review, monkeypatch):
    for name in 'abc':
        review.add_fixture(name)
    select(review, paths(review)[1])
    current = review._current.path
    before = paths(review)
    def fail(*args, **kwargs):
        raise OSError('synthetic unavailable file')
    monkeypatch.setattr(review._service, 'label', fail)
    review.btn_uncensored.click()
    assert review._current.path == current
    assert review._selected_visible_paths() == [current]
    assert paths(review) == before


def test_batch_confirmation_advances_once_after_last_selected_visible_row(review, qtbot):
    a, b, c, d = (review.add_fixture(name) for name in 'abcd')
    review.table.sortItems(0, Qt.AscendingOrder)
    select(review, a)
    row = review._table_presenter.row_for(c)
    review.table.selectionModel().select(review.table.model().index(row, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    review._start_batch_confirmation([a, c], CensorState.UNCENSORED)
    qtbot.waitUntil(lambda: not review._batch_review_worker.isRunning(), timeout=5000)
    qtbot.waitUntil(lambda: all(review._results[p].state is CensorState.UNCENSORED
                              for p in (a, c)), timeout=5000)
    assert review._current is not None and review._current.path == d
    assert review._selected_visible_paths() == [d]
    assert review._results[b].state is CensorState.UNKNOWN


def test_failed_batch_does_not_advance_past_unsaved_selection(review, qtbot, monkeypatch):
    a, b, c = (review.add_fixture(name) for name in 'abc')
    review.table.sortItems(0, Qt.AscendingOrder)
    select(review, b)
    original = review._service.label
    def label(path, *args, **kwargs):
        if path == b:
            raise OSError('synthetic save failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(review._service, 'label', label)
    review._start_batch_confirmation([a, b], CensorState.UNCENSORED)
    qtbot.waitUntil(lambda: not review._batch_review_worker.isRunning(), timeout=5000)
    qtbot.waitUntil(lambda: review._results[a].state is CensorState.UNCENSORED, timeout=5000)
    assert review._current.path == b
    assert review._results[c].state is CensorState.UNKNOWN


def test_keyboard_row_navigation_and_label_follow_same_order(review, qtbot):
    for name in 'abcd':
        review.add_fixture(name)
    review.table.sortItems(0, Qt.DescendingOrder)
    order = paths(review)
    select(review, order[1])
    qtbot.keyClick(review.table, Qt.Key_Down)
    assert review._current.path == order[2]
    assert review._table_presenter.active_path() == order[2]
    review.btn_uncensored.click()
    assert review._current.path == order[3]


def test_next_skips_confirmed_but_keeps_visible_deferred_row(review):
    a = review.add_fixture('a')
    review.add_fixture('b', manual=True)
    c = review.add_fixture('c uncensored')
    review.table.sortItems(0, Qt.AscendingOrder)
    select(review, a)
    review.btn_uncensored.click()
    assert review._current.path == c


def test_batch_completion_does_not_override_new_user_selection(review, qtbot):
    a, b, c = (review.add_fixture(name) for name in 'abc')
    select(review, a)
    review._start_batch_confirmation([a], CensorState.UNCENSORED)
    assert review._batch_review_worker.wait(3000)
    # Queue delivery has not run yet, so this is a genuine new choice made
    # after saving started and before the delayed completion navigation.
    select(review, c)
    qtbot.waitUntil(lambda: review._results[a].state is CensorState.UNCENSORED, timeout=5000)
    assert review._current.path == c
    assert review._selected_visible_paths() == [c]
    assert review._results[b].state is CensorState.UNKNOWN


def test_stopping_finished_batch_before_delivery_cancels_automatic_navigation(review, qtbot):
    from ui.services import stop_page
    a, b = (review.add_fixture(name) for name in 'ab')
    select(review, a)
    review._start_batch_confirmation([a], CensorState.UNCENSORED)
    assert review._batch_review_worker.wait(3000)
    stop_page(review)
    qtbot.waitUntil(lambda: review._results[a].state is CensorState.UNCENSORED, timeout=5000)
    assert review._current.path == a
    assert review._results[b].state is CensorState.UNKNOWN
