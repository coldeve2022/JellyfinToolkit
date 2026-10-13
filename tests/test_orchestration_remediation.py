"""End-to-end orchestration contracts with file writes isolated at the executor."""

from types import SimpleNamespace
from pathlib import Path
import json
import sys
import pytest


@pytest.fixture(autouse=True)
def isolated_operation_storage(monkeypatch, tmp_path):
    import config

    data = tmp_path / "operation-data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)


from config import ToolkitConfig
from automation import orchestrator as orch
from automation import pipeline
from utils.merge import MergePlan


def empty_plan(*args, **kwargs):
    return MergePlan(groups=[], unrecognized=[], stats={})


def prepare_fake_library(monkeypatch, paths):
    items = [
        SimpleNamespace(id=str(index), path=str(path), size=path.stat().st_size)
        for index, path in enumerate(paths)
    ]
    monkeypatch.setattr(
        orch,
        "collect_snapshot",
        lambda *a, **k: {"items": items, "paths": [str(p) for p in paths], "summary": {}},
    )

    def scores(selected, *args, **kwargs):
        return {
            "results": [
                SimpleNamespace(item=it, decision="delete", is_seen=False, is_junk=False)
                for it in selected
            ],
            "counts": {"delete": len(selected)},
            "labels": {},
        }

    monkeypatch.setattr(orch, "score_items", scores)
    monkeypatch.setattr(orch, "dedup_groups", lambda _items, *a, **kw: [])
    return items


def test_complete_empty_orchestrator_runs_without_invalid_merge_argument(monkeypatch):
    prepare_fake_library(monkeypatch, [])
    report = orch.run_orchestrator(ToolkitConfig(), log=lambda _: None)
    assert report["summary"]["library_files"] == 0
    assert report["status"] == "planned"


def test_directory_scope_filters_scoring_and_actual_delete_targets(monkeypatch, tmp_path):
    inside_dir = tmp_path / "inside"
    inside_dir.mkdir()
    inside = inside_dir / "TEST-101.mp4"
    outside = tmp_path / "TEST-102.mp4"
    inside.write_bytes(b"fixture")
    outside.write_bytes(b"fixture")
    prepare_fake_library(monkeypatch, [inside, outside])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    targets = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (targets.append(p) is None, ""))
    report = orch.run_orchestrator(
        ToolkitConfig(), {"roots": [str(inside_dir)], "apply_delete": True}, log=lambda _: None
    )
    assert targets == [str(inside)]
    assert report["summary"]["library_files"] == 1


def test_cancelled_partial_plan_never_starts_delete(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"fixture")
    prepare_fake_library(monkeypatch, [video])
    state = {"stop": False}

    def plan(*a, **kw):
        state["stop"] = True
        return empty_plan()

    monkeypatch.setattr(orch, "build_merge_plan", plan)
    targets = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (targets.append(p) is None, ""))
    report = orch.run_orchestrator(
        ToolkitConfig(),
        {"apply_delete": True},
        should_stop=lambda: state["stop"],
        log=lambda _: None,
    )
    assert targets == []
    assert report["status"] == "cancelled"


def test_approved_plan_rejects_changed_input(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"first")
    prepare_fake_library(monkeypatch, [video])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    cfg = ToolkitConfig()
    audit = orch.run_orchestrator(cfg, {"selected_actions": ["delete"]}, log=lambda _: None)
    video.write_bytes(b"changed media")
    targets = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (targets.append(p) is None, ""))
    report = orch.run_orchestrator(
        cfg, {"approved_plan_id": audit["plan"]["id"], "apply_delete": True}, log=lambda _: None
    )
    assert targets == []
    assert report["status"] == "failed"
    assert "变化" in report["error"]


def test_pipeline_cancel_after_planning_never_starts_merge(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"fixture")
    state = {"stop": False}
    group = SimpleNamespace(number="TEST101", status="ready", selected_paths=[str(video)])

    def plan(*a, **kw):
        state["stop"] = True
        return MergePlan(groups=[group], unrecognized=[], stats={})

    monkeypatch.setattr(pipeline, "build_merge_plan", plan)
    calls = []
    monkeypatch.setattr(
        pipeline, "merge_group_ffmpeg", lambda *a, **k: (calls.append(a) is None, "out")
    )
    report = pipeline.run_pipeline(
        ToolkitConfig(),
        roots=[str(tmp_path)],
        ffmpeg="synthetic-ffmpeg",
        ffprobe="synthetic-ffprobe",
        apply_merge=True,
        should_stop=lambda: state["stop"],
        log=lambda _: None,
    )
    assert calls == []
    assert report["status"] == "cancelled"


def test_collection_failure_is_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(
        orch.library_source,
        "collect",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    report = orch.run_orchestrator(ToolkitConfig(), log=lambda _: None)
    assert report["status"] == "failed"
    assert "offline" in report["error"]


def test_plan_report_is_serializable_and_does_not_refer_to_itself(monkeypatch):
    prepare_fake_library(monkeypatch, [])
    report = orch.run_orchestrator(ToolkitConfig(), log=lambda _: None)
    assert json.loads(json.dumps(report))["plan"]["id"]


def test_manual_roots_work_without_connection_but_delete_requires_snapshot(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"media")
    monkeypatch.setattr(
        orch.library_source,
        "collect",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    report = orch.run_orchestrator(ToolkitConfig(), {"roots": [str(tmp_path)]}, log=lambda _: None)
    assert report["status"] == "planned"
    assert report["summary"]["library_files"] == 1
    refused = orch.run_orchestrator(
        ToolkitConfig(), {"roots": [str(tmp_path)], "apply_delete": True}, log=lambda _: None
    )
    assert refused["status"] == "failed"


def test_approved_plan_cannot_be_executed_twice(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"fixture")
    prepare_fake_library(monkeypatch, [video])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    calls = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (calls.append(p) is None, ""))
    cfg = ToolkitConfig()
    plan = orch.run_orchestrator(cfg, {"selected_actions": ["delete"]}, log=lambda _: None)["plan"]
    options = {"approved_plan_id": plan["id"], "apply_delete": True}
    first = orch.run_orchestrator(cfg, options, log=lambda _: None)
    second = orch.run_orchestrator(cfg, options, log=lambda _: None)
    assert first["status"] == "succeeded"
    assert second["status"] == "failed"
    assert calls == [str(video)]


def test_target_created_after_preview_is_rejected(tmp_path):
    from automation.plans import save_plan, load_plan
    import pytest

    video = tmp_path / "TEST-101.mp4"
    target = tmp_path / "output.mp4"
    video.write_bytes(b"fixture")
    cfg = ToolkitConfig()
    plan = save_plan(
        cfg, [{"kind": "merge", "inputs": [str(video)], "target": str(target)}], ["merge"]
    )
    target.write_bytes(b"new user data")
    with pytest.raises(ValueError, match="变化"):
        load_plan(plan["id"], cfg)


def test_new_watching_activity_after_preview_protects_file(monkeypatch, tmp_path):
    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"fixture")
    items = prepare_fake_library(monkeypatch, [video])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    cfg = ToolkitConfig()
    plan = orch.run_orchestrator(cfg, {"selected_actions": ["delete"]}, log=lambda _: None)["plan"]
    items[0].is_played = True
    targets = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (targets.append(p) is None, ""))
    report = orch.run_orchestrator(
        cfg, {"approved_plan_id": plan["id"], "apply_delete": True}, log=lambda _: None
    )
    assert targets == []
    assert report["operations"][0]["state"] == "protected"


def test_new_label_on_alias_entity_protects_physical_file(monkeypatch, tmp_path):
    from utils.labels import LabelStore
    from utils.state_context import state_context
    from utils.scoring import LABLE_KEEP

    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"fixture")
    items = prepare_fake_library(monkeypatch, [video, video])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    cfg = ToolkitConfig()
    plan = orch.run_orchestrator(cfg, {"selected_actions": ["delete"]}, log=lambda _: None)["plan"]
    LabelStore(orch._data_dir() / "smart.db", context=state_context(cfg)).set(
        items[1].id, LABLE_KEEP
    )
    targets = []
    monkeypatch.setattr(orch, "trash_file", lambda p, **kwargs: (targets.append(p) is None, ""))
    report = orch.run_orchestrator(
        cfg, {"approved_plan_id": plan["id"], "apply_delete": True}, log=lambda _: None
    )
    assert targets == []
    assert report["operations"][0]["state"] == "protected"


def test_cli_stdout_is_json_and_failure_exit_is_nonzero(monkeypatch, capsys, tmp_path):
    import auto_orchestrator

    monkeypatch.setattr(
        sys, "argv", ["auto_orchestrator", "--report", str(tmp_path / "report.json")]
    )
    monkeypatch.setattr(
        auto_orchestrator,
        "run_orchestrator",
        lambda *a, **kw: {"status": "failed", "error": "offline", "stages": {}, "summary": {}},
    )
    rc = auto_orchestrator.main()
    capture = capsys.readouterr()
    assert json.loads(capture.out)["status"] == "failed"
    assert rc != 0


def test_plan_rejects_same_length_edit_with_preserved_timestamp(tmp_path):
    import os
    import pytest
    from automation.plans import save_plan, load_plan

    video = tmp_path / "TEST-101.mp4"
    video.write_bytes(b"original")
    cfg = ToolkitConfig()
    plan = save_plan(cfg, [{"kind": "delete", "path": str(video)}], ["delete"])
    before = video.stat()
    video.write_bytes(b"changed!")
    os.utime(video, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="变化"):
        load_plan(plan["id"], cfg)


def test_metadata_only_library_is_still_scored_and_capability_is_explicit(monkeypatch):
    item = SimpleNamespace(id="remote", path="/remote/NAS/TEST-101.mp4", size=123)
    monkeypatch.setattr(
        orch, "collect_snapshot", lambda *a, **k: {"items": [item], "paths": [], "summary": {}}
    )
    selected = []

    def scores(items, *args, **kwargs):
        selected.extend(items)
        return {"results": [], "counts": {}, "labels": {}}

    monkeypatch.setattr(orch, "score_items", scores)
    monkeypatch.setattr(orch, "dedup_groups", lambda _, *a, **kw: [])
    monkeypatch.setattr(orch, "build_merge_plan", empty_plan)
    report = orch.run_orchestrator(ToolkitConfig(), log=lambda _: None)
    assert selected == [item]
    assert report["stages"]["collect"]["remote_items"] == 1
    assert report["stages"]["collect"]["can_modify_local"] is False
