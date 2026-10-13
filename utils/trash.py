"""安全隔离：应用数据目录保存文件与恢复日志，失败保留原件。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def trash_file(path: str, *, should_stop=None) -> tuple[bool, str]:
    """Move a reviewed file to durable app quarantine; never invoke shell delete."""
    import uuid
    import config
    from utils.file_ops import OperationJournal, safe_replace

    p = Path(path)
    if not p.is_file() or p.is_symlink():
        return False, f"只支持逐个审核的普通文件，已保留：{path}"
    journal = OperationJournal()
    destination = Path(config.DATA_DIR) / "quarantine" / uuid.uuid4().hex / p.name
    result = safe_replace(
        p,
        destination,
        journal=journal,
        should_stop=should_stop,
        move_source=True,
        allow_overwrite=False,
        kind="quarantine",
    )
    return result["ok"], (
        f"已隔离，可在恢复记录中找回：{result['operation_id']}" if result["ok"] else result["error"]
    )


def list_quarantine():
    from utils.file_ops import OperationJournal

    return [
        entry
        for entry in OperationJournal().all()
        if entry["kind"] == "quarantine"
        and entry["state"] != "rolled_back"
        and Path(entry.get("target") or "").is_file()
    ]


def restore_quarantined(operation_id, *, should_stop=None):
    from utils.file_ops import OperationJournal

    result = OperationJournal().restore(operation_id, should_stop)
    return result["ok"], result.get("error") or "已恢复隔离文件"


# ── 删除日志（留痕，供追溯/恢复指引）──────────
class DeleteJournal:
    """记录已移入回收站的文件，便于核对与恢复指引。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS deleted(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    size_bytes INTEGER,
                    reason TEXT DEFAULT '',
                    deleted_at TEXT NOT NULL
                )"""
            )

    def record(self, path: str, size_bytes: int | None = None, reason: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO deleted(path, size_bytes, reason, deleted_at) VALUES(?,?,?,?)",
                (path, size_bytes, reason, datetime.now().isoformat(timespec="seconds")),
            )

    def all(self) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM deleted ORDER BY id DESC")]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM deleted").fetchone()[0]

    def total_size(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(SUM(size_bytes),0) FROM deleted").fetchone()[0]
