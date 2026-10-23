from config import ToolkitConfig
from censor import CensorService, CensorOptions
from censor.releases import ReleaseService


def test_release_settings_edit_cancel_and_apply(qapp, qtbot):
    from ui.release_settings import ReleaseSettings
    cfg = ToolkitConfig(release_proxy="http://old.invalid", release_auto_lookup=False)
    widget = ReleaseSettings(cfg)
    qtbot.addWidget(widget)
    widget.proxy.setText("http://new.invalid")
    widget.auto.setChecked(True)
    assert cfg.release_proxy == "http://old.invalid" and cfg.release_auto_lookup is False
    widget.apply(cfg)
    assert cfg.release_proxy == "http://new.invalid" and cfg.release_auto_lookup is True
    assert ToolkitConfig.load().release_proxy == "http://new.invalid"


def test_workers_snapshot_effective_config(qapp, tmp_path):
    from ui.censor_scan import CensorScanWorker
    from ui.censor_release import ReleaseLookupWorker
    cfg = ToolkitConfig(censor_frame_samples=24, release_detail_limit=2)
    scan = CensorScanWorker(CensorService(cfg, data_dir=tmp_path), [], CensorOptions())
    lookup = ReleaseLookupWorker(ReleaseService(cfg, data_dir=tmp_path), [])
    cfg.censor_frame_samples = 180
    cfg.release_detail_limit = 9
    assert scan._service.cfg.censor_frame_samples == 24
    assert lookup._service.cfg.release_detail_limit == 2


def test_detection_settings_do_not_mutate_until_saved(qapp, qtbot):
    from ui.pages.censorship import CensorshipPage
    cfg = ToolkitConfig(censor_candidates=12)
    widget = CensorshipPage(cfg)
    qtbot.addWidget(widget)
    widget.evidence_count.setValue(18)
    widget.settings_dialog.reject()
    assert cfg.censor_candidates == 12


def test_supjav_mode_is_validated_and_falls_back():
    """手改配置把 Supjav 模式写成别的值，必须收回到默认，而不是走不可预期分支。"""
    from config import DEFAULT_CONFIG, _coerce_config

    for value in ("auto", "direct", "index"):
        assert _coerce_config({"release_supjav_mode": value})["release_supjav_mode"] == value
    for bogus in ("only-index", "", "AUTO", "Direct"):
        assert _coerce_config({"release_supjav_mode": bogus})[
            "release_supjav_mode"] == DEFAULT_CONFIG["release_supjav_mode"]


