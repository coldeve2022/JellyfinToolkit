"""One action loads all history, with bounded rendering and cancellation."""
import threading

from censor.models import VideoVerdict
from censor.store import VerdictStore
from config import ToolkitConfig


def seed(store, count):
    with store._connect() as connection:
        connection.executemany('INSERT INTO censor_probe(path) VALUES (?)',
                               [(f'generic-{index:05}.mp4',) for index in range(count)])


def test_history_stream_uses_one_snapshot_when_results_are_updated(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    seed(store, 12)
    pages = store.iter_history_pages(5)
    first = next(pages)
    store.put(VideoVerdict(path='newer-item.mp4'))
    remaining = list(pages)
    paths = [r.path for page in [first, *remaining] for r in page['records']]
    assert len(paths) == len(set(paths)) == 12
    assert 'newer-item.mp4' not in paths
    assert remaining[-1]['next_offset'] == 12


def test_history_stream_advances_past_entire_corrupt_batch(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    seed(store, 8)
    with store._connect() as connection:
        connection.execute("UPDATE censor_probe SET updated_at='same',state='broken' WHERE path<'generic-00005'")
        connection.execute("UPDATE censor_probe SET updated_at='same' WHERE path>='generic-00005'")
    pages = list(store.iter_history_pages(5))
    assert pages[0]['records'] == [] and pages[0]['has_more']
    assert len(pages[1]['records']) == 3 and not pages[1]['has_more']


def test_one_click_loads_more_than_one_page_and_removes_manual_more_action(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    seed(page._service.store, 1103)
    page._load_history()
    qtbot.waitUntil(lambda: len(page._results) == 1103, timeout=15000)
    qtbot.waitUntil(lambda: not page._history_worker.isRunning(), timeout=5000)
    assert page.table.rowCount() == 1103
    assert all(row.from_history for row in page._results.values())
    assert '1103' in page.progress_title.text()
    history_menu = next(a.menu() for a in page.more_button.menu().actions()
                        if a.text() == '历史与重新分析')
    assert [a.text() for a in history_menu.actions() if '加载' in a.text()] == ['加载全部历史']
    page.shutdown()


def test_history_worker_waits_for_render_and_stop_wakes_it(qapp, qtbot, tmp_path):
    from ui.censor_history import HistoryLoadWorker
    store = VerdictStore(tmp_path / 'history.db')
    seed(store, 12)
    worker = HistoryLoadWorker(store, page_size=5)
    batches = []
    worker.page_ready.connect(batches.append)
    worker.start()
    try:
        qtbot.waitUntil(lambda: len(batches) == 1)
        qtbot.wait(120)
        assert len(batches) == 1 and worker.isRunning()
        worker.stop()
        qtbot.waitUntil(lambda: not worker.isRunning())
        assert len(batches) == 1
    finally:
        worker.stop()
        worker.wait(3000)


def test_stop_discards_unrendered_history_and_never_auto_restarts(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    seed(page._service.store, 1103)
    page._load_history()
    qtbot.waitUntil(lambda: bool(page._pending_results), timeout=5000)
    page._stop()
    qtbot.waitUntil(lambda: not page._history_worker.isRunning(), timeout=5000)
    loaded = len(page._results)
    qtbot.wait(200)
    assert len(page._results) == loaded < 1103
    assert not page._pending_results
    assert page._service.store.stats().total == 1103
    page.shutdown()


def test_history_reading_happens_off_the_gui_thread(qapp, qtbot, monkeypatch):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    main_thread = threading.get_ident()
    threads = []
    original = page._service.store._decode_row
    def record_thread(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(page._service.store, '_decode_row', record_thread)
    seed(page._service.store, 1)
    page._load_history()
    qtbot.waitUntil(lambda: len(page._results) == 1)
    qtbot.waitUntil(lambda: not page._history_worker.isRunning())
    assert threads and all(ident != main_thread for ident in threads)
    page.shutdown()


def test_empty_history_finishes_without_waiting_forever(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    page._load_history()
    qtbot.waitUntil(lambda: not page._history_loading)
    assert '全部加载' in page.progress_title.text()
    assert not page._results
    assert page.scan_progress.value() == page.scan_progress.maximum()
    page.shutdown()


def test_shutdown_stops_history_rendering_and_unblocks_the_worker(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig(censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    seed(page._service.store, 1103)
    page._load_history()
    qtbot.waitUntil(lambda: bool(page._pending_results))
    page.shutdown()
    qtbot.waitUntil(lambda: not page._history_worker.isRunning())
    loaded = len(page._results)
    qtbot.wait(200)
    assert not page._pending_results and len(page._results) == loaded
