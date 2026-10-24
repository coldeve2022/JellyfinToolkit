from pathlib import Path

from config import ToolkitConfig
from censor import CensorService, CensorOptions, CensorState


def test_candidate_pairing_requires_a_unique_work_and_compatible_container(tmp_path):
    from utils.replacement import preview_release_replacements
    original = tmp_path / "library" / "ABC-123.mp4"
    original.parent.mkdir()
    original.write_bytes(b"original")
    candidate = tmp_path / "download" / "ABC-123-Uncensored.mp4"
    candidate.parent.mkdir()
    candidate.write_bytes(b"replacement")
    plan = preview_release_replacements([str(candidate)], [str(original)])
    assert len(plan) == 1 and plan[0]["status"] == "ready"
    assert plan[0]["target"] == str(original)
    other = original.with_name("ABC-123-copy.mp4")
    other.write_bytes(b"another version")
    assert preview_release_replacements([str(candidate)], [str(original), str(other)])[0]["status"] == "ambiguous"
    mkv = original.with_suffix(".mkv")
    mkv.write_bytes(b"different container")
    assert preview_release_replacements([str(candidate)], [str(mkv)])[0]["status"] == "container_convert"


def test_candidate_plan_uses_recoverable_executor_and_invalidates_old_confirmation(tmp_path):
    from utils.replacement import preview_release_replacements, execute_replacements
    from utils.file_ops import OperationJournal
    original, candidate = tmp_path / "ABC-123.mp4", tmp_path / "ABC-123-Uncensored.mp4"
    original.write_bytes(b"old synthetic version")
    candidate.write_bytes(b"new synthetic version with different content")
    censor = CensorService(ToolkitConfig(censor_visual_enabled=False), data_dir=tmp_path / "state")
    result = censor.probe(original, CensorOptions(detect_visual=False))
    censor.label(original, CensorState.CENSORED, base=result)
    plan = preview_release_replacements([str(candidate)], [str(original)])
    stats = execute_replacements(plan, journal=OperationJournal(tmp_path / "operations.db"),
                                 validator=lambda _: (True, ""), log=lambda _: None)
    assert stats["ok"] == 1
    assert original.read_bytes() == b"new synthetic version with different content"
    assert censor.store.confirmed_censored() == []
    assert censor.store.label_history(str(original))
    assert Path(stats["operations"][0]["target"]) == original


def test_replace_page_receives_candidates_in_identifier_mode(qapp, qtbot, tmp_path):
    from ui.pages.replace import ReplacePage
    from ui.services import TaskManager
    page = ReplacePage(ToolkitConfig())
    qtbot.addWidget(page)
    original, candidate = tmp_path / "ABC-123.mp4", tmp_path / "ABC-123-Uncensored.mp4"
    original.write_bytes(b"old")
    candidate.write_bytes(b"new")
    page.receive_candidates([str(candidate)], [str(original)])
    qtbot.waitUntil(lambda: page._plan is not None, timeout=4000)
    qtbot.waitUntil(lambda: not TaskManager.instance().running(page), timeout=4000)
    assert page._plan[0]["status"] == "ready"
