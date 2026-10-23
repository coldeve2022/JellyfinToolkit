from config import ToolkitConfig
from censor import CensorService, CensorOptions, CensorState
from censor.releases import ReleaseService
from tests.test_system_optimization_release import Source


def test_common_workflow_uses_scope_and_manual_confirmation(tmp_path):
    from censor.workflow import run_censor_workflow
    cfg = ToolkitConfig(censor_visual_enabled=False, release_sources=["fake"])
    censor = CensorService(cfg, data_dir=tmp_path)
    source = Source()
    release = ReleaseService(cfg, data_dir=tmp_path, factory=lambda _: source)
    paths = []
    for name in ("ABC-123.mp4", "DEF-456.mp4"):
        path = tmp_path / name
        path.write_bytes(b"synthetic")
        result = censor.probe(path, CensorOptions(detect_visual=False))
        censor.label(path, CensorState.CENSORED, base=result)
        paths.append(str(path))
    report = run_censor_workflow(cfg, paths[:1], scan=True, lookup=True,
        censor_service=censor, release_service=release, log=lambda _: None)
    assert report["status"] == "succeeded"
    assert report["scan"]["completed"] == 1
    assert report["lookup"]["completed"] == 1
    assert release.cached("ABC-123") is not None and release.cached("DEF-456") is None


def test_workflow_cancel_does_not_start_lookup(tmp_path):
    from censor.workflow import run_censor_workflow
    source = Source()
    cfg = ToolkitConfig(release_sources=["fake"])
    report = run_censor_workflow(cfg, [], scan=True, lookup=True, should_stop=lambda: True,
        censor_service=CensorService(cfg, data_dir=tmp_path),
        release_service=ReleaseService(cfg, data_dir=tmp_path, factory=lambda _: source),
        log=lambda _: None)
    assert report["status"] == "cancelled" and source.calls == 0


def test_automation_template_preserves_detection_and_lookup_options(qapp, qtbot, tmp_path):
    from ui.pages.automation import AutomationPage
    page = AutomationPage(ToolkitConfig())
    qtbot.addWidget(page)
    page.cb_pixelation_scan.setChecked(True)
    page.cb_release_lookup.setChecked(True)
    path = tmp_path / "workflow.json"
    page._save_workflow(str(path))
    page.cb_pixelation_scan.setChecked(False)
    page.cb_release_lookup.setChecked(False)
    page._load_workflow(str(path))
    assert page._options(False)["pixelation_scan"] and page._options(False)["release_lookup"]
