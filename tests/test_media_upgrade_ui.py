from copy import deepcopy
from pathlib import Path

from config import ToolkitConfig


def wait_page(qtbot, page):
    from ui.services import busy
    qtbot.waitUntil(lambda: not busy(page), timeout=5000)


def test_receive_originals_only_queues_in_generic_mode(qapp, qtbot, tmp_path, monkeypatch):
    from ui.pages.replace import ReplacePage
    page = ReplacePage(ToolkitConfig()); qtbot.addWidget(page)
    original = tmp_path / 'ABC-123.mp4'; original.write_bytes(b'old')
    page.receive_originals([str(original)])
    assert page.match_mode.currentData() == 'upgrade'
    assert page.list_library.get_all_paths() == [str(original)]
    assert page.list_restored.count() == 0 and page._plan is None
    assert not page.btn_start.isEnabled() and original.read_bytes() == b'old'


def test_generic_prepare_is_separate_and_config_invalidates_snapshot(qapp, qtbot, tmp_path, monkeypatch):
    import ui.pages.replace as module
    from ui.services import events
    calls = []
    class Service:
        def __init__(self, cfg, data_dir=None):
            self.cfg = deepcopy(cfg)
        def preview(self, candidates, originals, cancel=None):
            calls.append('preview')
            return {'rows': [{'source': candidates[0], 'target': originals[0], 'identifier': 'ABC-123', 'status': 'ready', 'candidates': [originals[0]], 'inheritance_summary': 'local'}]}
        def prepare(self, plan, cancel=None):
            calls.append('prepare'); return dict(plan, snapshot_id='a'*32, snapshot_path=str(tmp_path / 'fixture.json'), snapshot_sha256='hash')
        def execute(self, *args, **kwargs):
            calls.append('execute'); raise AssertionError('prepare must never execute')
    monkeypatch.setattr(module, 'MediaUpgradeService', Service)
    page = module.ReplacePage(ToolkitConfig()); qtbot.addWidget(page)
    original = tmp_path / 'ABC-123 original.mp4'; candidate = tmp_path / 'ABC-123 better.mp4'
    original.write_bytes(b'old'); candidate.write_bytes(b'new')
    page.receive_originals([str(original)]); page._add_to_restored([str(candidate)])
    page._preview(); wait_page(qtbot, page)
    assert calls == ['preview'] and page.btn_prepare.isEnabled() and not page.btn_start.isEnabled()
    page._prepare_upgrade(); wait_page(qtbot, page)
    assert calls == ['preview', 'prepare'] and page.btn_start.isEnabled()
    assert page.pairs.columnCount() >= 5
    events.config_changed.emit()
    assert page._prepared is None and not page.btn_start.isEnabled()
    assert original.read_bytes() == b'old'


def test_generic_worker_does_not_call_legacy_metadata_updater(qapp, qtbot, monkeypatch, tmp_path):
    import ui.pages.replace as module
    calls = []
    class Service:
        def __init__(self, cfg, data_dir=None): pass
        def execute(self, prepared, cancel=None, log=print):
            calls.append('generic'); return {'ok': 0, 'fail': 0, 'needs_attention': 1, 'rows': [], 'operations': []}
    monkeypatch.setattr(module, 'MediaUpgradeService', Service)
    monkeypatch.setattr(module.ReplaceWorker, '_sync_censor_metadata', lambda *a: calls.append('legacy'))
    worker = module.UpgradeWorker(ToolkitConfig(), tmp_path, 'execute', {'rows': []})
    with qtbot.waitSignal(worker.finished, timeout=5000):
        worker.start()
    assert calls == ['generic']


def test_generic_nav_label_and_main_transfer_wiring(qapp, qtbot, monkeypatch):
    import main
    from ui.main_window import MainWindow
    from ui.services import events
    assert MainWindow.NAV_ITEMS[10][1] == '视频版本管理'
    seen = []
    class Page:
        def receive_originals(self, paths): seen.append(paths)
    class Window:
        def _navigate(self, index): seen.append(index)
    callback = main._wire_media_upgrades(Window(), Page())
    try:
        events.media_upgrade_requested.emit(['fixture.mp4'])
        assert seen == [['fixture.mp4'], 10]
    finally:
        events.media_upgrade_requested.disconnect(callback)


def test_generic_confirm_defaults_to_no_and_never_executes_on_decline(qapp, qtbot, monkeypatch):
    import ui.pages.replace as module
    page = module.ReplacePage(ToolkitConfig()); qtbot.addWidget(page)
    page._prepared = {'rows': [{'source': 'candidate.mp4', 'target': 'original.mp4', 'status': 'ready'}]}
    page._prepared_context = page._context()
    calls = []
    def decline(*args):
        assert args[-1] == module.QMessageBox.No
        calls.append('confirmed-no')
        return module.QMessageBox.No
    monkeypatch.setattr(module.QMessageBox, 'question', decline)
    monkeypatch.setattr(page, '_run_upgrade', lambda *a: calls.append('executed'))
    page._start()
    assert calls == ['confirmed-no']


def test_execute_reports_mutations_even_when_latest_reference_cannot_be_saved(qapp, qtbot, monkeypatch, tmp_path):
    import ui.pages.replace as module
    class Service:
        def __init__(self, cfg, data_dir=None): pass
        def execute(self, prepared, cancel=None, log=print):
            return {'ok': 1, 'rows': [], 'operations': [{'target': 'fixture.mp4', 'ok': True}]}
    def denied(*args): raise OSError('fixture permission denied')
    monkeypatch.setattr(module, 'MediaUpgradeService', Service)
    monkeypatch.setattr(module, '_save_upgrade_reference', denied)
    worker = module.UpgradeWorker(ToolkitConfig(), tmp_path, 'execute', {})
    seen = []; worker.result.connect(seen.append)
    with qtbot.waitSignal(worker.finished, timeout=5000):
        worker.start()
    assert seen and seen[0]['operations'][0]['target'] == 'fixture.mp4'


def test_saved_snapshot_reload_is_verify_only(qapp, qtbot, tmp_path):
    from ui.pages.replace import ReplacePage
    cfg = ToolkitConfig(jellyfin_server='', jellyfin_api_key='')
    page = ReplacePage(cfg); qtbot.addWidget(page)
    original = tmp_path / 'ABC-123 original.mp4'; candidate = tmp_path / 'ABC-123 higher.mp4'
    original.write_bytes(b'old'); candidate.write_bytes(b'new')
    page.receive_originals([str(original)]); page._add_to_restored([str(candidate)])
    page._preview(); wait_page(qtbot, page)
    page._prepare_upgrade(); wait_page(qtbot, page)
    assert page._prepared and page.btn_start.isEnabled()
    other = ReplacePage(cfg); qtbot.addWidget(other)
    other._reload_upgrade(); wait_page(qtbot, other)
    assert other._prepared['snapshot_id'] == page._prepared['snapshot_id']
    assert other.btn_verify.isEnabled() and not other.btn_start.isEnabled()
    assert original.read_bytes() == b'old' and candidate.read_bytes() == b'new'


def test_stale_preview_result_is_discarded_after_configuration_change(qapp, qtbot):
    from ui.pages.replace import ReplacePage
    from ui.services import events
    page = ReplacePage(ToolkitConfig()); qtbot.addWidget(page)
    context = page._context()
    events.config_changed.emit()
    page._upgrade_result('preview', {'rows': [{'status': 'ready', 'source': 'a', 'target': 'b'}]}, context)
    assert page._plan is None and page.pairs.rowCount() == 0
