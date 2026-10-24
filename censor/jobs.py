"""Durable file-level scan queue; metadata only influences scheduling."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from censor.models import ProcessingStatus


def scan_priority(path: str) -> tuple[int, str]:
    from utils.nfo_source import read_nfo_features
    from censor.file_signals import _classify_words

    from censor.tag_signals import classify_tag
    named_uncensored, named_censored = classify_tag(Path(path).stem)
    if named_uncensored and not named_censored:
        return 2, '文件名有明确无码/破解线索，优先按名称延后（不作为画面确认）'
    tags, _, _ = read_nfo_features(path)
    uncensored, censored, _ = _classify_words(tags)
    if censored:
        return 0, "NFO 有像素化线索或标签冲突"
    if uncensored:
        return 2, "NFO 有未像素化线索，延后扫描"
    return 1, "NFO 无相关线索"


class ScanJobs:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pixelation_queue ("
                "key TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,"
                'priority INTEGER NOT NULL DEFAULT 1,note TEXT NOT NULL DEFAULT "",'
                'error TEXT NOT NULL DEFAULT "",updated_at TEXT NOT NULL DEFAULT "")'
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pixelation_sources ("
                "path TEXT PRIMARY KEY,status TEXT NOT NULL)"
            )
            columns = {r[1] for r in conn.execute("PRAGMA table_info(pixelation_queue)")}
            if "manual_requested" not in columns:
                conn.execute(
                    "ALTER TABLE pixelation_queue ADD COLUMN manual_requested INTEGER NOT NULL DEFAULT 0"
                )

    def defer(self, path):
        with self.connect() as conn:
            conn.execute(
                'UPDATE pixelation_queue SET status="deferred" WHERE key=?', (self.key(path),)
            )

    def authorize(self, path):
        with self.connect() as conn:
            conn.execute(
                "UPDATE pixelation_queue SET manual_requested=1 WHERE key=?", (self.key(path),)
            )

    def authorized(self, path):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT manual_requested FROM pixelation_queue WHERE key=?", (self.key(path),)
            ).fetchone()
        return bool(row and row[0])

    def deferred(self):
        with self.connect() as conn:
            return conn.execute(
                'SELECT path,note FROM pixelation_queue WHERE status="deferred" ORDER BY path'
            ).fetchall()

    def enqueue_sources(self, paths, *, manual=False):
        with self.connect() as conn:
            conn.executemany(
                'INSERT INTO pixelation_sources VALUES (?,"pending") '
                'ON CONFLICT(path) DO UPDATE SET status="pending"',
                [(str(p),) for p in paths],
            )
            if manual:
                conn.executemany(
                    'UPDATE pixelation_queue SET manual_requested=1,status="pending" WHERE path=?',
                    [(str(p),) for p in paths],
                )

    def finish_sources(self, paths):
        with self.connect() as conn:
            conn.executemany(
                'UPDATE pixelation_sources SET status="done" WHERE path=?',
                [(str(p),) for p in paths],
            )

    def unfinished_sources(self):
        with self.connect() as conn:
            return [
                r[0]
                for r in conn.execute('SELECT path FROM pixelation_sources WHERE status="pending"')
            ]

    def note_for(self, path):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT note FROM pixelation_queue WHERE path=?", (str(path),)
            ).fetchone()
        return row[0] if row else "历史记录暂无调度线索"

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def key(path):
        return os.path.normcase(os.path.realpath(str(path)))

    def enqueue(self, paths):
        with self.connect() as conn:
            for path in paths:
                conn.execute(
                    "INSERT INTO pixelation_queue(key,path,status) VALUES (?,?,?) "
                    'ON CONFLICT(key) DO UPDATE SET path=excluded.path,status="pending",error=""',
                    (self.key(path), str(path), "pending"),
                )

    def prioritize(self, path, priority, note):
        with self.connect() as conn:
            conn.execute(
                "UPDATE pixelation_queue SET priority=?,note=? WHERE key=?",
                (priority, note, self.key(path)),
            )

    def started(self, path):
        with self.connect() as conn:
            conn.execute(
                'UPDATE pixelation_queue SET status="running",updated_at=datetime("now") '
                "WHERE key=?",
                (self.key(path),),
            )

    def finished(self, result):
        status = (
            "done"
            if result.status is ProcessingStatus.COMPLETED
            else "pending"
            if result.status is ProcessingStatus.CANCELLED
            else "failed"
        )
        with self.connect() as conn:
            conn.execute(
                'UPDATE pixelation_queue SET status=?,error=?,updated_at=datetime("now") '
                "WHERE key=?",
                (status, result.error, self.key(result.path)),
            )
            if status == "done":
                conn.execute(
                    "UPDATE pixelation_queue SET manual_requested=0 WHERE key=?",
                    (self.key(result.path),),
                )

    def pending(self):
        with self.connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT path FROM pixelation_queue "
                    'WHERE status IN ("pending","running") ORDER BY priority,key'
                )
            ]

    def retry_failed(self):
        with self.connect() as conn:
            return conn.execute(
                'UPDATE pixelation_queue SET status="pending" WHERE status="failed"'
            ).rowcount

    def stats(self):
        with self.connect() as conn:
            return dict(
                conn.execute("SELECT status,COUNT(*) FROM pixelation_queue GROUP BY status")
            )
