"""Generic evidence capture and review interaction regressions."""
from threading import Event
import sys

import pytest
from PySide6.QtCore import QThread, QItemSelectionModel

from censor import CensorOptions, CensorService, CensorState
from censor.base import ProbeContext
from censor.models import EvidenceSource
from censor.mosaic_cv import MosaicCvDetector
from censor.sampling import FrameSampler, SampledFrame
from censor.sampling import SamplingTimeout, SamplingError
from config import ToolkitConfig


@pytest.fixture
def page(qapp, qtbot, monkeypatch):
    from ui.pages.censorship import CensorshipPage
    widget = CensorshipPage(ToolkitConfig(censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(widget)
    monkeypatch.setattr(widget.metadata_controller, 'schedule', lambda: None)
    yield widget
    widget.shutdown()
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(widget), timeout=5000)


@pytest.mark.parametrize('cooperative', [True, False])
def test_only_metadata_sync_preserves_manual_review_buttons(page, qtbot, cooperative):
    from ui.services import TaskManager
    gate = Event()
    class Worker(QThread):
        def run(self):
            gate.wait(5)
        def stop(self):
            gate.set()
    worker = Worker()
    worker.setProperty('taskPreserveReview', cooperative)
    manager = TaskManager.instance()
    try:
        assert manager.submit(worker, page, 'synthetic background operation', mutating=True)
        assert page.btn_uncensored.isEnabled() is cooperative
        assert page.btn_censored.isEnabled() is cooperative
        assert not page.evidence_count.isEnabled()
    finally:
        gate.set()
        worker.wait(3000)
        qtbot.waitUntil(lambda: not manager.running(page), timeout=5000)


def test_deep_check_uses_all_selected_visible_videos(page, monkeypatch):
    from censor import VideoVerdict
    for name in ('a.mp4', 'b.mp4', 'c.mp4'):
        page._on_item(VideoVerdict(path=name))
    page.table.selectRow(0)
    page.table.selectionModel().select(page.table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows)
    expected = page._selected_visible_paths()
    called = []
    monkeypatch.setattr(page, '_start_scan', lambda paths, **kwargs: called.append((paths, kwargs)))
    page._deepen_current()
    assert called[0][0] == expected


class SyntheticSampler:
    last_duration = 1200.
    last_sampled_count = 0
    def iter_samples(self, path, count=60, *, sample_phase=0., **kwargs):
        for index in range(count):
            self.last_sampled_count = index + 1
            yield SampledFrame((index + .5) * 1200 / count,
                80, 80, bytes([index % 230 + 10]) * 6400)
    def write_sampled_thumbnail(self, frame, out, **kwargs):
        return FrameSampler(sys.executable).write_sampled_thumbnail(frame, out, **kwargs)


def test_deep_check_retains_more_uniform_evidence_than_six_overview_frames(tmp_path, monkeypatch):
    import censor.mosaic_cv as module
    monkeypatch.setattr(module, 'analyze_frames', lambda *args, **kwargs: ([], 0.))
    ctx = ProbeContext('ordinary.mp4', duration=1200., work_dir=tmp_path,
        cfg={'censor_candidates': 6}, shared={'sampler': SyntheticSampler(),
            'requested_samples': 180, 'evidence_frames': 24, 'sample_phase': .38})
    MosaicCvDetector().detect(ctx)
    assert len(ctx.shared['candidates']) >= 18
    assert min(c.timestamp for c in ctx.shared['candidates']) < 120
    assert max(c.timestamp for c in ctx.shared['candidates']) > 1080


def test_reference_capture_preserves_manual_label_and_original_analysis(tmp_path, monkeypatch):
    path = tmp_path / 'ordinary.mp4'
    path.write_bytes(b'synthetic ordinary media')
    service = CensorService({'censor_visual_enabled': False}, data_dir=tmp_path / 'state')
    prior = service.probe(path, CensorOptions(detect_visual=False))
    prior = service.label(path, CensorState.UNCENSORED, base=prior)
    monkeypatch.setattr(service, 'tools', lambda: (sys.executable, sys.executable))
    monkeypatch.setattr(FrameSampler, 'iter_samples', SyntheticSampler().iter_samples)
    refreshed = service.refresh_evidence(path, count=12, base=prior)
    assert refreshed.state is CensorState.UNCENSORED
    assert refreshed.verdict.of(EvidenceSource.MANUAL) is not None
    assert refreshed.analyzed_frames == prior.analyzed_frames
    assert refreshed.analysis_signature == prior.analysis_signature
    assert len(refreshed.candidates) == 12
    assert service.store.get(path).state is CensorState.UNCENSORED


def test_sampling_phase_changes_actual_seek_times(monkeypatch):
    sampler = FrameSampler(sys.executable, width=80, height=80)
    seen = []
    monkeypatch.setattr(sampler, 'frame_at', lambda path, time, **kwargs:
        seen.append(time) or SampledFrame(time, 80, 80, b'a' * 6400))
    list(sampler.iter_samples('ordinary', count=12, duration=120., sample_phase=0.))
    first = list(seen)
    seen.clear()
    list(sampler.iter_samples('ordinary', count=12, duration=120., sample_phase=.38))
    assert len(seen) == len(first) == 12
    assert all(a != b for a, b in zip(first, seen))


def test_duplicate_only_reference_round_is_recorded_and_advances_phase(tmp_path, monkeypatch):
    path = tmp_path / 'ordinary.mp4'; path.write_bytes(b'synthetic fixture')
    service = CensorService({'censor_visual_enabled': False}, data_dir=tmp_path / 'state')
    prior = service.probe(path, CensorOptions(detect_visual=False))
    monkeypatch.setattr(service, 'tools', lambda: (sys.executable, sys.executable))
    phases = []
    def static_frames(self, path, count=6, *, sample_phase=0., **kwargs):
        phases.append(sample_phase)
        for index in range(count):
            yield SampledFrame(index * 10., 80, 80, b'a' * 6400)
    monkeypatch.setattr(FrameSampler, 'iter_samples', static_frames)
    first = service.refresh_evidence(path, count=6, base=prior)
    second = service.refresh_evidence(path, count=6, base=first)
    assert second.detector_runs['review.frames']['added'] == 0
    third = service.refresh_evidence(path, count=6)
    assert third.detector_runs['review.frames']['round'] == 3
    assert len(set(phases)) == 3


def test_partial_deep_sampling_retains_already_decoded_reference_image(tmp_path, monkeypatch):
    import censor.mosaic_cv as module
    monkeypatch.setattr(module, 'analyze_frames', lambda *args, **kwargs: ([], 0.))
    class Failing(SyntheticSampler):
        def iter_samples(self, path, count=60, **kwargs):
            yield SampledFrame(10., 80, 80, b'a' * 6400)
            yield SampledFrame(20., 80, 80, b'b' * 6400)
            raise SamplingTimeout('synthetic decoder timeout')
    ctx = ProbeContext('ordinary.mp4', duration=1200., work_dir=tmp_path,
        shared={'sampler': Failing(), 'requested_samples': 180, 'evidence_frames': 24})
    with pytest.raises(SamplingError, match='synthetic decoder timeout'):
        MosaicCvDetector().detect(ctx)
    assert ctx.shared.get('candidates')
    from pathlib import Path
    assert any(Path(c.thumb_path).is_file() for c in ctx.shared['candidates'])


def test_queue_accepts_more_selected_files_while_busy_and_stop_discards_them(page, monkeypatch):
    from ui.services import stop_page
    import ui.pages.censorship as module
    monkeypatch.setattr(module, 'busy', lambda owner: True)
    called = []
    monkeypatch.setattr(page, '_start_scan', lambda *args, **kwargs: called.append(args))
    page._queue_evidence_request(['a.mp4', 'b.mp4'])
    page._queue_evidence_request(['b.mp4', 'c.mp4'])
    assert sum(len(request[2]) for request in page._evidence_requests) == 3
    assert not called
    stop_page(page)
    monkeypatch.setattr(module, 'busy', lambda owner: False)
    page._pump_evidence_requests()
    assert page._evidence_requests == [] and called == []


def test_task_center_stop_all_cancels_capture_queue_during_idle_gap(page, monkeypatch):
    from ui.services import TaskManager
    import ui.pages.censorship as module
    monkeypatch.setattr(module, 'busy', lambda owner: True)
    calls = []
    monkeypatch.setattr(page, '_start_scan', lambda *args, **kwargs: calls.append(args))
    page._queue_evidence_request(['ordinary.mp4'])
    TaskManager.instance().stop_all()
    monkeypatch.setattr(module, 'busy', lambda owner: False)
    page._pump_evidence_requests()
    assert not calls and not page._evidence_requests


def test_missing_batch_is_limited_to_visible_filter_scope(page, monkeypatch):
    from censor import VideoVerdict
    for name in ('visible.mp4', 'other.mp4'):
        page._on_item(VideoVerdict(path=name))
    page.search.setText('visible')
    calls = []
    monkeypatch.setattr(page, '_start_scan', lambda paths, **kwargs: calls.append((paths, kwargs)))
    page._refresh_missing_evidence()
    assert calls == [(['visible.mp4'], dict(force=True, evidence_only=True, missing_only=True))]


@pytest.fixture
def capture_service(tmp_path, monkeypatch):
    path = tmp_path / 'ordinary.mp4'; path.write_bytes(b'ordinary synthetic fixture')
    service = CensorService({'censor_visual_enabled': False,
        'censor_sync_nfo': False, 'censor_sync_jellyfin': False}, data_dir=tmp_path / 'state')
    previous = service.probe(path, CensorOptions(detect_visual=False))
    monkeypatch.setattr(service, 'tools', lambda: (sys.executable, sys.executable))
    monkeypatch.setattr(FrameSampler, 'iter_samples', SyntheticSampler().iter_samples)
    return service, path, previous


def test_capture_keeps_partial_analysis_status_and_its_error(capture_service):
    from censor.models import ProcessingStatus
    service, path, previous = capture_service
    previous.status = ProcessingStatus.PARTIAL
    previous.error = 'synthetic original visual failure'
    # fixture 里那次 probe 已经写进一条 COMPLETED；现在要构造"基线是部分完成"的场景。
    # 注意：不能靠 put() 把 COMPLETED 覆盖成 PARTIAL —— v4 起 store 明确禁止
    # 用未完成的结果顶掉已完成的结论。
    service.store.clear_probes()
    service.store.put(previous)
    result = service.refresh_evidence(path, count=12)
    assert len(result.candidates) == 12
    assert result.status is ProcessingStatus.PARTIAL
    assert result.error == previous.error
    assert result.detector_runs['review.frames']['status'] == 'completed'
    assert service.cached_result(path, CensorOptions(detect_visual=False)) is None


def test_capture_preserves_failed_ui_attempt_and_prior_successful_cache(capture_service):
    from copy import deepcopy
    from censor.models import ProcessingStatus
    service, path, previous = capture_service
    failed = deepcopy(previous)
    failed.status, failed.error = ProcessingStatus.PARTIAL, 'synthetic most recent scan failed'
    result = service.refresh_evidence(path, count=6, base=failed)
    assert result.status is ProcessingStatus.PARTIAL
    assert result.error == failed.error
    cached = service.store.get(path)
    assert cached.status is ProcessingStatus.COMPLETED and not cached.error
    assert len(result.candidates) == len(cached.candidates) == 6
    assert service.cached_result(path, CensorOptions(detect_visual=False)) is not None


def test_capture_failure_keeps_old_images_and_analysis(capture_service, monkeypatch):
    from pathlib import Path
    service, path, previous = capture_service
    first = service.refresh_evidence(path, count=6)
    old_paths = [c.thumb_path for c in first.candidates]
    def fail(*args, **kwargs):
        raise SamplingError('synthetic decoder unavailable')
    monkeypatch.setattr(FrameSampler, 'iter_samples', fail)
    result = service.refresh_evidence(path, count=6)
    assert result.detector_runs['review.frames']['status'] == 'failed'
    assert [c.thumb_path for c in result.candidates] == old_paths
    assert all(Path(p).is_file() for p in old_paths)
    assert result.analysis_signature == previous.analysis_signature


def test_capture_changed_file_does_not_inherit_confirmation(capture_service, monkeypatch):
    from censor.models import ProcessingStatus
    service, path, previous = capture_service
    original = service.label(path, CensorState.UNCENSORED, base=previous)
    def changed(self, *args, **kwargs):
        yield SampledFrame(1., 80, 80, b'a' * 6400)
        path.write_bytes(b'replaced synthetic media')
    monkeypatch.setattr(FrameSampler, 'iter_samples', changed)
    result = service.refresh_evidence(path, count=6)
    assert result.status is ProcessingStatus.PARTIAL
    assert result.verdict.of(EvidenceSource.MANUAL) is None
    assert not result.candidates
    assert service.store.get(path).file_identity == original.file_identity
    assert not service._active_work_dirs


def test_capture_respects_new_confirmation_saved_while_it_runs(capture_service, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    service, path, previous = capture_service
    started, resume = Event(), Event()
    def frames(self, *args, **kwargs):
        started.set()
        assert resume.wait(5)
        yield SampledFrame(1., 80, 80, b'a' * 6400)
    monkeypatch.setattr(FrameSampler, 'iter_samples', frames)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.refresh_evidence, path, count=6)
        try:
            assert started.wait(5)
            service.label(path, CensorState.UNCENSORED, base=previous)
        finally:
            resume.set()
        result = future.result(timeout=5)
    assert result.state is CensorState.UNCENSORED
    assert service.store.get(path).state is CensorState.UNCENSORED


def test_capture_batch_is_bounded_and_does_not_retire_scan_jobs(capture_service, tmp_path, monkeypatch):
    from copy import deepcopy
    from threading import Lock
    import time
    from censor.jobs import ScanJobs
    from censor.models import ProcessingStatus
    from ui.censor_scan import CensorScanWorker
    service, path, previous = capture_service
    paths = [path]
    for name in ('second.mp4', 'third.mp4'):
        extra = tmp_path / name; extra.write_bytes(b'ordinary fixture'); paths.append(extra)
    jobs = ScanJobs(service.store.db_path)
    jobs.enqueue(paths)
    lock, active, peaks = Lock(), [], []
    def capture(target, **kwargs):
        with lock:
            active.append(target); peaks.append(len(active))
        try:
            time.sleep(.03)
            result = deepcopy(previous)
            result.path, result.status = target, ProcessingStatus.PARTIAL
            result.error = 'original analysis incomplete'
            result.detector_runs['review.frames'] = dict(status='completed', added=1)
            return result
        finally:
            with lock:
                active.remove(target)
    monkeypatch.setattr(service, 'refresh_evidence', capture)
    monkeypatch.setattr('ui.censor_scan.os.cpu_count', lambda: 8)
    worker = CensorScanWorker(service, list(map(str, paths)),
        CensorOptions(force=True, detect_visual=False, evidence_only=True, evidence_frames=12), workers=2)
    snapshots = []
    worker.task_status.connect(snapshots.append)
    worker.run()
    assert max(peaks) == 2
    assert snapshots[-1]['completed'] == 3 and snapshots[-1]['failed'] == 0
    assert set(jobs.pending()) == set(map(str, paths))


def test_context_menu_preserves_multiselect_for_batch_capture(page, monkeypatch):
    from censor import VideoVerdict
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMenu
    for name in ('first.mp4', 'second.mp4'):
        page._on_item(VideoVerdict(path=name))
    page.table.selectAll()
    expected = page._selected_visible_paths()
    calls = []
    monkeypatch.setattr(page, '_start_scan', lambda paths, **kwargs: calls.append(paths))
    def execute():
        menu = QApplication.activePopupWidget() or page.findChildren(QMenu)[-1]
        try:
            next(a for a in menu.actions() if a.text() == '补抓选中证据图').trigger()
        finally:
            menu.close()
    timer = QTimer(page)
    timer.setSingleShot(True)
    timer.timeout.connect(execute)
    timer.start(0)
    item = page.table.item(0, 0)
    page._show_row_menu(page.table.visualItemRect(item).center())
    assert page._selected_visible_paths() == expected
    assert calls == [expected]


def test_normal_cache_signature_ignores_new_default_evidence_controls(capture_service, monkeypatch):
    import censor.service as module
    service, path, previous = capture_service
    original = module.asdict
    def legacy_options(value):
        data = original(value)
        if isinstance(value, CensorOptions):
            data.pop('evidence_only', None)
            data.pop('evidence_frames', None)
        return data
    monkeypatch.setattr(module, 'asdict', legacy_options)
    options = CensorOptions(detect_visual=False)
    assert service._analysis_signature(path, options, service._active_detectors(options), []) == previous.analysis_signature


def test_real_ffmpeg_deep_and_reference_capture_produce_more_and_different_images(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from PySide6.QtGui import QImage
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg unavailable')
    path = tmp_path / 'ordinary.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray',
        '-s', '32x32', '-r', '2', '-i', 'pipe:0', '-c:v', 'libx264', '-qp', '0', str(path)],
        input=b''.join(bytes([index + 10]) * 1024 for index in range(240)),
        capture_output=True, check=True, timeout=20)
    service = CensorService({'censor_visual_enabled': True, 'censor_detectors': ['visual.mosaic_cv'],
        'censor_frame_samples': 6, 'censor_candidates': 6,
        'censor_sample_width': 80, 'censor_sample_height': 80,
        'censor_sync_nfo': False, 'censor_sync_jellyfin': False}, data_dir=tmp_path / 'state')
    monkeypatch.setattr(service, 'tools', lambda: (ffmpeg, ffprobe))
    ordinary = service.probe(path)
    assert len(ordinary.candidates) == 6
    service.label(path, CensorState.UNCENSORED, base=ordinary)
    options = CensorOptions(force=True, recheck_manual=True, frame_samples=24, evidence_frames=12)
    first = service.probe(path, options)
    assert len(first.candidates) > len(ordinary.candidates)
    reference = service.refresh_evidence(path, count=12)
    assert reference.detector_runs['review.frames']['added'] > 0
    assert reference.analyzed_frames == first.analyzed_frames
    second = service.probe(path, options)
    assert second.state is CensorState.UNCENSORED
    assert second.detector_runs['visual.mosaic_cv']['sample_phase'] != first.detector_runs['visual.mosaic_cv']['sample_phase']
    assert {c.timestamp for c in second.candidates} != {c.timestamp for c in first.candidates}
    assert all(not QImage(c.thumb_path).isNull() for c in second.candidates)
    assert service.store.get(path).state is CensorState.UNCENSORED


def test_metadata_pump_uses_cooperative_worker_and_never_grays_label_buttons(page, qtbot, monkeypatch):
    import censor.metadata_sync as module
    started, resume = Event(), Event()
    class Metadata:
        def __init__(self, *args): pass
        def status(self):
            return dict(done=0, pending=1, waiting=0, failed=0, stale=0)
        def run_pending(self, **kwargs):
            started.set()
            assert resume.wait(5)
            return {}
    page.cfg.censor_sync_nfo = True
    monkeypatch.setattr(module, 'MetadataSyncService', Metadata)
    monkeypatch.setattr(page.metadata_controller, '_status', lambda: dict(pending=0))
    try:
        page.metadata_controller._pump()
        assert started.wait(5)
        assert page.btn_censored.isEnabled() and page.btn_uncensored.isEnabled()
        assert page._metadata_worker.property('taskPreserveReview') is True
    finally:
        resume.set()
        page._metadata_worker.wait(3000)
        from ui.services import busy
        qtbot.waitUntil(lambda: not busy(page), timeout=5000)
    assert page.btn_censored.isEnabled() and page.btn_uncensored.isEnabled()
