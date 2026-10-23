"""Adversarial regressions from the independent final review."""
import time

from config import ToolkitConfig
from censor import CensorService, CensorOptions
from censor.models import ProcessingStatus
from censor.releases.models import ReleaseCandidate, MagnetLink, ReleaseReport, Availability


def test_plain_magnet_never_inherits_neighbor_version_tag():
    from censor.releases.javdb import JavDbSource
    page = '''<div class="item" data-size="9000"><a href="magnet:?xt=plain"><span class="name">ABC-123</span></a></div>
    <div class="item" data-size="4000"><a href="magnet:?xt=clean"><span class="name">ABC-123 Uncensored</span></a><span class="tag">Uncensored</span></div>'''
    magnets = JavDbSource._parse_magnets(page)
    assert magnets[0].tags == ()
    candidate = ReleaseCandidate('javdb', number='ABC-123', uncensored=True, magnets=tuple(magnets))
    assert candidate.best_magnet.uri == 'magnet:?xt=clean'


def test_unmarked_magnet_does_not_become_a_verified_version():
    candidate = ReleaseCandidate('javdb', uncensored=True,
        magnets=(MagnetLink('magnet:?xt=unknown', name='ABC-123'),))
    report = ReleaseReport(candidates=(candidate,))
    assert candidate.best_magnet is None
    assert not report.has_download


def test_cached_lookup_finishes_a_requeued_work(tmp_path):
    from censor.releases import ReleaseService
    from tests.test_system_optimization_release import Source
    service = ReleaseService({'release_sources': ['fake']}, data_dir=tmp_path, factory=lambda _: Source())
    service.lookup('ABC-123')
    service.queue(['ABC-123'])
    assert service.lookup('ABC-123').cache_hit
    assert service.pending() == []


def test_configured_ttl_is_used_by_default_lookup(tmp_path):
    from censor.releases import ReleaseService
    from tests.test_system_optimization_release import Source
    source = Source()
    service = ReleaseService({'release_sources': ['fake'], 'release_cache_days': 1},
        data_dir=tmp_path, factory=lambda _: source)
    report = service.lookup('ABC-123')
    report.queried_at = time.time() - 2 * 86400
    service.store.put(report, service.signature())
    assert service.fresh('ABC-123') is None
    assert not service.lookup('ABC-123').cache_hit


def test_supjav_partial_engine_failure_keeps_negative_incomplete(tmp_path, monkeypatch):
    from censor.releases import ReleaseService
    from censor.releases.supjav import SupjavSource
    from tests.test_system_optimization_release import Source
    monkeypatch.setattr('censor.releases.supjav.lookup_site', lambda *a, **kw:
        ([], {'bing': '返回 0 条，其中本站 0 条', 'duckduckgo': '失败：timeout'}))
    index = SupjavSource(object())
    service = ReleaseService({'release_sources': ['normal', 'supjav']}, data_dir=tmp_path,
        factory=lambda name: index if name == 'supjav' else Source(uncensored=False))
    report = service.lookup('ABC-123')
    assert report.status is ProcessingStatus.PARTIAL
    assert report.availability is Availability.UNKNOWN
    assert 'duckduckgo' in report.sources['supjav']


def test_queued_old_scan_cannot_undo_media_invalidation(qapp, qtbot, tmp_path):
    from ui.pages.censorship import CensorshipPage
    from tests.test_system_optimization_state import scanned
    path, svc, result = scanned(tmp_path)
    page = CensorshipPage(svc.cfg)
    qtbot.addWidget(page)
    page._on_item(result)
    page._queue_item(result)
    path.write_bytes(b'replacement synthetic version')
    page._media_changed([str(path)])
    page._drain_results()
    assert page._results[str(path)].status is ProcessingStatus.PARTIAL


def test_scan_uses_its_captured_tool_configuration(qapp, tmp_path, monkeypatch):
    from ui.censor_scan import CensorScanWorker
    seen = []
    def resolve(name, **kwargs):
        seen.append(kwargs.get('configured_path'))
        return 'resolved/' + name
    monkeypatch.setattr('utils.tools.resolve_tool', resolve)
    cfg = ToolkitConfig(merge_ffmpeg_path='original/ffmpeg.exe')
    worker = CensorScanWorker(CensorService(cfg, data_dir=tmp_path), [], CensorOptions())
    cfg.merge_ffmpeg_path = 'changed/ffmpeg.exe'
    worker._service.tools()
    assert seen == ['original/ffmpeg.exe', 'original/ffmpeg.exe']


def test_replacement_can_schedule_verification(qapp, qtbot, tmp_path, monkeypatch):
    from ui.pages.censorship import CensorshipPage
    path = tmp_path / 'ABC-123.mp4'
    path.write_bytes(b'replaced synthetic media')
    cfg = ToolkitConfig(censor_verify_replacements=True)
    page = CensorshipPage(cfg)
    qtbot.addWidget(page)
    requests = []
    monkeypatch.setattr(page, '_start_scan', lambda paths, **kw: requests.append((paths, kw)))
    page._media_changed([str(path)])
    qtbot.waitUntil(lambda: bool(requests), timeout=2000)
    assert requests == [([str(path)], {'force': True})]


def test_metadata_error_does_not_erase_successful_file_operation(qapp, tmp_path, monkeypatch):
    from ui.pages.replace import ReplaceWorker
    operation = {'ok': True, 'target': str(tmp_path / 'ABC-123.mp4')}
    monkeypatch.setattr('utils.replacement.execute_replacements', lambda *a, **kw:
        {'ok': 1, 'fail': 0, 'operations': [operation]})
    worker = ReplaceWorker([], [])
    worker.plan = []
    def fail(_):
        raise OSError('metadata unavailable')
    monkeypatch.setattr(worker, '_sync_censor_metadata', fail)
    finished = []
    worker.finished.connect(lambda ok, fail: finished.append((ok, fail)))
    worker.run()
    assert worker.stats['ok'] == 1
    assert worker.stats['operations'] == [operation]
    assert worker.stats['metadata_failed'] == 1
    assert finished == [(1, 0)]


def test_shared_settings_reload_saved_values(qapp, qtbot):
    from ui.release_settings import ReleaseSettings
    cfg = ToolkitConfig(release_proxy='http://old.invalid')
    first, second = ReleaseSettings(cfg), ReleaseSettings(cfg)
    qtbot.addWidget(first)
    qtbot.addWidget(second)
    first.proxy.setText('http://new.invalid')
    first.apply(cfg)
    assert second.values()['release_proxy'] == 'http://new.invalid'


def test_magnet_must_belong_to_the_requested_work():
    candidate = ReleaseCandidate('javdb', number='ABC-123', uncensored=True,
        magnets=(MagnetLink('magnet:?xt=other', name='DEF-456 Uncensored'),))
    assert candidate.best_magnet is None


def test_javbus_negative_category_is_not_uncensored(monkeypatch):
    from censor.releases.javbus import JavBusSource
    class Transport:
        def get(self, *a, **kw):
            return '<p><span class="header">識別碼:</span> ABC-123</p>'
    source = JavBusSource(Transport())
    monkeypatch.setattr(source, '_categories', lambda _: ('非无码',))
    candidate = source._fetch_movie('ABC-123', '', lambda _: None)
    assert not candidate.uncensored


def test_empty_javdb_detail_is_incomplete(tmp_path, monkeypatch):
    from censor.releases.javdb import JavDbSource
    from censor.releases import ReleaseService
    class Transport:
        def get(self, url, **kw):
            return '<a href="/v/one" class="box" title="ABC-123"><strong>ABC-123</strong></a>' if '/search?' in url else ''
    service = ReleaseService({'release_sources': ['javdb']}, data_dir=tmp_path,
        factory=lambda _: JavDbSource(Transport()))
    report = service.lookup('ABC-123')
    assert report.status is ProcessingStatus.FAILED
    assert report.availability is Availability.UNKNOWN


def test_sorted_scan_updates_preserve_hidden_identity(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    from censor.models import VideoVerdict
    from PySide6.QtCore import Qt
    page = CensorshipPage(ToolkitConfig())
    qtbot.addWidget(page)
    page.search.setText('AAA')
    page._on_item(VideoVerdict(path='AAA.mp4', visual_score=1))
    page._on_item(VideoVerdict(path='BBB.mp4', visual_score=20))
    page.table.sortItems(4, Qt.AscendingOrder)
    page._on_item(VideoVerdict(path='AAA.mp4', visual_score=90))
    assert {page.table.item(i, 0).text() for i in range(2) if not page.table.isRowHidden(i)} == {'AAA.mp4'}


def test_release_updates_reapply_state_filter(qapp, qtbot):
    from ui.pages.censor_replace import CensorReplacePage, NumberRow
    page = CensorReplacePage(ToolkitConfig())
    qtbot.addWidget(page)
    page._rows = {'ABC123': NumberRow('ABC-123', ['synthetic'], ReleaseReport(number='ABC123', availability=Availability.NONE))}
    page._fill_table()
    page.state_filter.setCurrentIndex(page.state_filter.findData('none'))
    page._on_report(ReleaseReport(number='ABC123', availability=Availability.CONFIRMED))
    assert page.table.isRowHidden(0)
    assert page._current_key == ''


def test_bulk_lookup_target_preparation_runs_off_gui(qapp, qtbot, monkeypatch):
    import threading
    from ui.pages.censor_replace import CensorReplacePage, NumberRow
    page = CensorReplacePage(ToolkitConfig())
    qtbot.addWidget(page)
    page._rows = {'ABC123': NumberRow('ABC-123', ['synthetic'])}
    seen = []
    def fresh(*a, **kw):
        seen.append(threading.get_ident())
        return {}
    monkeypatch.setattr(page._release, 'fresh_many', fresh, raising=False)
    def start(*, force, numbers=None):
        if numbers is not None:
            return
        CensorReplacePage._lookup(page, force=force, numbers=numbers)
    monkeypatch.setattr(page, '_lookup', start)
    page._lookup(force=False)
    qtbot.waitUntil(lambda: bool(seen))
    assert all(t != threading.get_ident() for t in seen)


def test_old_cache_preparation_is_discarded_after_config_change(qapp, qtbot, tmp_path, monkeypatch):
    import threading
    from ui.pages.censorship import CensorshipPage
    from ui.services import busy
    from tests.test_system_optimization_state import scanned
    path, svc, result = scanned(tmp_path)
    page = CensorshipPage(svc.cfg)
    qtbot.addWidget(page)
    entered, release = threading.Event(), threading.Event()
    def partition(*a, **kw):
        entered.set()
        release.wait(2)
        return [], [result]
    monkeypatch.setattr(page, '_partition_reusable', partition)
    page._start_scan([str(path)])
    qtbot.waitUntil(entered.is_set)
    page.cfg.censor_frame_samples += 1
    release.set()
    qtbot.waitUntil(lambda: not busy(page))
    assert str(path) not in page._results
