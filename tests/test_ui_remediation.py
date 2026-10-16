"""Behavioral UI regressions; all inputs/stores are fabricated and isolated."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from config import ToolkitConfig


@pytest.fixture
def cfg(monkeypatch):
    from utils import library_source, lada_tool, whisper_tool, tools

    monkeypatch.setattr(library_source, "describe", lambda cfg: "isolated library")
    monkeypatch.setattr(library_source, "resolve", lambda *a, **k: ("db", {"path": ""}))
    monkeypatch.setattr(tools, "resolve_tool", lambda *a, **k: "")
    monkeypatch.setattr(lada_tool, "find_lada_cli", lambda *a, **k: None)
    monkeypatch.setattr(whisper_tool, "find_infer_exe", lambda *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    value = ToolkitConfig()
    value.save = lambda: None
    return value


def test_removed_manual_target_never_appears_in_execution_plan(qapp, cfg, tmp_path):
    from ui.pages.delete import DeletePage

    page = DeletePage(cfg)
    video = tmp_path / "test.mp4"
    video.write_bytes(b"fixture")
    page._add_paths([str(video)])
    page.manual_list.selectAll()
    page.manual_list._remove_selected()
    assert page.manual_list.count() == 0
    assert not page._manual_paths


def test_subtitle_removed_directory_is_not_scanned(qapp, cfg, tmp_path):
    from ui.pages.subtitle import SubtitlePage

    page = SubtitlePage(cfg)
    page._on_dirs_dropped([str(tmp_path)])
    page.dir_list.selectAll()
    page.dir_list._remove_selected()
    assert not page._directories


def test_repair_plan_uses_current_queue_including_unscanned_inputs(qapp, cfg):
    from ui.pages.repair import RepairPage

    page = RepairPage(cfg)
    page._scan_results = {
        "removed.mp4": {"is_corrupted": True},
        "good.mp4": {"is_corrupted": False, "is_compatible": True},
    }
    page.file_list.add_item("new", "new.mp4")
    assert page._select_repair_queue("auto") == ["new.mp4"]


def test_dedup_theme_preserves_cancelled_deletion(qapp, cfg):
    from ui.pages.dedup import DedupPage

    def item(id):
        return SimpleNamespace(
            id=id,
            path=id + ".mp4",
            actors=[],
            studios=[],
            genres=[],
            tags=[],
            is_uncensored=False,
            is_cd=False,
            cd_index=None,
            year=2020,
            num="TEST-123",
            size=100,
        )

    page = DedupPage(cfg)
    page.groups = [
        {"num": "TEST-123", "all_cd": False, "items": [item("a"), item("b")], "losers": {"b"}}
    ]
    page._render_tree()
    page.tree.topLevelItem(0).child(1).setCheckState(0, Qt.Unchecked)
    qapp.processEvents()
    page.apply_theme()
    assert page._collect_checked() == []


def test_whisper_default_and_edited_parameters_roundtrip(qapp, cfg):
    from ui.pages.subtitle_gen import SubtitleGenPage

    page = SubtitleGenPage(cfg)
    page.combo_model.setCurrentIndex(0)
    page._apply_params()
    assert cfg.whisper_model == ""
    page.combo_model.setCurrentIndex(1)
    page.combo_model.setEditText("custom-test-model")
    page.combo_formats.setCurrentIndex(0)
    page.combo_formats.setEditText("ass")
    page._apply_params()
    assert cfg.whisper_model == "custom-test-model"
    assert cfg.whisper_sub_formats == "ass"


def test_settings_reset_and_ffmpeg_persistence(qapp, cfg):
    from ui.pages.settings import SettingsPage

    page = SettingsPage(cfg)
    page._reset()
    page.input_ffmpeg_path.setText("X:/fixture/ffmpeg.exe")
    page._save()
    assert cfg.merge_ffmpeg_path == "X:/fixture/ffmpeg.exe"


def test_censorship_repeated_input_does_not_replace_running_thread(qapp, cfg, monkeypatch):
    from ui.pages.censorship import CensorshipPage

    page = CensorshipPage(cfg)
    active = SimpleNamespace(isRunning=lambda: True)
    page._worker = active
    page._start_scan_worker(["second-fixture"])
    assert page._worker is active


def test_automatic_write_confirmation_can_cancel(qapp, cfg, monkeypatch):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page.cb_delete.setChecked(True)
    page._start(True)
    assert page._worker is None


def test_review_percent_sort_numeric(qapp, cfg):
    from ui.pages.review import ReviewPage

    page = ReviewPage(cfg)
    page._set_table(
        page.tab_like, [("A", "A", "74%", "r", "meta", "a"), ("B", "B", "9%", "r", "meta", "b")]
    )
    values = {
        page.tab_like.item(r, 2).text(): page.tab_like.item(r, 2).data(Qt.UserRole)
        for r in range(2)
    }
    assert values == {"74%": 74.0, "9%": 9.0}


def test_insight_bucket_percent_is_population_share(qapp, cfg):
    from ui.pages.insight import InsightPage

    page = InsightPage(cfg)
    page._summary = {"total": 100}
    page._all_rows = [("分类桶", "A", 80), ("分类桶", "B", 20)]
    page._apply_filter()
    assert [page.table.item(r, 3).text() for r in range(2)] == ["80%", "20%"]


def test_generated_queue_uses_path_role(qapp, cfg, tmp_path):
    from ui.pages.subtitle_gen import SubtitleGenPage

    page = SubtitleGenPage(cfg)
    video = tmp_path / "test.mp4"
    video.write_bytes(b"fixture")
    page._add_paths([str(video)])
    assert page.list_files.get_all_paths() == [str(video)]


def test_readonly_checks_honor_checkbox(qapp, cfg):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page.cb_scan_video.setChecked(True)
    assert page._options(False)["scan_video"] is True


def test_task_manager_waits_for_actual_thread_end(qapp, qtbot, cfg):
    from threading import Event
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import QWidget
    from ui.services import TaskManager

    gate = Event()

    class Worker(QThread):
        finished = Signal(dict)

        def run(self):
            self.finished.emit({"ok": 1})
            gate.wait(2)

        def stop(self):
            gate.set()

    manager = TaskManager()
    worker = Worker()
    owner = QWidget()
    assert manager.submit(worker, owner, "fixture task", mutating=True)
    qtbot.waitUntil(worker.isRunning)
    qapp.processEvents()
    assert manager.running(owner)
    other = Worker()
    assert manager.submit(other, QWidget(), "conflict", mutating=True) is False
    manager.stop_all()
    qtbot.waitUntil(lambda: not manager.running(), timeout=3000)
    assert manager.records[-1]["status"] == "cancelled"
    assert not worker.isRunning()


def test_global_label_notification_refreshes_delete(qapp, cfg):
    from ui.pages.delete import DeletePage
    from ui.services import events

    page = DeletePage(cfg)
    page.store.set("sample-feedback", "DELETE")
    events.labels_changed.emit()
    assert page.labels.get("sample-feedback") == "DELETE"


def test_shutdown_requests_stop_and_close_waits_for_threads(qapp, cfg, monkeypatch):
    from ui.main_window import MainWindow
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QWidget

    page = QWidget()
    calls = []
    page.shutdown = lambda: calls.append("shutdown")
    page._worker = SimpleNamespace(isRunning=lambda: True, stop=lambda: calls.append("stop"))
    window = MainWindow(cfg)
    window.register_page(page)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    assert "stop" in calls
    page._worker.isRunning = lambda: False
    window.close()  # release the deliberately blocked close timer between tests


def test_navigation_is_scrollable_and_task_center_registered(qapp, cfg):
    from ui.main_window import MainWindow, NAV_LABELS
    from PySide6.QtWidgets import QScrollArea

    window = MainWindow(cfg)
    assert "任务中心" in NAV_LABELS
    assert window.findChild(QScrollArea, "navigation-scroll") is not None


def test_generated_subtitle_rechecked_even_after_old_scan(qapp, cfg, tmp_path):
    from ui.pages.subtitle import SubtitlePage

    page = SubtitlePage(cfg)
    video = tmp_path / "test.mp4"
    video.write_bytes(b"fixture")
    page._checked = {str(video)}
    page.result_list.add_item(video.name, str(video))
    video.with_suffix(".srt").write_text("fixture subtitle")
    page._refresh_missing()
    assert page.result_list.count() == 0


def test_ai_keep_is_removed_from_delete_plan(qapp, cfg, monkeypatch):
    from ui.pages.delete import DeletePage

    page = DeletePage(cfg)
    page._delete_rows = [
        {"path": "keep.mp4", "num": "KEEP-1", "size": 1},
        {"path": "delete.mp4", "num": "DEL-1", "size": 2},
    ]
    page._ai_keep_nums = {"KEEP-1"}
    assert page._planned_targets(False) == [("delete.mp4", 2)]


def test_ai_batch_never_writes_machine_verdict_as_manual_label(qapp, cfg):
    from ui.pages.review import ReviewPage

    page = ReviewPage(cfg)
    page._ai_review_marks = ["machine-choice"]
    page._ai_review_pool = [[]]
    page._ai_review_idx = 0
    page._refresh_after_change = lambda: None
    page._on_review_batch_done({})
    assert "machine-choice" not in page.store.all()


def test_failed_automation_is_not_success(qapp, cfg):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page._on_error("fixture failure")
    page._on_finished()
    assert page.progress.value() != 100
    assert "失败" in page.log_panel.toPlainText()


def test_server_cleanup_requires_reviewed_plan(qapp, cfg, monkeypatch):
    from ui.pages.settings import SettingsPage
    from PySide6.QtCore import QThread, Signal
    import workers.jellyfin_sync as sync

    class SafeWorker(QThread):
        log = Signal(str)
        finished = Signal(dict)

        def __init__(self, *args, **kwargs):
            super().__init__()

        def run(self):
            self.finished.emit({"empty": True})

    monkeypatch.setattr(sync, "JellyfinCleanWorker", SafeWorker)
    page = SettingsPage(cfg)
    page.input_jf_server.setText("http://fixture.invalid")
    page.input_jf_key.setText("fixture-key")
    page._clean_jellyfin()
    assert getattr(page, "_jf_worker", None) is None


def test_automation_executes_only_reviewed_plan(qapp, cfg, monkeypatch):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page.cb_delete.setChecked(True)
    plan = {
        "id": "same-plan",
        "actions": [{"kind": "delete", "path": "fixture.mp4", "reason": "reviewed"}],
    }
    page._on_report({"status": "planned", "plan": plan})
    assert page._options(True)["approved_plan_id"] == "same-plan"
    page.cb_merge.setChecked(True)
    assert page._options(True)["approved_plan_id"] == ""


def test_whisper_file_import_rejects_audio(qapp, cfg, tmp_path):
    from ui.pages.subtitle_gen import SubtitleGenPage

    page = SubtitleGenPage(cfg)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture")
    page._add_paths([str(audio)])
    assert page.list_files.count() == 0


def test_ai_adoption_is_explicit_and_undoable(qapp, cfg, monkeypatch):
    from ui.pages.review import ReviewPage
    from utils.library import Item

    page = ReviewPage(cfg)
    page.items = [Item("candidate", "candidate", "candidate.mp4", "Movie", 2020)]
    page._collect_ai_suggestion({"item_id": "candidate", "verdict": "DELETE", "reason": "fixture"})
    assert "candidate" not in page.store.all()
    page.ai_suggestions.item(0, 0).setCheckState(Qt.Checked)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page._refresh_after_change = lambda: None
    page._adopt_ai_suggestions()
    assert page.store.all()["candidate"] == "DELETE"
    page._undo_ai_adoption()
    assert "candidate" not in page.store.all()


def test_task_center_restores_actual_quarantined_fixture(qapp, qtbot, cfg, tmp_path, monkeypatch):
    import ui.pages.tasks as tasks_module
    from utils.trash import trash_file
    from ui.pages.tasks import TaskCenterPage
    from ui.services import TaskManager, busy

    source = tmp_path / "test.mp4"
    source.write_bytes(b"fixture-media")
    ok, _ = trash_file(str(source))
    assert ok and not source.exists()

    trace = []
    real_start_task = tasks_module.start_task

    def spy_start_task(owner, worker, title=None, mutating=None):
        result = real_start_task(owner, worker, title, mutating)
        trace.append(
            (
                "start_task",
                type(worker).__name__,
                result,
                bool(getattr(worker, "_stopped", None)),
                worker.isRunning(),
                worker.isFinished(),
            )
        )
        return result

    monkeypatch.setattr(tasks_module, "start_task", spy_start_task)

    real_submit = TaskManager.submit

    def spy_submit(self, worker, owner, title, mutating=False, resources=()):
        result = real_submit(self, worker, owner, title, mutating, resources)
        trace.append(("submit", type(worker).__name__, result))
        return result

    monkeypatch.setattr(TaskManager, "submit", spy_submit)

    page = TaskCenterPage(cfg)
    renders = []
    real_render = page._render_backups

    def spy_render(entries):
        renders.append(len(entries))
        real_render(entries)

    page._render_backups = spy_render
    page._load_backups()

    def snapshot():  # 诊断用：超时时把现场打出来
        worker = getattr(page, "_worker", None)
        active = []
        for entry in TaskManager.instance()._active.values():
            try:
                alive = entry["worker"].isRunning()
            except RuntimeError:
                alive = "C++已销毁"
            active.append((entry["record"]["title"], entry["record"]["status"], alive))
        try:
            from utils.file_ops import OperationJournal
            from utils.trash import list_quarantine

            journal = (
                f"quarantine={len(list_quarantine())} "
                f"all={[(r['kind'], r['state']) for r in OperationJournal().all()]}"
            )
        except Exception as exc:
            journal = f"读取失败:{type(exc).__name__}:{exc}"
        return (
            f"rows={page.backups.rowCount()} busy={busy(page)} "
            f"worker_attr={type(worker).__name__ if worker is not None else None} "
            f"active={active} trace={trace} renders={renders} {journal} "
            f"details={page.details.toPlainText()[:200]!r}"
        )

    try:
        qtbot.waitUntil(lambda: not busy(page), timeout=3000)
    except Exception as exc:
        raise AssertionError(f"页面一直 busy：{snapshot()}") from exc
    try:
        qtbot.waitUntil(lambda: page.backups.rowCount() > 0, timeout=3000)
    except Exception as exc:
        raise AssertionError(f"备份行数一直为 0：{snapshot()}") from exc
    matching = [
        row
        for row in range(page.backups.rowCount())
        if page.backups.item(row, 2).text() == str(source)
    ]
    assert len(matching) == 1
    page.backups.selectRow(matching[0])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page._restore_selected()
    qtbot.waitUntil(lambda: not busy(page), timeout=3000)
    assert source.read_bytes() == b"fixture-media"


def test_automation_binding_actual_user_invalidates_old_plan(qapp, cfg):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page._on_report(
        {
            "status": "planned",
            "plan": {"id": "old-plan", "actions": []},
            "stages": {
                "score": {
                    "resolved_user_id": "actual-viewer",
                    "resolved_source_identity": "api:fixture",
                    "source_context": "actual-scope",
                }
            },
        }
    )
    assert cfg.jellyfin_user_id == "actual-viewer"
    assert page._preview_plan is None


def test_dedup_plan_never_removes_last_copy(qapp, cfg):
    from ui.pages.dedup import DedupPage

    def item(id):
        return SimpleNamespace(
            id=id,
            path=id + ".mp4",
            actors=[],
            studios=[],
            genres=[],
            tags=[],
            is_uncensored=False,
            is_cd=False,
            cd_index=None,
            year=2020,
            num="TEST-123",
            size=100,
        )

    page = DedupPage(cfg)
    page.groups = [
        {"num": "TEST-123", "all_cd": False, "items": [item("a"), item("b")], "losers": {"b"}}
    ]
    page._render_tree()
    for idx in range(2):
        page.tree.topLevelItem(0).child(idx).setCheckState(0, Qt.Checked)
    qapp.processEvents()
    assert page._planned_targets() == []


def test_ai_scores_only_scope_can_be_cleared(qapp, cfg):
    from ui.pages.profile import ProfilePage
    from ui.services import state_context
    from config import DATA_DIR
    from utils.ai_scoring import AIScoreStore

    store = AIScoreStore(DATA_DIR / "smart.db", context=state_context(cfg), fingerprint="fixture")
    store.save("candidate", 0.8, "fixture")
    page = ProfilePage(cfg)
    page._refresh_ai_clear()
    assert page.btn_ai_clear.isEnabled()


def test_stale_analysis_cannot_rebind_changed_server(qapp, cfg):
    from ui.services import accept_snapshot, _snapshot_key

    owner = SimpleNamespace(cfg=cfg, _snapshot_request_key=_snapshot_key(cfg))
    cfg.jellyfin_server = "http://different.invalid"
    data = {"resolved_user_id": "old-user", "resolved_source_identity": "server:old"}
    assert not accept_snapshot(owner, data)
    assert cfg.jellyfin_user_id == ""
    assert data.get("error")


def test_censorship_only_classifies_filtered_media(tmp_path):
    from ui.pages.censorship import CensorshipScanWorker

    audio = tmp_path / "audio.wav"
    media = tmp_path / "media.mp4"
    aux = tmp_path / "theme.mp4"
    for path in (audio, media, aux):
        path.write_bytes(b"fixture")
    worker = CensorshipScanWorker([str(audio), str(tmp_path)], {".mp4"}, [])
    rows = []
    worker.file_classified.connect(lambda path, _kind: rows.append(path))
    worker.run()
    assert rows == [str(media)]


def test_task_center_legacy_migration_requires_explicit_selection(qapp, cfg, monkeypatch):
    from ui.pages.tasks import TaskCenterPage
    from ui.services import state_context
    from utils.labels import LabelStore
    from utils.library import Item
    from config import DATA_DIR

    cfg.jellyfin_server = "http://fixture.invalid"
    cfg.jellyfin_user_id = "viewer"
    item = Item("legacy-ui", "legacy fixture", "fixture.mp4", "Movie", 2020)
    item.source_identity = "server:http://fixture.invalid"
    item.behavior_user_id = "viewer"
    legacy = LabelStore(DATA_DIR / "smart.db")
    legacy.set(item.id, "KEEP")
    current = LabelStore(DATA_DIR / "smart.db", context=state_context(cfg))
    page = TaskCenterPage(cfg)
    page._on_legacy_preview(
        {
            "items": [item],
            "resolved_source_identity": item.source_identity,
            "resolved_user_id": "viewer",
        }
    )
    assert page.migration.rowCount() == 1
    assert not current.get(item.id)
    page._adopt_legacy()
    assert not current.get(item.id)
    page.migration.item(0, 0).setCheckState(Qt.Checked)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page._adopt_legacy()
    assert current.get(item.id) == "KEEP"
    assert legacy.get(item.id) == "KEEP"


def test_automation_workflow_reload_never_reuses_approval(qapp, cfg, tmp_path):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page.cb_merge.setChecked(True)
    path = tmp_path / "workflow.json"
    page._save_workflow(path)
    page.cb_merge.setChecked(False)
    page._preview_plan = {"id": "old-approval", "actions": []}
    page._load_workflow(path)
    assert page.cb_merge.isChecked()
    assert not page._preview_plan
    assert not page._options(False)["apply_merge"]


def test_context_switch_clears_ai_decisions_and_undo(qapp, cfg, monkeypatch):
    from ui.pages.review import ReviewPage
    from ui.services import refresh_configuration, state_context
    from utils.library import Item
    from utils.labels import LabelStore
    from config import DATA_DIR

    cfg.jellyfin_user_id = "old-viewer"
    page = ReviewPage(cfg)
    page._state_context = state_context(cfg)
    page.items = [Item("same-id", "fixture", "fixture.mp4", "Movie", 2020)]
    page._collect_ai_suggestion(
        {"item_id": "same-id", "verdict": "DELETE", "reason": "old suggestion"}
    )
    page.ai_suggestions.item(0, 0).setCheckState(Qt.Checked)
    page._last_action = ("same-id", "DELETE")
    page._last_ai_adoption = [("same-id", "DELETE")]
    cfg.jellyfin_user_id = "new-viewer"
    refresh_configuration(page)
    page._refresh_after_change = lambda: None
    store = LabelStore(DATA_DIR / "smart.db", context=state_context(cfg))
    store.set("same-id", "KEEP")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page._adopt_ai_suggestions()
    page._undo()
    page._undo_ai_adoption()
    assert store.get("same-id") == "KEEP"
    assert page.ai_suggestions.rowCount() == 0


def test_context_switch_clears_profile_and_ai_actions(qapp, cfg):
    from ui.pages.profile import ProfilePage
    from ui.services import refresh_configuration, state_context

    page = ProfilePage(cfg)
    page._state_context = state_context(cfg)
    page.profile = {"marker": "old viewer"}
    page.btn_ai.setEnabled(True)
    page.btn_ai_taste.setEnabled(True)
    cfg.jellyfin_user_id = "new-viewer"
    refresh_configuration(page)
    assert page.profile == {}
    assert not page.btn_ai.isEnabled()
    assert not page.btn_ai_taste.isEnabled()


def test_task_failed_result_arrives_after_thread_return(qapp, qtbot):
    from PySide6.QtCore import QThread, Signal
    from PySide6.QtWidgets import QWidget
    from ui.services import TaskManager

    class Fail(QThread):
        finished = Signal(dict)

        def run(self):
            self.finished.emit({"error": "actual queued failure"})

    owner = QWidget()
    worker = Fail()
    manager = TaskManager.instance()
    assert manager.submit(worker, owner, "queued failure")
    assert worker.wait(1000)
    manager._poll()
    qtbot.waitUntil(lambda: not manager._active, timeout=2000)
    assert manager.records[-1]["status"] == "failed"


def test_same_viewer_source_binding_broadcasts_scope_change(qapp, cfg):
    from ui.services import accept_snapshot, events, state_context

    cfg.jellyfin_user_id = "viewer"
    before = state_context(cfg)
    owner = SimpleNamespace(cfg=cfg)
    observed = []

    def changed():
        observed.append(state_context(cfg))

    events.config_changed.connect(changed)
    try:
        assert accept_snapshot(
            owner,
            {"resolved_user_id": "viewer", "resolved_source_identity": "db:synthetic-new-library"},
        )
        assert state_context(cfg) != before
        assert observed == [state_context(cfg)]
    finally:
        events.config_changed.disconnect(changed)


def test_profile_feedback_change_invalidates_ai_input(qapp, cfg):
    from ui.pages.profile import ProfilePage
    from ui.services import events, state_context
    from utils.labels import LabelStore
    from utils.library import Item
    from utils.state_context import score_fingerprint
    from config import DATA_DIR

    page = ProfilePage(cfg)
    page.items = [Item("seed", "seed", "seed.mp4", "Movie", 2020)]
    store = LabelStore(DATA_DIR / "smart.db", context=state_context(cfg))
    page.profile = {"n_pos": 1}
    page._fingerprint = score_fingerprint(page.items, store.all(), cfg)
    page.btn_ai_taste.setEnabled(True)
    store.set("seed", "DELETE")
    events.labels_changed.emit()
    assert not page.profile
    assert not page.btn_ai_taste.isEnabled()


def test_feedback_changed_during_analysis_rejects_old_profile(qapp, cfg):
    from ui.pages.profile import ProfilePage
    from ui.services import events, state_context
    from utils.scoring_service import score_snapshot
    from utils.labels import LabelStore
    from utils.library import Item
    from config import DATA_DIR

    item = Item("race-seed", "race seed", "fixture.mp4", "Movie", 2020)
    item.is_favorite = True
    data = score_snapshot([item], cfg, {})
    page = ProfilePage(cfg)
    LabelStore(DATA_DIR / "smart.db", context=state_context(cfg)).set(item.id, "DELETE")
    events.labels_changed.emit()
    page._on_done(data)
    assert not page.profile
    assert not page.btn_export.isEnabled()
    assert not page.btn_ai_taste.isEnabled()


def test_automation_identity_summary_is_not_label_snapshot(qapp, cfg):
    from ui.pages.automation import AutomationPage

    page = AutomationPage(cfg)
    page._on_report(
        {
            "status": "planned",
            "plan": {"id": "reviewable", "actions": []},
            "stages": {
                "score": {"labels": 0, "resolved_user_id": "", "resolved_source_identity": ""}
            },
        }
    )
    assert page._preview_plan["id"] == "reviewable"
    assert not page._failed


def test_real_orchestrator_empty_scope_report_is_reviewable(qapp, cfg, monkeypatch):
    from automation import orchestrator
    from ui.pages.automation import AutomationPage

    monkeypatch.setattr(
        orchestrator, "collect_snapshot", lambda *a, **k: {"items": [], "paths": [], "summary": {}}
    )
    report = orchestrator.run_orchestrator(
        cfg, {"roots": [], "selected_actions": []}, log=lambda _: None
    )
    assert report["status"] == "planned", report.get("error")
    assert isinstance(report["stages"]["score"]["labels"], int)
    page = AutomationPage(cfg)
    page._on_report(report)
    assert not page._failed
    assert page._preview_plan["id"] == report["plan"]["id"]


def test_semantic_profiles_are_built_per_bucket(qapp, cfg, monkeypatch):
    from ui.pages.review import ReviewPage
    from utils.library import Item

    cfg.ai_enabled = True

    def item(id, bucket, fav):
        it = Item(id, id, id + ".mp4", "Movie", 2020)
        it.bucket = bucket
        it.is_favorite = fav
        it.actors = ["actor-" + bucket]
        return it

    page = ReviewPage(cfg)
    page.items = [
        item("positive-jav", "jav", True),
        item("positive-animation", "hentai", True),
        item("candidate-jav", "jav", False),
        item("candidate-animation", "hentai", False),
    ]
    page.results = [
        SimpleNamespace(item=it, is_seen=False, is_junk=False, featureless=False, decision="review")
        for it in page.items[2:]
    ]
    captured = []
    page._run_score_batch = lambda lines: captured.append(lines)
    page._ai_score_all()
    assert isinstance(captured[0], dict)
    assert "actor-jav" in captured[0]["jav"]
    assert "actor-hentai" not in captured[0]["jav"]
    assert "actor-hentai" in captured[0]["hentai"]


def test_delete_selected_scope_excludes_other_tab(qapp, cfg, tmp_path):
    from ui.pages.delete import DeletePage
    from PySide6.QtWidgets import QTableWidgetItem

    page = DeletePage(cfg)
    page._delete_rows = [{"path": "suggested.mp4", "size": 1}]
    page.table.setRowCount(1)
    cell = QTableWidgetItem("suggested")
    cell.setData(Qt.UserRole, "suggested.mp4")
    page.table.setItem(0, 0, cell)
    page.table.selectRow(0)
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fixture")
    page._add_paths([str(video)])
    page.manual_list.selectAll()
    page.findChild(
        __import__("PySide6.QtWidgets", fromlist=["QTabWidget"]).QTabWidget
    ).setCurrentIndex(1)
    assert page._planned_targets(True) == [(str(video), None)]
    assert page._planned_targets(False) == [(str(video), None)]


def test_mapping_table_is_canonical_and_saves(qapp, cfg):
    from ui.pages.settings import SettingsPage

    page = SettingsPage(cfg)
    page._add_mapping()
    page.mapping_table.item(0, 0).setText("/media/movies")
    page.mapping_table.item(0, 1).setText("D:/Media")
    page._save()
    assert cfg.library_path_mappings == [{"remote": "/media/movies", "local": "D:/Media"}]
    page.mapping_table.selectRow(0)
    page._remove_mappings()
    page._save()
    assert cfg.library_path_mappings == []


@pytest.mark.parametrize("kind", ["replace", "nfo"])
def test_legacy_worker_facade_reports_actual_failed_stats(qapp, qtbot, monkeypatch, kind):
    from PySide6.QtWidgets import QWidget
    from ui.services import TaskManager

    if kind == "replace":
        from ui.pages.replace import ReplaceWorker

        monkeypatch.setattr("utils.replacement.preview_replacements", lambda *a, **k: [])
        monkeypatch.setattr(
            "utils.replacement.execute_replacements",
            lambda *a, **k: {
                "ok": 0,
                "fail": 1,
                "cancelled": False,
                "fails": ["fixture rejected"],
                "operations": [],
            },
        )
        worker = ReplaceWorker([], [])
    else:
        from ui.pages.nfo_fix import NFOWorker

        monkeypatch.setattr("utils.nfo.preview_nfo_fixes", lambda *a, **k: [])
        monkeypatch.setattr(
            "utils.nfo.execute_nfo_fixes",
            lambda *a, **k: {
                "fixed": 0,
                "renamed": 0,
                "fail": 1,
                "cancelled": False,
                "fails": ["fixture rejected"],
                "operations": [],
            },
        )
        worker = NFOWorker("synthetic")
    owner = QWidget()
    manager = TaskManager()
    assert manager.submit(worker, owner, "fixture rejected execution")
    qtbot.waitUntil(lambda: not manager.running(), timeout=3000)
    assert manager.records[-1]["status"] == "failed"
    assert "fixture rejected" in manager.records[-1]["error"]
