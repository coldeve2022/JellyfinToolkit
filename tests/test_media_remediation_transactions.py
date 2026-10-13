"""Invariants: failures and collisions never destroy recoverable originals."""

import importlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows transient sharing violation")
def test_archive_journal_recovers_from_transient_windows_sharing_lock(tmp_path, monkeypatch):
    from utils import merge_archive as archive

    monkeypatch.setattr(archive, "journal_file", lambda: tmp_path / "journal.json")
    actual = archive.os.replace
    calls = []

    def locked_once(source, target):
        calls.append(source)
        if len(calls) == 1:
            error = PermissionError("transient sharing violation")
            error.winerror = 32
            raise error
        return actual(source, target)

    monkeypatch.setattr(archive.os, "replace", locked_once)
    archive.save_journal([{"id": "recoverable"}])
    assert archive.load_journal() == [{"id": "recoverable"}]
    assert len(calls) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_archive_journal_keeps_old_data_and_cleans_stage_on_permanent_failure(tmp_path, monkeypatch):
    from utils import merge_archive as archive

    monkeypatch.setattr(archive, "journal_file", lambda: tmp_path / "journal.json")
    archive.save_journal([{"id": "old"}])

    def denied(*args):
        raise PermissionError("persistent permission failure")

    monkeypatch.setattr(archive.os, "replace", denied)
    with pytest.raises(PermissionError):
        archive.save_journal([{"id": "new"}])
    assert archive.load_journal() == [{"id": "old"}]
    assert not list(tmp_path.glob("*.tmp"))


def module(name):
    assert importlib.util.find_spec(name), f"missing media safety service: {name}"
    return importlib.import_module(name)


def test_nfo_collision_preserves_both_files(tmp_path):
    from utils.nfo import safe_rename

    src, dst = tmp_path / "old.nfo", tmp_path / "new.nfo"
    src.write_text("old")
    dst.write_text("curated")
    with pytest.raises(FileExistsError):
        safe_rename(str(src), str(dst))
    assert src.read_text() == "old"
    assert dst.read_text() == "curated"


def test_missing_nfo_source_never_removes_target(tmp_path):
    from utils.nfo import safe_rename

    dst = tmp_path / "new.nfo"
    dst.write_text("curated")
    with pytest.raises(OSError):
        safe_rename(str(tmp_path / "missing.nfo"), str(dst))
    assert dst.read_text() == "curated"


def test_transaction_can_restore_overwritten_target(tmp_path):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "source.mp4", tmp_path / "target.mp4"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    result = ops.safe_replace(src, dst, journal=journal)
    assert result["ok"], result
    assert src.read_bytes() == b"new"
    assert dst.read_bytes() == b"new"
    assert Path(result["backup_path"]).read_bytes() == b"old"
    restored = journal.restore(result["operation_id"])
    assert restored["ok"], restored
    assert dst.read_bytes() == b"old"
    assert journal.get(result["operation_id"])["state"] == "rolled_back"


def test_journal_failure_precedes_any_mutation(tmp_path, monkeypatch):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "source", tmp_path / "target"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")

    def unavailable(*args, **kwargs):
        raise OSError("journal disk full")

    monkeypatch.setattr(journal, "begin", unavailable)
    result = ops.safe_replace(src, dst, journal=journal, move_source=True)
    assert not result["ok"]
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"


def test_validation_failure_retains_source_and_existing_target(tmp_path):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "source", tmp_path / "target"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    result = ops.safe_replace(
        src, dst, journal=journal, validator=lambda _: (False, "invalid media"), move_source=True
    )
    assert not result["ok"]
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"
    assert journal.get(result["operation_id"])["state"] == "failed"


def test_cancel_before_commit_preserves_both(tmp_path):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "source", tmp_path / "target"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    state = {"stop": False}

    def validate(_):
        state["stop"] = True
        return True, ""

    result = ops.safe_replace(
        src, dst, journal=journal, validator=validate, should_stop=lambda: state["stop"]
    )
    assert not result["ok"] and result["state"] == "cancelled"
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"


def test_ambiguous_restored_pair_is_not_selected(tmp_path):
    service = module("utils.replacement")
    a, b, r = [tmp_path / name for name in ("a", "b", "restored")]
    for p in (a, b, r):
        p.mkdir()
    (a / "movie.mp4").write_bytes(b"a")
    (b / "movie.mp4").write_bytes(b"b")
    (r / "movie.restored.mp4").write_bytes(b"r")
    plan = service.preview_replacements([str(r)], [str(a), str(b)])
    assert plan[0]["status"] == "ambiguous" and not plan[0]["target"]
    assert len(plan[0]["candidates"]) == 2
    result = service.execute_replacements(plan)
    assert result["ok"] == 0
    assert (a / "movie.mp4").read_bytes() == b"a" and (b / "movie.mp4").read_bytes() == b"b"


def test_quarantine_has_restore_without_calling_system_delete(tmp_path, monkeypatch):
    from utils import trash

    p = tmp_path / "video.mp4"
    p.write_bytes(b"original")
    # Shell deletion must never be the fallback for unsupported recycling.
    assert hasattr(trash, "list_quarantine"), "missing restorable quarantine"
    ok, detail = trash.trash_file(str(p))
    assert ok, detail
    entries = trash.list_quarantine()
    entry = next(e for e in entries if e["source"] == str(p))
    assert not p.exists()
    restored, detail = trash.restore_quarantined(entry["id"])
    assert restored, detail
    assert p.read_bytes() == b"original"


def test_wal_backup_includes_committed_rows(tmp_path):
    ops = module("utils.file_ops")
    live = tmp_path / "live.db"
    backup = tmp_path / "backup.db"
    conn = sqlite3.connect(str(live))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE items (v TEXT)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("INSERT INTO items VALUES ('committed')")
    conn.commit()
    try:
        ops.snapshot_sqlite(live, backup)
        with sqlite3.connect(str(backup)) as copy:
            assert copy.execute("SELECT v FROM items").fetchall() == [("committed",)]
    finally:
        conn.close()


def test_vtt_short_timestamps_and_settings_survive_clean(tmp_path):
    from utils.subtitle_clean import clean_file

    p = tmp_path / "subtitle.vtt"
    p.write_text("WEBVTT\n\nfirst\n00:01.000 --> 00:03.000 align:start\nHello\n", encoding="utf-8")
    clean_file(p)
    after = p.read_text()
    assert "Hello" in after and "align:start" in after and "first" in after


def test_unparseable_subtitles_are_never_overwritten(tmp_path):
    from utils.subtitle_clean import clean_file

    p = tmp_path / "subtitle.srt"
    original = "bad timestamp --> unknown\nHello\n"
    p.write_text(original)
    clean_file(p)
    assert p.read_text() == original


def test_unnumbered_srt_cue_retained(tmp_path):
    from utils.subtitle_clean import clean_file

    p = tmp_path / "subtitle.srt"
    p.write_text("00:00:01,000 --> 00:00:03,000\nHello\n")
    clean_file(p)
    assert "Hello" in p.read_text()


def test_archive_sessions_with_same_number_stay_separate(tmp_path):
    from utils.merge_repair import archived_merge_items

    entries = []
    for session in ("A", "B"):
        d = tmp_path / session
        d.mkdir()
        for i in (1, 2):
            bp = d / f"backup{i}.mp4"
            bp.write_bytes(b"part")
            entries.append(
                dict(
                    session_id=session,
                    number="ABC",
                    kind="media",
                    original_path=str(d / f"cd{i}.mp4"),
                    backup_path=str(bp),
                )
            )
        final = d / "ABC.mp4"
        final.write_bytes(b"merged")
        entries.append(
            dict(session_id=session, number="ABC", kind="merged_output", final_path=str(final))
        )
    items = archived_merge_items(entries)
    assert len(items) == 2
    assert sorted(len(it["media_entries"]) for it in items) == [2, 2]


def test_failed_normalization_never_stays_in_library(tmp_path, monkeypatch):
    from utils.merge import PartFile, PartProbe, EpisodeGroup
    from workers.merge import MergeWorker

    parts = []
    for i in (1, 2):
        p = tmp_path / f"ABC-123-cd{i}.mkv"
        p.write_bytes(bytes([i]))
        parts.append(
            PartFile(
                str(p),
                p.name,
                p.stem,
                "ABC-123",
                i,
                "cd",
                probe=PartProbe(video_codec="h264", container="mkv", duration=100),
            )
        )
    group = EpisodeGroup("ABC123", parts, selected_paths=[p.path for p in parts])
    worker = MergeWorker([group], "fake", {}, "fake")

    def fail(cmd, timeout):
        Path(cmd[-1]).write_bytes(b"partial")
        return False, "failed"

    monkeypatch.setattr(worker, "_run_ffmpeg", fail)
    ok, _ = worker._merge_group(group)
    assert not ok
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ABC-123-cd1.mkv", "ABC-123-cd2.mkv"]


def test_nfo_plan_refuses_changed_source(tmp_path):
    from utils import nfo

    assert hasattr(nfo, "preview_nfo_fixes")
    p = tmp_path / "movie.nfo"
    p.write_text("<movie><filename>x.ts</filename></movie>")
    plan = nfo.preview_nfo_fixes([str(tmp_path)])
    p.write_text("new curated content")
    result = nfo.execute_nfo_fixes(plan)
    assert result["fail"] == 1 and p.read_text() == "new curated content"


def test_archive_records_intent_before_moving_any_original(tmp_path, monkeypatch):
    from utils import merge_archive

    media = tmp_path / "media"
    media.mkdir()
    a, b, out = [media / name for name in ("ABC-cd1.mp4", "ABC-cd2.mp4", "ABC 合并.mp4")]
    for p in (a, b, out):
        p.write_bytes(b"media")
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        merge_backup_root=str(tmp_path / "backups"),
        merge_original_subfolder="old",
        merge_rename_to_number=True,
    )

    def unavailable(*args, **kw):
        raise OSError("journal unavailable")

    monkeypatch.setattr(merge_archive, "save_journal", unavailable)
    ok, _, _, _ = merge_archive.archive_group_for_merge(cfg, "ABC", [str(a), str(b)], str(out))
    assert not ok
    assert a.exists() and b.exists() and out.exists()


def test_archive_does_not_remove_external_subtitles_without_merged_copy(tmp_path):
    from utils.merge_archive import archive_group_for_merge
    from types import SimpleNamespace

    a, b, out = [tmp_path / name for name in ("ABC-cd1.mp4", "ABC-cd2.mp4", "ABC 合并.mp4")]
    for p in (a, b, out):
        p.write_bytes(b"media")
    a.with_suffix(".srt").write_text("1\n00:00:00,100 --> 00:00:00,500\ncaption\n")
    cfg = SimpleNamespace(
        merge_backup_root=str(tmp_path / "backups"),
        merge_original_subfolder="old",
        merge_rename_to_number=True,
    )
    ok, _, _, _ = archive_group_for_merge(cfg, "ABC", [str(a), str(b)], str(out))
    assert not ok and a.exists() and a.with_suffix(".srt").exists()


def test_restoration_rejects_new_edit_of_target(tmp_path):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    result = ops.safe_replace(src, dst, journal=journal)
    dst.write_bytes(b"new user edit")
    restored = journal.restore(result["operation_id"])
    assert not restored["ok"] and dst.read_bytes() == b"new user edit"


def test_restore_move_refuses_source_reoccupied_by_new_file(tmp_path):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"original")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    result = ops.safe_replace(src, dst, journal=journal, move_source=True)
    src.write_bytes(b"independent new source")
    restored = journal.restore(result["operation_id"])
    assert not restored["ok"]
    assert src.read_bytes() == b"independent new source" and dst.read_bytes() == b"original"


def test_legacy_rename_refuses_sidecar_collision_before_video_move(tmp_path):
    from utils.merge import finalize_legacy_merged_output

    video = tmp_path / "ABC-123 合并.mp4"
    video.write_bytes(b"media")
    video.with_suffix(".srt").write_text("old captions")
    dst = tmp_path / "ABC-123.srt"
    dst.write_text("curated captions")
    ok, _, _ = finalize_legacy_merged_output(str(video))
    assert not ok and video.exists() and dst.read_text() == "curated captions"


def test_replacement_detects_target_change_between_plan_check_and_transaction(
    tmp_path, monkeypatch
):
    from utils import replacement

    target = tmp_path / "movie.mp4"
    target.write_bytes(b"old target")
    source = tmp_path / "movie.restored.mp4"
    source.write_bytes(b"new source")
    plan = replacement.preview_replacements([str(source)], [str(target)])
    actual = replacement.safe_replace

    def competing_change(src, dst, **kwargs):
        target.write_bytes(b"new independent target")
        return actual(src, dst, **kwargs)

    monkeypatch.setattr(replacement, "safe_replace", competing_change)
    stats = replacement.execute_replacements(
        plan, validator=lambda _: (True, ""), log=lambda _: None
    )
    assert not stats["ok"] and target.read_bytes() == b"new independent target" and source.exists()


def test_cancel_at_commit_rolls_back_and_keeps_consumable_source(tmp_path, monkeypatch):
    ops = module("utils.file_ops")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    journal = ops.OperationJournal(tmp_path / "journal.db")
    state = {"stop": False}
    actual = journal.update

    def cancel_at_commit(operation_id, status, **fields):
        result = actual(operation_id, status, **fields)
        if status == "committed":
            state["stop"] = True
        return result

    monkeypatch.setattr(journal, "update", cancel_at_commit)
    result = ops.safe_replace(
        src, dst, journal=journal, move_source=True, should_stop=lambda: state["stop"]
    )
    assert not result["ok"] and result["state"] == "cancelled"
    assert src.read_bytes() == b"new" and dst.read_bytes() == b"old"


def test_repair_sidecar_failure_rolls_back_entire_replacement(tmp_path, monkeypatch):
    from config import ToolkitConfig
    from utils import file_ops
    from workers import merge_repair

    sources = []
    for index in (1, 2):
        path = tmp_path / f"backup{index}.mp4"
        path.write_bytes(b"original part")
        sources.append(path)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"old damaged final")
    output = tmp_path / "new.mp4"
    output.write_bytes(b"new validated output")
    output.with_suffix(".srt").write_text("new caption")
    monkeypatch.setattr(merge_repair, "build_parts_from_paths", lambda *args: [])
    monkeypatch.setattr(merge_repair, "make_group_from_parts", lambda *args: object())
    monkeypatch.setattr(merge_repair.MergeWorker, "_merge_group", lambda *args: (True, str(output)))
    actual = file_ops.safe_replace

    def replace(source, target, **kwargs):
        if kwargs.get("kind") == "merge_repair_sidecar":
            return dict(ok=False, error="injected sidecar failure")
        return actual(source, target, **kwargs)

    monkeypatch.setattr(file_ops, "safe_replace", replace)
    worker = merge_repair.RepairMergeWorker([], ToolkitConfig())
    ok, _ = worker._repair_one(
        dict(
            number="ABC123",
            final_path=str(final),
            media_entries=[dict(backup_path=str(p)) for p in sources],
        )
    )
    assert not ok
    assert final.read_bytes() == b"old damaged final"
    assert output.read_bytes() == b"new validated output"


def test_failed_source_restore_copy_never_leaves_active_partial_media(tmp_path, monkeypatch):
    from utils import file_ops

    source = tmp_path / "source.mp4"
    source.write_bytes(b"replacement result")
    target = tmp_path / "target.mp4"
    target.write_bytes(b"original target")
    journal = file_ops.OperationJournal(tmp_path / "journal" / "operations.db")
    result = file_ops.safe_replace(source, target, journal=journal, move_source=True)
    assert result["ok"] and not source.exists()
    actual = file_ops.shutil.copy2

    def interrupted(src, dest, *args, **kwargs):
        if Path(src).name == "output.mp4" and Path(dest).parent == source.parent:
            Path(dest).write_bytes(b"partial")
            raise OSError("injected interrupted source restore")
        return actual(src, dest, *args, **kwargs)

    monkeypatch.setattr(file_ops.shutil, "copy2", interrupted)
    restored = journal.restore(result["operation_id"])
    assert not restored["ok"]
    assert not source.exists()
    assert not list(source.parent.glob(".toolkit-stage-*"))
    assert target.read_bytes() == b"replacement result"


def test_nfo_content_plan_refuses_change_at_transaction_start(tmp_path, monkeypatch):
    from utils import nfo, file_ops

    path = tmp_path / "movie.nfo"
    path.write_text("<movie><filename>x.ts</filename></movie>")
    plan = nfo.preview_nfo_fixes([str(tmp_path)])
    actual = file_ops.safe_write_text

    def raced(target, text, **kwargs):
        Path(target).write_text("new user edit")
        return actual(target, text, **kwargs)

    monkeypatch.setattr(file_ops, "safe_write_text", raced)
    stats = nfo.execute_nfo_fixes(plan, log=lambda _: None)
    assert stats["fail"] == 1 and path.read_text() == "new user edit"


def test_staged_commit_refuses_late_target_edit(tmp_path, monkeypatch):
    from utils.file_ops import OperationJournal, safe_replace

    source = tmp_path / "source.mp4"
    target = tmp_path / "target.mp4"
    source.write_bytes(b"result")
    target.write_bytes(b"reviewed target")
    journal = OperationJournal(tmp_path / "journal.db")
    actual = journal.update

    def update(operation, state, **fields):
        result = actual(operation, state, **fields)
        if state == "staged":
            target.write_bytes(b"new user edit during staging")
        return result

    monkeypatch.setattr(journal, "update", update)
    result = safe_replace(source, target, journal=journal)
    assert not result["ok"]
    assert target.read_bytes() == b"new user edit during staging" and source.exists()
