"""Public settings and UI integration for generic review workload and metadata."""
from copy import deepcopy

import pytest

from config import ToolkitConfig, _coerce_config
from censor import CensorOptions, CensorService
from censor.models import CensorState, FrameCandidate


def test_group_and_sync_configuration_has_safe_persisted_defaults(tmp_path):
    cfg = ToolkitConfig()
    assert cfg.censor_triage_enabled
    assert cfg.censor_triage_prefixes == ['FC2']
    assert cfg.censor_triage_samples == 30
    assert cfg.censor_sync_nfo and cfg.censor_sync_jellyfin
    assert cfg.censor_sync_tag_censored != cfg.censor_sync_tag_uncensored
    cfg.censor_triage_prefixes = ['FC2', 'DEMO']
    cfg.censor_sync_jellyfin = False
    cfg.save()
    loaded = ToolkitConfig.load()
    assert loaded.censor_triage_prefixes == ['FC2', 'DEMO']
    assert not loaded.censor_sync_jellyfin


def test_group_and_managed_tag_settings_are_normalized():
    raw = _coerce_config({'censor_triage_prefixes': [' fc2 ', 'FC2', None, '../all', 'DEMO'],
        'censor_triage_samples': 1, 'censor_sync_tag_censored': '',
        'censor_sync_tag_uncensored': '\n'})
    assert raw['censor_triage_prefixes'] == ['FC2', 'DEMO']
    assert raw['censor_triage_samples'] >= 24
    assert raw['censor_sync_tag_censored'].strip()
    assert raw['censor_sync_tag_uncensored'].strip()


def test_sync_display_settings_do_not_invalidate_completed_analysis(tmp_path):
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'synthetic')
    cfg = ToolkitConfig(censor_visual_enabled=False)
    service = CensorService(cfg, data_dir=tmp_path / 'state')
    options = CensorOptions(detect_visual=False)
    service.probe(path, options)
    cfg.censor_sync_nfo = not cfg.censor_sync_nfo
    cfg.censor_sync_tag_censored = 'My positive label'
    assert service.cached_result(path, options) is not None


def test_metadata_change_after_confirmation_retains_exact_visual_evidence(tmp_path):
    from censor.base import BaseDetector
    from censor.models import Evidence, EvidenceSource
    from censor.registry import detectors
    class Visual(BaseDetector):
        name = 'test.review.visual'
        capabilities = ('visual',)
        def detect(self, ctx):
            ctx.duration = 120
            ctx.shared.update(candidates=[FrameCandidate(35, 12)], sampled_frames=30,
                              analyzed_frames=30, visual_score=12.)
            return [Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, detail='synthetic')]
    detectors.add(Visual.name, Visual, replace=True)
    try:
        path = tmp_path / 'generic.mp4'
        path.write_bytes(b'synthetic')
        service = CensorService({'censor_detectors': [Visual.name]}, data_dir=tmp_path / 'state')
        first = service.probe(path)
        service.label(path, CensorState.UNCENSORED, base=first)
        path.with_suffix('.nfo').write_text('<movie><tag>generic tag</tag></movie>', encoding='utf-8')
        result = service.probe(path)
        assert result.state is CensorState.UNCENSORED
        assert [c.timestamp for c in result.candidates] == [35]
        assert result.duration == 120
        assert result.detector_runs[Visual.name]['status'] == 'retained_manual'
    finally:
        detectors.discard(Visual.name)


def test_review_settings_expose_group_and_sync_controls(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig())
    qtbot.addWidget(page)
    controls = page.review_settings
    assert controls.triage_enabled.isChecked()
    assert controls.triage_prefixes.text() == 'FC2'
    controls.triage_prefixes.setText('FC2, DEMO')
    controls.sync_server.setChecked(False)
    controls.apply(page.cfg)
    assert page.cfg.censor_triage_prefixes == ['FC2', 'DEMO']
    assert not page.cfg.censor_sync_jellyfin


def test_gui_confirmation_enqueues_and_background_syncs_without_blocking(qapp, qtbot, tmp_path):
    from ui.pages.censorship import CensorshipPage
    cfg = ToolkitConfig(censor_visual_enabled=False, censor_sync_jellyfin=False)
    page = CensorshipPage(cfg)
    qtbot.addWidget(page)
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'synthetic media')
    first = page._service.probe(path, CensorOptions(detect_visual=False))
    page._on_item(first)
    page.table.selectRow(0)
    page._on_row_selected()
    page._label(CensorState.UNCENSORED)
    qtbot.waitUntil(lambda: path.with_suffix('.nfo').exists(), timeout=7000)
    qtbot.waitUntil(lambda: not page._metadata_worker or not page._metadata_worker.isRunning(), timeout=5000)
    assert '无码' in path.with_suffix('.nfo').read_text(encoding='utf-8')
    assert page._service.metadata_sync().status()['done'] == 1
    assert page.sync_status.text()
    page.shutdown()


def test_stopping_metadata_pump_does_not_immediately_restart_pending_jobs(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    page = CensorshipPage(ToolkitConfig())
    qtbot.addWidget(page)
    page.metadata_controller.pause()
    page.metadata_controller._pump()
    assert page._metadata_worker is None


def test_batch_confirmation_is_limited_to_selected_paths_and_persists(qapp, qtbot, tmp_path):
    from ui.pages.censorship import CensorshipPage
    from PySide6.QtWidgets import QAbstractItemView
    page = CensorshipPage(ToolkitConfig(censor_visual_enabled=False,
        censor_sync_nfo=False, censor_sync_jellyfin=False))
    qtbot.addWidget(page)
    assert page.table.selectionMode() == QAbstractItemView.ExtendedSelection
    paths = []
    for index in range(3):
        path = tmp_path / f'generic-{index}.mp4'
        path.write_bytes(b'synthetic')
        paths.append(str(path))
        page._on_item(page._service.probe(path, CensorOptions(detect_visual=False)))
    page._start_batch_confirmation(paths[:2], CensorState.UNCENSORED)
    qtbot.waitUntil(lambda: not page._batch_review_worker.isRunning(), timeout=5000)
    qtbot.waitUntil(lambda: all(page._results[p].state is CensorState.UNCENSORED for p in paths[:2]), timeout=5000)
    assert page._results[paths[2]].state is CensorState.UNKNOWN
    assert set(page._service.store.labels()) == set(paths[:2])
    page.shutdown()


def test_batch_cancel_retains_only_the_completed_confirmation(qapp, tmp_path):
    from ui.review_controls import BatchConfirmationWorker
    service = CensorService({'censor_visual_enabled': False}, data_dir=tmp_path / 'state')
    items = []
    for index in range(3):
        path = tmp_path / f'generic-{index}.mp4'
        path.write_bytes(b'synthetic')
        items.append(service.probe(path, CensorOptions(detect_visual=False)))
    worker = BatchConfirmationWorker(service, items, CensorState.UNCENSORED)
    original = worker.service.label
    def one_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        worker.stop()
        return result
    worker.service.label = one_then_stop
    reports = []
    worker.result.connect(reports.append)
    worker.run()
    assert reports[0]['cancelled'] and reports[0]['completed'] == 1
    assert set(service.store.labels()) == {items[0].path}
