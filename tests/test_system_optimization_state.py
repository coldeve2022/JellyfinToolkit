import pytest

from config import ToolkitConfig
from censor import CensorService, CensorOptions, CensorState
from censor.identity import identify_file
from censor.releases.models import ReleaseReport
from censor.releases.store import ReleaseStore


def scanned(tmp_path):
    path = tmp_path / "ABC-123.mp4"
    path.write_bytes(b"synthetic media")
    cfg = ToolkitConfig(censor_visual_enabled=False)
    svc = CensorService(cfg, data_dir=tmp_path / "state")
    result = svc.probe(path, CensorOptions(detect_visual=False))
    return path, svc, result


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_stale_file_confirmation_is_not_an_active_replacement_target(tmp_path, change):
    path, svc, result = scanned(tmp_path)
    svc.label(path, CensorState.CENSORED, base=result)
    assert len(svc.store.confirmed_censored()) == 1
    if change == "replace":
        path.write_bytes(b"different synthetic media version")
    else:
        path.unlink()
    assert svc.store.confirmed_censored() == []
    assert svc.store.label_history(str(path)), "历史不能因文件变化被删除"


def test_workers_do_not_invalidate_but_threshold_does(tmp_path):
    path, svc, _ = scanned(tmp_path)
    opts = CensorOptions(detect_visual=False)
    svc.cfg.censor_workers = 7
    assert svc.cached_result(path, opts) is not None
    svc.cfg.censor_strong_score += 1
    assert svc.cached_result(path, opts) is None


def test_tool_resolution_refreshes_when_configuration_changes(tmp_path, monkeypatch):
    _, svc, _ = scanned(tmp_path)
    monkeypatch.setattr("utils.tools.resolve_tool", lambda name, **kw: "old/" + name)
    assert svc.tools()[0] == "old/ffmpeg"
    svc.cfg.merge_ffmpeg_path = "new/ffmpeg"
    monkeypatch.setattr("utils.tools.resolve_tool", lambda name, **kw: "new/" + name)
    assert svc.tools()[0] == "new/ffmpeg"


def test_withdraw_label_preserves_history_and_requires_new_confirmation(tmp_path):
    path, svc, result = scanned(tmp_path)
    svc.label(path, CensorState.CENSORED, base=result)
    svc.remove_label(path)
    assert svc.store.get_label(str(path), file_identity=identify_file(path).serialize()) is None
    assert not svc.store.confirmed_censored()
    assert svc.store.label_history(str(path))
    svc.label(path, CensorState.UNCENSORED)
    assert svc.store.get_label(str(path), file_identity=identify_file(path).serialize())[0] is CensorState.UNCENSORED


@pytest.mark.parametrize("raw", ["[]", '{"candidates":[1]}', '{"sources":true}',
                                   '{"candidates":[{"magnets":[null]}]}'])
def test_malformed_cache_row_does_not_break_other_reports(tmp_path, raw):
    store = ReleaseStore(tmp_path / "release.db")
    store.put(ReleaseReport(number="BAD123"))
    store.put(ReleaseReport(number="GOOD123"))
    with store._connect() as conn:
        conn.execute("UPDATE release_report SET report=? WHERE number='BAD123'", (raw,))
    assert store.get("BAD123") is None
    assert store.get("GOOD123") is not None
