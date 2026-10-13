"""Durable, recoverable file operations. No Qt or permanent-delete fallback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime
from contextlib import closing, contextmanager
from pathlib import Path

_LOCK = threading.RLock()


def path_key(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def source_output_key(path):
    return hashlib.sha256(path_key(path).encode("utf-8")).hexdigest()[:12]


def mapped_output_directory(path, output_dir, files=None):
    """Centralized jobs keep path identity even across separate invocations."""
    video = Path(path)
    root = Path(output_dir) if output_dir else video.parent
    if output_dir:
        root /= source_output_key(path)
    return root


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_new_file(stage, target):
    """Destination-local atomic install which never clobbers another process."""
    if os.name == "nt":
        os.rename(stage, target)  # Windows rename refuses an existing destination.
    else:
        os.link(stage, target)
        Path(stage).unlink()


def _restore_new_file(recovered, source, digest, should_stop=None):
    """Cross-volume recovery becomes visible only after the whole copy is verified."""
    source.parent.mkdir(parents=True, exist_ok=True)
    fd, stage = tempfile.mkstemp(prefix=".toolkit-stage-", suffix=source.suffix, dir=source.parent)
    os.close(fd)
    try:
        shutil.copy2(recovered, stage)
        if file_hash(stage) != digest or file_hash(recovered) != digest:
            raise OSError("恢复副本校验失败，已保留备份")
        if should_stop and should_stop():
            raise OSError("已停止，已保留备份")
        install_new_file(stage, source)
    finally:
        Path(stage).unlink(missing_ok=True)


def snapshot_sqlite(source, destination):
    """Copy the committed logical SQLite database, including active WAL pages."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if path_key(source) == path_key(destination):
        raise ValueError("快照目标不能是源数据库")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".db")
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("数据库快照完整性检查失败")
        if destination.exists():
            raise FileExistsError(str(destination))
        install_new_file(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OperationJournal:
    """Intent is committed before touching source/target; incomplete intents recover."""

    def __init__(self, path=None):
        if path is None:
            import config

            path = Path(config.DATA_DIR) / "media_operations.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS operations "
                "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT, "
                "target TEXT, state TEXT NOT NULL, payload TEXT NOT NULL, "
                "created_at TEXT NOT NULL)"
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def begin(self, kind, source="", target="", **payload):
        operation_id = payload.pop("operation_id", uuid.uuid4().hex)
        row = dict(
            id=operation_id,
            kind=kind,
            source=str(source),
            target=str(target),
            state="prepared",
            created_at=datetime.now().isoformat(timespec="seconds"),
            **payload,
        )
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if target:
                active = conn.execute(
                    "SELECT id FROM operations WHERE target=? AND state IN ('prepared','staged','partial')",
                    (str(target),),
                ).fetchone()
                if active:
                    raise RuntimeError(f"目标有尚未恢复的操作: {active[0]}")
            conn.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (
                    operation_id,
                    kind,
                    str(source),
                    str(target),
                    "prepared",
                    json.dumps(row, ensure_ascii=False),
                    row["created_at"],
                ),
            )
        return row

    def update(self, operation_id, state, **fields):
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            payload = json.loads(row[0])
            payload.update(fields, state=state)
            conn.execute(
                "UPDATE operations SET state=?,payload=? WHERE id=?",
                (state, json.dumps(payload, ensure_ascii=False), operation_id),
            )
        return payload

    def get(self, operation_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self._conn() as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload FROM operations ORDER BY created_at DESC,rowid DESC"
                )
            ]

    def restore(self, operation_id, should_stop=None):
        entry = self.get(operation_id)
        if not entry:
            return dict(ok=False, error="操作记录不存在", operation_id=operation_id)
        if should_stop and should_stop():
            return dict(ok=False, error="已停止", state="cancelled", operation_id=operation_id)
        if entry.get("state") == "rolled_back":
            return dict(ok=True, error="", state="rolled_back", operation_id=operation_id)
        if entry["kind"] == "quarantine":
            source = Path(entry["source"])
            backup = Path(entry.get("backup_path") or entry["target"])
            if source.exists():
                if (
                    source.is_file()
                    and entry.get("restoring")
                    and file_hash(source) == entry.get("source_hash")
                ):
                    self.update(operation_id, "rolled_back")
                    return dict(ok=True, error="", state="rolled_back", operation_id=operation_id)
                return dict(
                    ok=False, error="原路径已有文件，恢复冲突，请先审阅", operation_id=operation_id
                )
            if not backup.exists():
                return dict(ok=False, error="隔离文件不存在", operation_id=operation_id)
            if (
                backup.is_file()
                and entry.get("source_hash")
                and file_hash(backup) != entry["source_hash"]
            ):
                return dict(ok=False, error="隔离文件已被修改，拒绝恢复", operation_id=operation_id)
            try:
                self.update(operation_id, "partial", restoring=True)
                _restore_new_file(backup, source, entry["source_hash"], should_stop)
                backup.unlink()
                self.update(operation_id, "rolled_back")
                return dict(ok=True, error="", state="rolled_back", operation_id=operation_id)
            except OSError as exc:
                return dict(ok=False, error=str(exc), operation_id=operation_id)
        target = Path(entry["target"])
        old_digest, new_digest = entry.get("target_hash"), entry.get("source_hash")
        if (
            entry.get("move_source")
            and Path(entry["source"]).is_file()
            and file_hash(entry["source"]) != new_digest
        ):
            return dict(
                ok=False, error="原路径被新文件占用，拒绝覆盖或删除备份", operation_id=operation_id
            )
        current = file_hash(target) if target.is_file() else None
        if (
            current == old_digest
            and old_digest
            and not (entry.get("move_source") and not Path(entry["source"]).exists())
        ):
            self.update(operation_id, "rolled_back")
            return dict(ok=True, error="", state="rolled_back", operation_id=operation_id)
        if current and current != new_digest:
            return dict(
                ok=False,
                error="目标已被其它操作修改，恢复冲突，请先审阅",
                operation_id=operation_id,
            )
        backup = Path(entry["backup_path"]) if entry.get("backup_path") else None
        try:
            self.update(operation_id, "partial", restoring=True)
            # Retain the new output before restoring the old target (and consumed source).
            recovered = (
                self.path.parent / "media-backups" / operation_id / ("output" + target.suffix)
            )
            if target.is_file():
                recovered.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, recovered)
                if file_hash(recovered) != new_digest:
                    raise OSError("新产物保留副本校验失败")
            source = Path(entry["source"])
            if entry.get("move_source") and not source.exists() and recovered.exists():
                _restore_new_file(recovered, source, new_digest, should_stop)
            if entry.get("target_existed"):
                if not backup or not backup.is_file() or file_hash(backup) != old_digest:
                    raise OSError("原目标备份缺失或校验不符，已保留现状")
                fd, stage = tempfile.mkstemp(
                    prefix=".toolkit-stage-", dir=target.parent, suffix=target.suffix
                )
                os.close(fd)
                try:
                    shutil.copy2(backup, stage)
                    if file_hash(stage) != old_digest:
                        raise OSError("原目标恢复副本校验失败")
                    if (file_hash(target) if target.is_file() else None) != current:
                        raise OSError("恢复期间目标发生变化，已保留备份")
                    os.replace(stage, target)
                finally:
                    Path(stage).unlink(missing_ok=True)
            elif target.exists():
                # The created output is retained above, never permanently discarded.
                if not target.is_file() or file_hash(target) != current:
                    raise OSError("恢复期间目标发生变化，已保留备份")
                target.unlink()
            self.update(operation_id, "rolled_back", recovered_output=str(recovered))
            return dict(ok=True, error="", state="rolled_back", operation_id=operation_id)
        except Exception as exc:
            return dict(ok=False, error=str(exc), operation_id=operation_id)


def safe_replace(
    source,
    target,
    *,
    journal=None,
    should_stop=None,
    move_source=False,
    validator=None,
    kind="replace",
    allow_overwrite=True,
    session_id=None,
    expected_source_hash=None,
    expected_target_hash=None,
):
    """Stage locally, verify, retain old target, journal, then atomically commit."""
    source, target = Path(source), Path(target)
    result = dict(
        ok=False, error="", state="failed", operation_id="", target=str(target), backup_path=""
    )
    stage = None
    with _LOCK:
        try:
            if not source.is_file():
                raise FileNotFoundError(f"源文件不存在: {source}")
            if path_key(source) == path_key(target):
                raise ValueError("源与目标相同")
            if should_stop and should_stop():
                result.update(state="cancelled", error="已停止")
                return result
            if target.exists() and (not allow_overwrite or not target.is_file()):
                raise FileExistsError(f"目标已经存在: {target}")
            journal = journal or OperationJournal()
            operation_id = uuid.uuid4().hex
            backup = journal.path.parent / "media-backups" / operation_id / target.name
            existed = target.exists()
            source_digest = file_hash(source)
            target_digest = file_hash(target) if existed else None
            if expected_source_hash is not None and source_digest != expected_source_hash:
                raise OSError("源文件已改变，请重新审阅计划")
            if expected_target_hash is not None and target_digest != expected_target_hash:
                raise OSError("目标已改变，请重新审阅计划")
            entry = journal.begin(
                kind,
                str(source),
                str(target),
                operation_id=operation_id,
                backup_path=str(backup) if existed else "",
                target_existed=existed,
                source_hash=source_digest,
                target_hash=target_digest,
                move_source=move_source,
                session_id=session_id,
            )
            result.update(operation_id=entry["id"], backup_path=entry["backup_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, stage = tempfile.mkstemp(
                prefix=".toolkit-stage-", suffix=target.suffix, dir=target.parent
            )
            os.close(fd)
            journal.update(operation_id, "prepared", stage_path=stage)
            shutil.copy2(source, stage)
            if file_hash(stage) != source_digest or file_hash(source) != source_digest:
                raise OSError("复制期间源文件发生变化，已保留原件")
            if validator:
                check = validator(Path(stage))
                valid, detail = check if isinstance(check, tuple) else (bool(check), "产物校验失败")
                if not valid:
                    raise ValueError(detail)
            if should_stop and should_stop():
                journal.update(operation_id, "cancelled")
                result.update(state="cancelled", error="已停止")
                return result
            if existed:
                if not target.is_file() or file_hash(target) != target_digest:
                    raise OSError("目标在计划后发生变化，拒绝覆盖")
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                if file_hash(backup) != target_digest:
                    raise OSError("原目标备份校验失败")
            elif target.exists():
                raise FileExistsError("目标在计划后已经存在")
            journal.update(operation_id, "staged")
            if should_stop and should_stop():
                journal.update(operation_id, "cancelled")
                result.update(state="cancelled", error="已停止")
                return result
            if existed:
                if not target.is_file() or file_hash(target) != target_digest:
                    raise OSError("落位前目标发生变化，拒绝覆盖")
                os.replace(stage, target)
            else:
                # No-clobber atomic install, including a competing process creating target.
                install_new_file(stage, target)
            journal.update(operation_id, "committed")
            if should_stop and should_stop():
                restored = journal.restore(operation_id)
                result.update(
                    state="cancelled" if restored["ok"] else "partial",
                    error="已停止，已回滚" if restored["ok"] else restored["error"],
                )
                return result
            if move_source:
                if file_hash(source) != source_digest:
                    raise OSError("源在落位后改变，保留源文件以供审阅")
                source.unlink()
            journal.update(operation_id, "succeeded", source_deleted=move_source)
            result.update(ok=True, state="succeeded")
            return result
        except Exception as exc:
            result["error"] = str(exc)
            if result["operation_id"]:
                # A post-commit journal failure is recoverable from the persisted staged intent.
                try:
                    journal.update(result["operation_id"], "failed", error=str(exc))
                except Exception:
                    result["state"] = "partial"
            return result
        finally:
            if stage:
                Path(stage).unlink(missing_ok=True)


def safe_write_text(
    path,
    text,
    *,
    encoding="utf-8",
    journal=None,
    should_stop=None,
    session_id=None,
    expected_target_hash=None,
):
    """Recoverable content edit; failures cannot truncate an existing document."""
    path = Path(path)
    fd, source = tempfile.mkstemp(suffix=path.suffix)
    os.close(fd)
    try:
        Path(source).write_text(text, encoding=encoding)
        return safe_replace(
            source,
            path,
            journal=journal,
            should_stop=should_stop,
            kind="text_edit",
            session_id=session_id,
            expected_target_hash=expected_target_hash,
        )
    finally:
        Path(source).unlink(missing_ok=True)
