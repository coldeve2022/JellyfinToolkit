"""Regression contracts for the generic scan, table and owned task pipeline."""
from threading import Event

import pytest
from PySide6.QtCore import QThread, Qt, Signal, QItemSelectionModel
from PySide6.QtWidgets import QWidget

from censor import CensorOptions, CensorService, CensorState, VideoVerdict
from censor.models import ProcessingStatus
from config import ToolkitConfig


@pytest.fixture
def page(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    widget = CensorshipPage(ToolkitConfig(censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(widget)
    yield widget
    widget.shutdown()
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(widget), timeout=5000)


def test_folder_import_reuses_manual_cache_before_filename_deferral(qapp, tmp_path):
    from ui.censor_scan import CensorScanWorker
    path = tmp_path / 'DEMO-001 uncensored.mp4'; path.write_bytes(b'ordinary synthetic fixture')
    service = CensorService({'censor_visual_enabled': False, 'censor_sync_nfo': False,
        'censor_sync_jellyfin': False}, data_dir=tmp_path / 'state')
    previous = service.probe(path, CensorOptions(detect_visual=False))
    service.label(path, CensorState.CENSORED, 'manual fixture', base=previous)
    worker = CensorScanWorker(service, [str(tmp_path)], CensorOptions(detect_visual=False))
    received = []; worker.item_done.connect(received.append)
    worker.run()
    assert [row.path for row in received] == [str(path)]
    assert received[0].cache_hit and received[0].state is CensorState.CENSORED
    assert worker.jobs.stats().get('deferred', 0) == 0


def test_explicit_force_is_not_silently_deferred(qapp, tmp_path, monkeypatch):
    from ui.censor_scan import CensorScanWorker
    path = tmp_path / 'DEMO-002 uncensored.mp4'; path.write_bytes(b'synthetic')
    service = CensorService({'censor_visual_enabled': False}, data_dir=tmp_path / 'state')
    called = []
    def probe(path, options, **kwargs):
        called.append(path)
        return VideoVerdict(path=path, status=ProcessingStatus.COMPLETED)
    monkeypatch.setattr(service, 'probe', probe)
    worker = CensorScanWorker(service, [str(path)], CensorOptions(force=True, detect_visual=False))
    worker.run()
    assert called == [str(path)]
    assert worker.jobs.stats().get('done') == 1


def test_reused_pending_item_is_completed_in_durable_queue(page, tmp_path):
    path = tmp_path / 'DEMO-003.mp4'; path.write_bytes(b'synthetic')
    options = CensorOptions(detect_visual=False)
    page._service.probe(path, options)
    page._jobs.enqueue([str(path)])
    todo, reused = page._partition_reusable([str(path)], options)
    assert todo == [] and len(reused) == 1
    assert page._jobs.pending() == []


def test_result_refresh_updates_identifier_and_numeric_order(page):
    page._on_item(VideoVerdict(path='one.mp4', number='DEMO-001', visual_score=2))
    page._on_item(VideoVerdict(path='two.mp4', number='DEMO-002', visual_score=10))
    page._on_item(VideoVerdict(path='three.mp4', number='DEMO-003', visual_score=100))
    page._on_item(VideoVerdict(path='one.mp4', number='DEMO-999', visual_score=2))
    row = next(r for r in range(page.table.rowCount()) if page.table.item(r, 0).data(Qt.UserRole) == 'one.mp4')
    assert page.table.item(row, 1).text() == 'DEMO-999'
    page.table.sortItems(4, Qt.AscendingOrder)
    assert [page.table.item(r, 0).data(Qt.UserRole) for r in range(3)] == ['one.mp4', 'two.mp4', 'three.mp4']


def test_numeric_scores_sort_by_value(page):
    for path, score in [('one.mp4', 2), ('two.mp4', 10), ('three.mp4', 100)]:
        page._on_item(VideoVerdict(path=path, visual_score=score))
    page.table.sortItems(4, Qt.AscendingOrder)
    assert [page.table.item(r, 0).data(Qt.UserRole) for r in range(3)] == ['one.mp4', 'two.mp4', 'three.mp4']


def test_all_view_does_not_reclassify_every_row_for_filter(page, monkeypatch):
    from censor.review_route import WorkflowView
    import ui.pages.censorship as module
    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.ALL.value))
    def redundant_classification(result):
        raise AssertionError('ALL accepts every processing bucket without recomputing it')
    monkeypatch.setattr(module, 'review_bucket', redundant_classification)
    assert page._matches_filter(VideoVerdict(path='generic.mp4'))


def test_summary_does_not_check_manual_workflow_for_automatic_rows(page, monkeypatch):
    import ui.pages.censorship as module
    page._on_item(VideoVerdict(path='generic.mp4'))
    def redundant_classification(result):
        raise AssertionError('an automatic row cannot be manually confirmed')
    monkeypatch.setattr(module, 'awaits_replacement', redundant_classification)
    page._refresh_stats()
    assert '待处理 1' in page._stats_label.text()


def test_multi_selection_shows_current_selected_row_and_clear_removes_review(page):
    page.table.setSortingEnabled(False)
    page._on_item(VideoVerdict(path='one.mp4'))
    page._on_item(VideoVerdict(path='two.mp4'))
    page.table.selectAll()
    row = next(r for r in range(2) if page.table.item(r, 0).data(Qt.UserRole) == 'two.mp4')
    page.table.setCurrentCell(row, 0, QItemSelectionModel.NoUpdate)
    page._on_row_selected()
    assert page._current.path == 'two.mp4'
    page.table.clearSelection()
    page._on_row_selected()
    assert page._current is None


def test_stop_cancels_a_task_waiting_for_owner(qapp, qtbot):
    from ui.services import TaskManager, start_task, stop_page
    owner = QWidget(); qtbot.addWidget(owner)
    gate = Event(); calls = []
    class First(QThread):
        def run(self): gate.wait(5)
        def stop(self): gate.set()
    class Second(QThread):
        def run(self): calls.append('started')
    first, second = First(), Second()
    manager = TaskManager.instance()
    try:
        assert manager.submit(first, owner, 'first generic task')
        assert start_task(owner, second, 'queued generic task')
        stop_page(owner)
        gate.set(); first.wait(3000)
        qtbot.waitUntil(lambda: not manager.running(owner), timeout=5000)
        qtbot.wait(150)
        assert calls == []
        assert not any(row['title'] == 'queued generic task' for row in manager.records)
    finally:
        gate.set(); first.wait(3000); second.wait(3000)


def test_stop_cancels_after_stopped_continuation(qapp, qtbot):
    from ui.services import TaskManager, after_stopped, stop_page
    owner = QWidget(); qtbot.addWidget(owner)
    gate = Event(); called = []
    class First(QThread):
        def run(self): gate.wait(5)
        def stop(self): gate.set()
    first = First(); manager = TaskManager.instance()
    try:
        assert manager.submit(first, owner, 'continuation fixture')
        after_stopped(owner, lambda: called.append('continued'))
        stop_page(owner); gate.set(); first.wait(3000)
        qtbot.waitUntil(lambda: not manager.running(owner), timeout=5000)
        qtbot.wait(150)
        assert called == []
    finally:
        gate.set(); first.wait(3000)


def test_worker_signal_is_disconnected_from_destroyed_manager(qapp, qtbot):
    from shiboken6 import delete
    from ui.services import TaskManager
    owner = QWidget(); qtbot.addWidget(owner)
    gate = Event()
    class Worker(QThread):
        task_status = Signal(object)
        def run(self): gate.wait(5)
    worker = Worker(); manager = TaskManager()
    assert manager.submit(worker, owner, 'lifetime fixture')
    delete(manager)
    try:
        with qtbot.captureExceptions() as errors:
            worker.task_status.emit(dict(progress=10, detail='fixture'))
            qapp.processEvents()
        assert errors == []
    finally:
        gate.set(); worker.wait(3000); qapp.processEvents()


def test_batch_sort_preserves_filter_identity(page):
    from censor.models import Verdict
    page.combo_conclusion.setCurrentIndex(page.combo_conclusion.findData(CensorState.CENSORED.value))
    page.table.sortItems(0, Qt.AscendingOrder)
    page._queue_item(VideoVerdict(path='z.mp4', verdict=Verdict(state=CensorState.CENSORED)))
    page._queue_item(VideoVerdict(path='a.mp4', verdict=Verdict(state=CensorState.UNKNOWN)))
    page._drain_results()
    for row in range(2):
        path = page.table.item(row, 0).data(Qt.UserRole)
        assert page.table.isRowHidden(row) == (path == 'a.mp4')


def test_layout_presets_and_opt_in_view_memory(page, qapp, qtbot):
    from censor.review_route import WorkflowView
    page._apply_review_layout('diagnostic')
    assert all(not page.table.isColumnHidden(col) for col in (0, 1, 2, 3, 4, 5, 6))
    page._apply_review_layout('review')
    assert page.table.isColumnHidden(5) and not page.table.isColumnHidden(0)
    page.workspace.state['remember_view'] = True
    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.DONE.value))
    page.workspace.save()
    from ui.pages.censorship import CensorshipPage
    other = CensorshipPage(page.cfg); qtbot.addWidget(other)
    assert other.combo_filter.currentData() == WorkflowView.DONE.value
    other.shutdown()


def test_auto_next_keeps_pre_confirmation_order_when_status_sort_moves_row(page, tmp_path, monkeypatch):
    paths = []
    for index in range(3):
        path = tmp_path / f'DEMO-{index:03}.mp4'; path.write_bytes(b'ordinary synthetic fixture')
        result = page._service.probe(path, CensorOptions(detect_visual=False))
        page._on_item(result)
        paths.append(str(path))
    page.table.sortItems(3, Qt.AscendingOrder)
    order = [page.table.item(row, 0).data(Qt.UserRole) for row in range(3)]
    page.table.selectRow(1)
    monkeypatch.setattr(page.metadata_controller, 'schedule', lambda: None)
    page.workspace.state['auto_next'] = True
    page._label(CensorState.UNCENSORED)
    assert page._current.path == order[2]


@pytest.mark.parametrize('method', ['task_center', 'stop_all', 'stop_all_between_stages'])
def test_task_center_stop_cancels_owner_pipeline(qapp, qtbot, method):
    from ui.services import TaskManager, start_task, after_stopped
    owner = QWidget(); qtbot.addWidget(owner)
    gate = Event(); called = []
    class First(QThread):
        def run(self): gate.wait(5)
        def stop(self): gate.set()
    class Second(QThread):
        def run(self): called.append('pending')
    first, second = First(), Second()
    manager = TaskManager.instance()
    try:
        assert manager.submit(first, owner, 'task-center fixture')
        assert start_task(owner, second, 'must not start')
        after_stopped(owner, lambda: called.append('continued'))
        task_id = next(key for key, value in manager._active.items() if value['owner'] is owner)
        if method == 'stop_all_between_stages':
            gate.set(); first.wait(3000)
            manager._active[task_id]['delivery_barrier_passed'] = True
            manager._poll()
            assert task_id not in manager._active
        if method == 'task_center':
            manager.stop(task_id)
        else:
            manager.stop_all()
        gate.set(); first.wait(3000)
        qtbot.waitUntil(lambda: not manager.running(owner), timeout=5000)
        qtbot.wait(160)
        assert called == []
    finally:
        gate.set(); first.wait(3000); second.wait(3000)


def test_cached_preparation_uses_bounded_render_queue(page, qtbot):
    rows = [VideoVerdict(path=f'cached-{index:04}.mp4', cache_hit=True) for index in range(203)]
    page._prepared_scan(([], rows), CensorOptions(detect_visual=False), False)
    assert page.table.rowCount() == 0
    assert len(page._pending_results) == 203
    qtbot.waitUntil(lambda: page.table.rowCount() == 203, timeout=5000)
    assert not page._pending_results
