import sys

from censor.base import ProbeContext
from censor.sampling import FrameSampler, SampledFrame


def test_detector_consumes_frames_incrementally():
    from censor.mosaic_cv import MosaicCvDetector
    import numpy as np
    ctx = ProbeContext('synthetic', cfg={'censor_frame_samples': 4})
    class Sampler:
        last_duration = 80.
        last_sampled_count = 0
        def sample(self, *args, **kwargs):
            raise AssertionError('batch frame retention must not be used')
        def iter_samples(self, *args, **kwargs):
            for index in range(4):
                assert ctx.shared.get('analyzed_frames', 0) == index
                self.last_sampled_count = index + 1
                yield SampledFrame(index * 20, 80, 80,
                    np.full((80, 80), 30 + index, np.uint8).tobytes(), 1)
    ctx.shared['sampler'] = Sampler()
    result = MosaicCvDetector().detect(ctx)
    assert result and ctx.shared['analyzed_frames'] == 4


def test_sampler_iterator_does_not_decode_ahead(monkeypatch):
    sampler = FrameSampler(sys.executable, width=2, height=2, pixel_format='gray')
    calls = []
    def frame(path, timestamp, **kwargs):
        calls.append(timestamp)
        return SampledFrame(timestamp, 2, 2, b'1234', 1)
    monkeypatch.setattr(sampler, 'frame_at', frame)
    frames = sampler.iter_samples('synthetic', count=60, duration=120.)
    assert calls == []
    next(frames)
    assert len(calls) == 1
    frames.close()
    assert len(calls) == 1


def test_png_evidence_does_not_start_a_subprocess(tmp_path, monkeypatch):
    from PySide6.QtGui import QImage
    sampler = FrameSampler(sys.executable, width=2, height=2, pixel_format='gray')
    def forbidden(*args, **kwargs):
        raise AssertionError('PNG evidence must use the analyzed pixels directly')
    monkeypatch.setattr(sampler, '_run_command', forbidden)
    out = tmp_path / 'frame.png'
    assert sampler.write_sampled_thumbnail(SampledFrame(0, 2, 2, b'1234', 1), out)
    image = QImage(str(out))
    assert image.width() == 2 and image.pixelColor(0, 0).red() == 49


def test_thumbnail_cleanup_keeps_referenced_evidence(tmp_path):
    from censor import CensorService, VideoVerdict
    from censor.models import FrameCandidate
    svc = CensorService(data_dir=tmp_path)
    live = svc.work_dir / 'file' / 'live' / 'frame.png'
    stale = svc.work_dir / 'file' / 'orphan' / 'frame.png'
    for path in (live, stale):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'png')
    svc.store.put(VideoVerdict(path='synthetic', candidates=[FrameCandidate(0, 40, thumb_path=str(live))]))
    result = svc.cleanup_thumbnails(max_bytes=0, min_age_seconds=0)
    assert live.exists() and not stale.exists()
    assert result['removed_bytes'] == 3


def test_cache_preparation_does_not_block_gui(qapp, qtbot, tmp_path, monkeypatch):
    import threading
    import time
    from config import ToolkitConfig
    from ui.pages.censorship import CensorshipPage
    from ui.services import TaskManager
    page = CensorshipPage(ToolkitConfig(censor_visual_enabled=False))
    qtbot.addWidget(page)
    path = tmp_path / 'ABC-123.mp4'
    path.write_bytes(b'synthetic')
    entered, release = threading.Event(), threading.Event()
    threads = []
    def cached(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        release.wait(2)
        return None
    monkeypatch.setattr(page._service, 'cached_result', cached)
    start = time.perf_counter()
    page._start_scan([str(path)])
    elapsed = time.perf_counter() - start
    release.set()
    qtbot.waitUntil(lambda: not TaskManager.instance().running(page), timeout=5000)
    assert elapsed < .5
    assert threads and all(t != threading.get_ident() for t in threads)


def test_high_resolution_scans_respect_memory_budget(qapp, tmp_path):
    from censor import CensorService, CensorOptions
    from ui.censor_scan import CensorScanWorker
    service = CensorService({'censor_sample_width': 3840, 'censor_sample_height': 2160,
        'censor_memory_mb': 64}, data_dir=tmp_path)
    worker = CensorScanWorker(service, [], CensorOptions(), workers=8)
    assert worker._workers == 1


def test_replacement_list_identity_checks_run_off_gui(qapp, qtbot, monkeypatch):
    import threading
    import time
    from config import ToolkitConfig
    from ui.pages.censor_replace import CensorReplacePage
    page = CensorReplacePage(ToolkitConfig(release_auto_lookup=False))
    qtbot.addWidget(page)
    if getattr(page, '_refresh_worker', None):
        qtbot.waitUntil(lambda: page._refresh_worker is None or not page._refresh_worker.isRunning(), timeout=4000)
    release = threading.Event()
    threads = []
    def records():
        threads.append(threading.get_ident())
        release.wait(2)
        return []
    monkeypatch.setattr(page._censor.store, 'confirmed_censored', records)
    start = time.perf_counter()
    page.refresh()
    elapsed = time.perf_counter() - start
    release.set()
    if getattr(page, '_refresh_worker', None):
        qtbot.waitUntil(lambda: page._refresh_worker is None or not page._refresh_worker.isRunning(), timeout=4000)
    assert elapsed < .5
    assert threads and all(t != threading.get_ident() for t in threads)
