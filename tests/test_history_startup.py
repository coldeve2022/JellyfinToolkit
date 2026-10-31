"""First visit restores persisted history without analyzing or changing the view."""
import threading

import pytest

from censor import CensorState, VideoVerdict
from censor.models import Evidence, EvidenceSource
from censor.fusion import fuse
from censor.review_route import WorkflowView
from config import ToolkitConfig


@pytest.fixture
def page(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    widget = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(widget)
    yield widget
    widget.shutdown()
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(widget), timeout=5000)


def seed(page, count=603):
    """造一批"历史已完成的扫描结果"。

    显式写 `status='completed'`：只插 path 的话列默认值是 'partial'，
    那是"插进去但还没分析过"的语义，界面会如实标注"未跑完"。
    """
    with page._service.store._connect() as connection:
        connection.executemany(
            "INSERT INTO censor_probe(path, status) VALUES (?, 'completed')",
            [(f'old-{index:05}.mp4',) for index in range(count)],
        )
    page._service.store.set_label('old-00000.mp4', CensorState.CENSORED)
    manual = VideoVerdict(path='old-00000.mp4')
    manual.verdict = fuse([Evidence(EvidenceSource.MANUAL, CensorState.CENSORED, 1., '人工确认')])
    page._service.store.put(manual)


def test_first_visit_restores_all_including_manual_without_scan_or_view_reset(page, qtbot, monkeypatch):
    seed(page)
    forbidden = []
    monkeypatch.setattr(page, '_start_scan', lambda *a, **k: forbidden.append('scan'))
    monkeypatch.setattr(page._service, 'probe', lambda *a, **k: forbidden.append('probe'))
    monkeypatch.setattr(page._service, 'probe_many', lambda *a, **k: forbidden.append('probe_many'))
    monkeypatch.setattr(page._service, '_queue_review_metadata', lambda *a, **k: forbidden.append('external_tags'))
    monkeypatch.setattr(page._service, 'review_queue', lambda *a, **k: forbidden.append('pending') or [])
    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.DONE.value))
    page.search.setText('old-00000')
    page.show()
    qtbot.waitUntil(lambda: len(page._results) == 603, timeout=5000)
    assert page._results['old-00000.mp4'].verdict.of(EvidenceSource.MANUAL)
    assert all(result.from_history for result in page._results.values())
    assert page.combo_filter.currentData() == WorkflowView.DONE.value
    assert page.search.text() == 'old-00000'
    assert forbidden == []


def test_return_to_page_does_not_reload_or_clear_existing_results(page, qtbot, monkeypatch):
    seed(page, 1)
    page.show()
    qtbot.waitUntil(lambda: len(page._results) == 1, timeout=5000)
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(page))
    page._on_item(VideoVerdict(path='current-scan.mp4'))
    monkeypatch.setattr(page, '_load_history', lambda: pytest.fail('repeat history startup'))
    page.hide()
    page.show()
    qtbot.wait(150)
    assert 'current-scan.mp4' in page._results


def test_first_visit_preserves_existing_results_and_reads_saved_counts_off_thread(page, qtbot, monkeypatch):
    seed(page, 601)
    page._on_item(VideoVerdict(path='current-scan.mp4'))
    threads = []
    original = page._service.store.stats
    def stats():
        threads.append(threading.get_ident())
        return original()
    monkeypatch.setattr(page._service.store, 'stats', stats)
    page.show()
    qtbot.waitUntil(lambda: bool(threads), timeout=2000)
    qtbot.waitUntil(lambda: '601' in page.data_status.text())
    assert '人工确认 1' in page.data_status.text()
    assert str(page._service.store.db_path.resolve()) in page.data_status.text()
    assert threads and all(t != threading.get_ident() for t in threads)
    assert list(page._results) == ['current-scan.mp4']


@pytest.mark.parametrize('stop_method', ['_stop', 'shutdown', 'close'])
def test_stop_before_deferred_start_prevents_restart(page, qtbot, monkeypatch, stop_method):
    seed(page, 1)
    calls = []
    monkeypatch.setattr(page, '_load_history', lambda: calls.append('load'))
    page.show()
    getattr(page, stop_method)()
    page.hide()
    page.show()
    qtbot.wait(200)
    assert calls == []
    assert not page._results


def test_startup_waits_for_page_task_and_keeps_its_results(page, qtbot):
    from threading import Event
    from ui.services import CallWorker, start_task, busy
    seed(page, 1)
    gate = Event()
    page._test_worker = CallWorker(lambda stop: gate.wait(2))
    start_task(page, page._test_worker, 'existing page task')
    page.show()
    qtbot.wait(100)
    assert not page._history_loading and page._history_worker is None
    page._on_item(VideoVerdict(path='active-scan.mp4'))
    gate.set()
    qtbot.waitUntil(lambda: not busy(page), timeout=5000)
    qtbot.wait(150)
    assert list(page._results) == ['active-scan.mp4']


def test_stop_during_automatic_history_does_not_restart_on_return(page, qtbot):
    from ui.services import busy
    seed(page, 1103)
    page.show()
    qtbot.waitUntil(lambda: bool(page._pending_results), timeout=5000)
    page._stop()
    qtbot.waitUntil(lambda: not busy(page), timeout=5000)
    loaded = len(page._results)
    worker = page._history_worker
    page.hide()
    page.show()
    qtbot.wait(200)
    assert len(page._results) == loaded < 1103
    assert page._history_worker is worker and not worker.isRunning()
    assert page._service.store.stats().total == 1103


def test_startup_waits_for_existing_task_then_loads_history_if_still_empty(page, qtbot):
    from threading import Event
    from ui.services import CallWorker, start_task
    seed(page, 1)
    gate = Event()
    page._test_worker = CallWorker(lambda stop: gate.wait(2))
    start_task(page, page._test_worker, 'existing task without rows')
    page.show()
    qtbot.wait(100)
    assert page._history_worker is None
    gate.set()
    qtbot.waitUntil(lambda: len(page._results) == 1, timeout=5000)


def test_another_pages_task_does_not_prevent_read_only_history(page, qapp, qtbot):
    from threading import Event
    from PySide6.QtWidgets import QWidget
    from ui.services import CallWorker, start_task, busy
    other = QWidget()
    qtbot.addWidget(other)
    gate = Event()
    other._worker = CallWorker(lambda stop: gate.wait(3))
    seed(page, 1)
    start_task(other, other._worker, 'other page task', mutating=True)
    try:
        page.show()
        qtbot.waitUntil(lambda: len(page._results) == 1, timeout=2000)
        assert busy(other)
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not busy(other), timeout=5000)


def test_auto_history_reports_saved_counts_without_gui_db_scan(page, qtbot, monkeypatch):
    seed(page, 603)
    threads = []
    original = page._service.store.stats
    def stats():
        threads.append(threading.get_ident())
        return original()
    monkeypatch.setattr(page._service.store, 'stats', stats)
    page.show()
    qtbot.waitUntil(lambda: len(page._results) == 603, timeout=5000)
    assert '已入库 603 条' in page.data_status.text()
    # 历史行都是完整分析过的，不该被标成"未跑完"
    assert '未跑完' not in page.data_status.text()
    assert '人工确认 1' in page.data_status.text()
    assert threads and all(t != threading.get_ident() for t in threads)

