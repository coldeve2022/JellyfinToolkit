from config import ToolkitConfig
from censor import CensorService, CensorOptions, CensorState
from censor.releases import ReleaseService
from tests.test_system_optimization_release import Source


def test_lookup_worker_reports_failure_to_task_center(qapp, qtbot, tmp_path):
    from ui.censor_release import ReleaseLookupWorker
    from ui.services import TaskManager
    from PySide6.QtWidgets import QWidget
    owner = QWidget()
    qtbot.addWidget(owner)
    manager = TaskManager()
    worker = ReleaseLookupWorker(ReleaseService({"release_sources": ["fake"]}, data_dir=tmp_path,
        factory=lambda _: Source(fail=True)), ["ABC-123"])
    assert manager.submit(worker, owner, "检索")
    qtbot.waitUntil(lambda: not manager.running(), timeout=4000)
    assert manager.records[-1]["status"] == "failed"
    assert worker.stats["fail"] == 1


def test_manual_confirmation_can_start_automatic_lookup(qapp, qtbot, tmp_path, monkeypatch):
    from ui.pages.censor_replace import CensorReplacePage
    from ui.services import events, TaskManager
    monkeypatch.delenv("JELLYFIN_TOOLKIT_OFFLINE", raising=False)
    cfg = ToolkitConfig(censor_visual_enabled=False, release_auto_lookup=True, release_sources=["fake"])
    path = tmp_path / "ABC-123.mp4"
    path.write_bytes(b"synthetic")
    censor = CensorService(cfg)
    result = censor.probe(path, CensorOptions(detect_visual=False))
    page = CensorReplacePage(cfg)
    qtbot.addWidget(page)
    page._release = ReleaseService(cfg, factory=lambda _: Source())
    censor.label(path, CensorState.CENSORED, base=result)
    events.censor_changed.emit([str(path)])
    qtbot.waitUntil(lambda: page._rows.get("ABC123") is not None and
        page._rows["ABC123"].report is not None, timeout=20000)
    qtbot.waitUntil(lambda: not TaskManager.instance().running(page), timeout=20000)
    assert page._rows["ABC123"].report.has_download is False
    assert page._rows["ABC123"].report.availability.value == "confirmed"


def test_queue_confirmed_groups_copies_and_ignores_unconfirmed(tmp_path):
    from censor.workflow import queue_confirmed
    cfg = ToolkitConfig(censor_visual_enabled=False)
    censor, release = CensorService(cfg, data_dir=tmp_path), ReleaseService(cfg, data_dir=tmp_path)
    for name in ("ABC-123.mp4", "ABC-123-copy.mp4", "DEF-456.mp4"):
        path = tmp_path / name
        path.write_bytes(b"synthetic")
        result = censor.probe(path, CensorOptions(detect_visual=False))
        if name.startswith("ABC"):
            censor.label(path, CensorState.CENSORED, base=result)
    assert queue_confirmed(censor, release) == ["ABC-123"]
    assert release.pending() == ["ABC123"]
