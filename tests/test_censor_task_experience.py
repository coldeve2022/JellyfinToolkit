"""Interactive scanning, explicit cache reuse and review lifecycle regressions."""
from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread
from censor.models import CensorState, Evidence, EvidenceSource, VideoVerdict
from censor.base import BaseDetector
from censor.registry import detectors
from censor.service import CensorService, CensorOptions
from censor.review_route import category
from config import ToolkitConfig
from ui.pages.censorship import CensorshipPage, CensorScanWorker
from ui.services import TaskManager


def test_scan_keeps_table_and_filters_interactive_but_locks_settings(qapp, qtbot):
    page = CensorshipPage(ToolkitConfig())
    gate = Event()
    class Worker(QThread):
        def run(self):
            gate.wait(5)
    worker = Worker()
    manager = TaskManager()
    try:
        assert manager.submit(worker, page, 'scan fixture')
        assert page.table.isEnabled()
        assert page.table.horizontalHeader().isEnabled()
        assert page.combo_filter.isEnabled()
        assert not page.evidence_count.isEnabled()
    finally:
        gate.set()
        worker.wait(3000)
        qtbot.waitUntil(lambda: not manager.running(), timeout=15000)
        page.deleteLater()
        manager._timer.stop()


def test_page_updates_never_wait_for_queue_database(qapp, monkeypatch):
    page = CensorshipPage(ToolkitConfig())
    def forbidden(*args):
        raise AssertionError('queue DB access on GUI thread')
    monkeypatch.setattr(page._jobs, 'stats', forbidden)
    monkeypatch.setattr(page._jobs, 'note_for', forbidden)
    page._on_item(VideoVerdict(path='generic.mp4'))
    page.table.selectRow(0)
    page._on_row_selected()
    page.deleteLater()


def test_reused_unreviewed_positive_is_still_reviewable(tmp_path):
    calls = []
    class Visual(BaseDetector):
        name = 'test.lifecycle'
        capabilities = ('visual',)
        def detect(self, ctx):
            calls.append(ctx.path)
            return [Evidence(EvidenceSource.VISUAL,CensorState.CENSORED,.85,'generic grid')]
    detectors.add(Visual.name, Visual, replace=True)
    try:
        path = tmp_path / 'generic.mp4'
        path.write_bytes(b'generic fixture')
        svc = CensorService({'censor_detectors':[Visual.name]},data_dir=tmp_path / 'state')
        first = svc.probe(path)
        second = svc.probe(path)
        assert second.cache_hit
        assert not first.cache_hit
        assert second.needs_review
        assert category(second) == 'review'
        assert len(calls) == 1
        svc.label(path,CensorState.UNCENSORED,'human review',base=second)
        assert category(svc.probe(path)) == 'manual'
    finally:
        detectors.discard(Visual.name)


def test_worker_reports_discovery_and_analysis_to_task_protocol(tmp_path):
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'generic fixture')
    svc = CensorService({'censor_visual_enabled':False},data_dir=tmp_path / 'state')
    worker = CensorScanWorker(svc,[str(path)],CensorOptions(detect_visual=False))
    snapshots = []
    worker.task_status.connect(snapshots.append)
    worker.run()
    assert any(s['phase'] == '发现文件' for s in snapshots)
    assert snapshots[-1]['completed'] == 1
    assert snapshots[-1]['total'] == 1
    assert snapshots[-1]['progress'] == 100
    assert snapshots[-1]['review'] == 1
    assert worker.stats.get('error','') == ''


def test_primary_progress_is_separate_from_summary(qapp):
    page = CensorshipPage(ToolkitConfig())
    page._on_task_status(dict(phase='分析视频',completed=3,total=20,discovered=20,
        progress=15,cached=1,failed=0,deferred=2,review=2,manual=0,
        detail='generic.mp4 · 分析画面 37/60',active=[]))
    assert page.scan_progress.value() == 15
    assert '3/20' in page.progress_title.text()
    assert '37/60' in page.progress_detail.text()
    page.deleteLater()

def test_bulk_delivery_yields_and_preserves_review_selection(qapp, qtbot):
    from PySide6.QtCore import QTimer
    page = CensorshipPage(ToolkitConfig())
    for i in range(2000):
        page._queue_item(VideoVerdict(path=f'generic-{i:04d}.mp4',cache_hit=True))
    heartbeats = []
    QTimer.singleShot(0,lambda:heartbeats.append(len(page._pending_results)))
    qtbot.waitUntil(lambda:page.table.rowCount() >= 80, timeout=15000)
    page.table.selectRow(0)
    selected = page._current.path
    qtbot.waitUntil(lambda:not page._pending_results, timeout=10000)
    assert page.table.rowCount() == 2000
    assert heartbeats and heartbeats[0] > 0
    assert page._current.path == selected
    assert page._category_counts['review'] == 2000
    page.deleteLater()


def test_task_center_receives_the_same_phase_and_counts(qapp, qtbot):
    from PySide6.QtCore import Signal
    from ui.pages.tasks import TaskCenterPage
    page = CensorshipPage(ToolkitConfig())
    gate = Event()
    class Worker(QThread):
        task_status = Signal(object)
        def run(self):
            gate.wait(5)
    worker = Worker()
    manager = TaskManager.instance()
    center = TaskCenterPage(page.cfg)
    try:
        assert manager.submit(worker,page,'generic pixelation task')
        payload = dict(phase='分析视频',completed=7,total=20,progress=35,
                       detail='分析视频 7/20 · generic.mp4 · 分析画面 37/60',active=[])
        worker.task_status.emit(payload)
        qapp.processEvents()
        record = next(r for r in manager.records if r['title'] == 'generic pixelation task')
        assert record['progress'] == 35
        assert record['scan']['completed'] == 7
        assert '37/60' in record['detail']
        row = next(r for r in range(center.tasks.rowCount())
                   if center.tasks.item(r,0).text() == 'generic pixelation task')
        assert center.tasks.item(row,2).text() == '35%'
        assert '37/60' in center.tasks.item(row,5).text()
    finally:
        gate.set()
        worker.wait(3000)
        qtbot.waitUntil(lambda:not manager.running(page),timeout=15000)
        center.deleteLater()
        page.deleteLater()


def test_legacy_auto_positive_remains_in_review_history(tmp_path):
    from censor.store import VerdictStore
    from censor.models import Verdict
    store = VerdictStore(tmp_path / 'state.db')
    store.put(VideoVerdict(path='generic.mp4',verdict=Verdict(state=CensorState.CENSORED,
        confidence=.85,needs_review=False,
        evidence=[Evidence(EvidenceSource.VISUAL,CensorState.CENSORED,.85,'legacy grid')])))
    assert [r.path for r in store.review_queue()] == ['generic.mp4']

def test_cache_heavy_worker_throttles_status_events(tmp_path, monkeypatch):
    paths = []
    for i in range(30):
        path = tmp_path / f'generic-{i}.mp4'
        path.write_bytes(b'generic fixture')
        paths.append(str(path))
    # 数据库放项目内而不是 %TEMP%：Temp 目录有杀软/清理程序实时扫描，
    # SQLite 的 -wal 写入偶发失败会让"30 个文件全成功"这条断言随机崩溃，
    # 而与"节流"这个被测语义无关。素材文件仍用 tmp_path。
    state = Path(__file__).resolve().parent.parent / 'build' / 'scan-state' / tmp_path.name
    state.mkdir(parents=True, exist_ok=True)
    svc = CensorService({'censor_visual_enabled':False},data_dir=state)
    # 单线程：这条用例验的是"状态节流"，不是并发写库。默认 4 线程写同一个
    # SQLite 时，本机杀软实时扫描会让个别写入变成 readonly，断言随机失败。
    worker = CensorScanWorker(svc,paths,CensorOptions(detect_visual=False),workers=1)
    # One fixed reporting interval; file completions must not bypass throttling.
    worker._clock = lambda: 1.
    snapshots = []
    worker.task_status.connect(snapshots.append)
    worker.run()
    assert len(snapshots) < 10
    assert snapshots[-1]['completed'] == 30

def test_buffered_snapshot_cannot_overwrite_new_manual_review(qapp,tmp_path):
    page = CensorshipPage(ToolkitConfig())
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'generic fixture')
    initial = page._service.probe(path,CensorOptions(detect_visual=False))
    page._on_item(initial)
    page.table.selectRow(0)
    page._on_row_selected()
    from copy import deepcopy
    stale = deepcopy(initial)
    page._queue_item(stale)
    page._label(CensorState.UNCENSORED)
    assert category(page._results[str(path)]) == 'manual'
    page._drain_results()
    assert category(page._results[str(path)]) == 'manual'
    assert page._results[str(path)].state is CensorState.UNCENSORED
    page.deleteLater()


def test_review_limit_counts_actionable_items_not_manual_prefix(tmp_path):
    from censor.store import VerdictStore
    from censor.models import Verdict
    store = VerdictStore(tmp_path / 'state.db')
    store.put(VideoVerdict(path='manual.mp4',visual_score=40,
        verdict=Verdict(state=CensorState.CENSORED,evidence=[Evidence(EvidenceSource.MANUAL,CensorState.CENSORED,1.)])))
    store.set_label('manual.mp4', CensorState.CENSORED)
    store.put(VideoVerdict(path='automatic.mp4',visual_score=35,
        verdict=Verdict(state=CensorState.CENSORED,needs_review=False,
        evidence=[Evidence(EvidenceSource.VISUAL,CensorState.CENSORED,.85)])))
    assert [r.path for r in store.review_queue(1)] == ['automatic.mp4']

def test_unknown_identity_after_replacement_never_inherits_manual_review(qapp,tmp_path):
    from censor.models import ProcessingStatus
    page = CensorshipPage(ToolkitConfig())
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'original fixture')
    page._on_item(page._service.probe(path,CensorOptions(detect_visual=False)))
    page.table.selectRow(0)
    page._label(CensorState.UNCENSORED)
    path.write_bytes(b'replacement fixture')
    cancelled = page._service.probe(path,should_stop=lambda:True)
    assert cancelled.status is ProcessingStatus.CANCELLED
    page._on_item(cancelled)
    assert category(page._results[str(path)]) == 'failed'
    assert page._results[str(path)].verdict.of(EvidenceSource.MANUAL) is None
    replacement = page._service.probe(path,CensorOptions(detect_visual=False))
    page._on_item(replacement)
    assert category(page._results[str(path)]) == 'review'
    assert page._service.store.label_history(str(path))
    page.deleteLater()
