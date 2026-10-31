"""检索结果缓存 —— 同一个番号一天查八遍是浪费，也是把站点惹毛的最快方式。

和 ``censor.store`` 用同一个数据库文件，但**完全独立的表**：
清理像素化自动结果不会连带清掉检索结果，反之亦然。

缓存键是作品编号（不是文件路径），因为"这部作品有没有替代版本"是作品级事实 ——
同一部片的多个副本、多个分辨率，共享同一条检索结果。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from censor.models import ProcessingStatus

from censor.releases.models import (
    Availability, MagnetLink, ReleaseCandidate, ReleaseReport, normalize_number,
)

__all__ = ["ReleaseStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS release_report (
    number      TEXT PRIMARY KEY,
    availability TEXT NOT NULL DEFAULT 'unknown',
    signature   TEXT NOT NULL DEFAULT '',
    report      TEXT NOT NULL DEFAULT '{}',
    queried_at  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_release_availability ON release_report(availability);
CREATE TABLE IF NOT EXISTS release_attempt (
    number TEXT PRIMARY KEY,
    report TEXT NOT NULL,
    queried_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS release_lookup_queue (
    number      TEXT PRIMARY KEY,
    priority    INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def _key(number: str) -> str:
    """缓存键统一走 normalize_number —— ``ABC-123`` 与 ``ABC123`` 是同一个作品。"""
    return normalize_number(number)


def _dump(report: ReleaseReport) -> str:
    return json.dumps(
        {
            "number": report.number,
            "availability": report.availability.value,
            "note": report.note,
            "elapsed": report.elapsed,
            "queried_at": report.queried_at,
            "sources": report.sources,
            "status": report.status.value,
            "candidates": [
                {
                    "source": c.source,
                    "number": c.number,
                    "title": c.title,
                    "page_url": c.page_url,
                    "maker": c.maker,
                    "label": c.label,
                    "release_date": c.release_date,
                    "duration_minutes": c.duration_minutes,
                    "categories": list(c.categories),
                    "uncensored": c.uncensored,
                    "uncensored_reason": c.uncensored_reason,
                    "uncensored_confidence": c.uncensored_confidence,
                    "magnets": [
                        {"uri": m.uri, "name": m.name, "size_mb": m.size_mb,
                         "files": m.files, "date": m.date, "tags": list(m.tags)}
                        for m in c.magnets
                    ],
                }
                for c in report.candidates
            ],
        },
        ensure_ascii=False,
    )


def _load(raw: str) -> ReleaseReport:
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("报告必须为对象")
    candidates = []
    for item in data.get("candidates") or []:
        candidates.append(ReleaseCandidate(
            source=str(item.get("source", "")),
            number=str(item.get("number", "")),
            title=str(item.get("title", "")),
            page_url=str(item.get("page_url", "")),
            maker=str(item.get("maker", "")),
            label=str(item.get("label", "")),
            release_date=str(item.get("release_date", "")),
            duration_minutes=int(item.get("duration_minutes", 0) or 0),
            categories=tuple(item.get("categories") or ()),
            uncensored=bool(item.get("uncensored")),
            uncensored_reason=str(item.get("uncensored_reason", "")),
            uncensored_confidence=float(item.get("uncensored_confidence", 0.0) or 0.0),
            magnets=tuple(
                MagnetLink(
                    uri=str(m.get("uri", "")),
                    name=str(m.get("name", "")),
                    size_mb=int(m.get("size_mb", 0) or 0),
                    files=int(m.get("files", 0) or 0),
                    date=str(m.get("date", "")),
                    tags=tuple(m.get("tags") or ()),
                )
                for m in item.get("magnets") or []
            ),
        ))
    return ReleaseReport(
        number=str(data.get("number", "")),
        availability=Availability(data.get("availability", "unknown")),
        candidates=tuple(candidates),
        sources=dict(data.get("sources") or {}),
        note=str(data.get("note", "")),
        elapsed=float(data.get("elapsed", 0.0) or 0.0),
        queried_at=float(data.get("queried_at", 0.0) or 0.0),
        status=ProcessingStatus(data.get("status", "completed")),
    )


@dataclass
class ReleaseStats:
    total: int = 0
    confirmed: int = 0
    likely: int = 0
    none: int = 0
    unknown: int = 0

    def as_text(self) -> str:
        return (f"已查 {self.total} · 确有 {self.confirmed} · 疑似 {self.likely}"
                f" · 未见 {self.none} · 未查 {self.unknown}")


class ReleaseStore:
    """番号级检索缓存 + 待查队列。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ── 报告 ──
    def get(self, number: str) -> ReleaseReport | None:
        key = _key(number)
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report FROM release_report WHERE number = ?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            return _load(row[0])
        except (ValueError, TypeError, AttributeError, KeyError):
            return None

    def get_many(self, numbers) -> dict[str, ReleaseReport]:
        return self._get_many('release_report', numbers)

    def latest_attempts(self, numbers) -> dict[str, ReleaseReport]:
        return self._get_many('release_attempt', numbers)

    def fresh_many(self, numbers, *, signature, ttl_seconds):
        keys = list(dict.fromkeys(_key(n) for n in numbers if _key(n)))
        out = {}
        with self._connect() as conn:
            for offset in range(0, len(keys), 800):
                batch = keys[offset:offset + 800]
                rows = conn.execute(
                    'SELECT r.number,r.report,r.signature,a.report FROM release_report r '
                    'LEFT JOIN release_attempt a ON a.number=r.number WHERE r.number IN '
                    f"({','.join('?' for _ in batch)})", batch).fetchall()
                for key, raw, stored_signature, attempt_raw in rows:
                    if signature != stored_signature:
                        continue
                    try:
                        report = _load(raw)
                        attempt = _load(attempt_raw) if attempt_raw else None
                    except (ValueError, TypeError, AttributeError, KeyError):
                        continue
                    if report.status is not ProcessingStatus.COMPLETED:
                        continue
                    if ttl_seconds > 0 and _now() - report.queried_at > ttl_seconds:
                        continue
                    if (attempt and attempt.queried_at >= report.queried_at
                            and attempt.status is not ProcessingStatus.COMPLETED):
                        continue
                    out[key] = report
        return out

    def _get_many(self, table, numbers) -> dict[str, ReleaseReport]:
        keys = list(dict.fromkeys(_key(n) for n in numbers if _key(n)))
        out = {}
        with self._connect() as conn:
            for offset in range(0, len(keys), 800):
                batch = keys[offset:offset + 800]
                rows = conn.execute(f"SELECT number,report FROM {table} WHERE number IN "
                                    f"({','.join('?' for _ in batch)})", batch).fetchall()
                for key, raw in rows:
                    try:
                        out[key] = _load(raw)
                    except (ValueError, TypeError, AttributeError, KeyError):
                        continue
        return out

    def get_fresh(self, number: str, *, ttl_seconds: float) -> ReleaseReport | None:
        """只返回未过期的报告。``ttl_seconds <= 0`` 表示永不过期。"""
        report = self.get(number)
        if report is None:
            return None
        if report.status is not ProcessingStatus.COMPLETED:
            return None
        if ttl_seconds > 0 and _now() - report.queried_at > ttl_seconds:
            return None
        return report

    def put(self, report: ReleaseReport, signature: str = "") -> None:
        """写一条报告。``signature`` 记录"用哪些源/哪个算法版本查出来的"。"""
        key = _key(report.number)
        if not key:
            return
        report.queried_at = report.queried_at or _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO release_report (number, availability, signature, report, queried_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(number) DO UPDATE SET availability=excluded.availability,"
                " signature=excluded.signature, report=excluded.report,"
                " queried_at=excluded.queried_at",
                (key, report.availability.value, signature or "",
                 _dump(report), report.queried_at or _now()),
            )

    def signature_matches(self, number: str, signature: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT signature FROM release_report WHERE number = ?", (_key(number),),
            ).fetchone()
        return bool(row and row[0] == signature)

    def delete(self, number: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM release_report WHERE number = ?", (_key(number),))

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM release_report")

    def stats(self) -> ReleaseStats:
        stats = ReleaseStats()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT availability, COUNT(*) FROM release_report GROUP BY availability"
            ).fetchall()
        for value, count in rows:
            count = int(count)
            stats.total += count
            if value in ("confirmed", "likely", "none", "unknown"):
                setattr(stats, value, getattr(stats, value) + count)
            else:
                stats.unknown += count
        return stats

    def numbers_with(self, availability: Availability) -> set[str]:
        with self._connect() as conn:
            return {
                row[0] for row in conn.execute(
                    "SELECT number FROM release_report WHERE availability = ?",
                    (availability.value,),
                )
            }

    # ── 待查队列 ──
    def enqueue(self, numbers, *, priority: int = 1, note: str = "") -> int:
        added = 0
        with self._connect() as conn:
            for number in numbers:
                key = _key(number)
                if not key:
                    continue
                conn.execute(
                    "INSERT INTO release_lookup_queue (number, priority, note, status, updated_at)"
                    " VALUES (?,?,?,'pending',datetime('now'))"
                    " ON CONFLICT(number) DO UPDATE SET status='pending',"
                    " priority=MIN(priority, excluded.priority), note=excluded.note",
                    (key, int(priority), note),
                )
                added += 1
        return added

    def pending(self, limit: int = 200) -> list[str]:
        with self._connect() as conn:
            return [
                row[0] for row in conn.execute(
                    "SELECT number FROM release_lookup_queue WHERE status='pending'"
                    " ORDER BY priority, number LIMIT ?", (int(limit),),
                )
            ]

    def done(self, number: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE release_lookup_queue SET status='done', updated_at=datetime('now')"
                " WHERE number = ?", (_key(number),),
            )

    def queue_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            return dict(conn.execute(
                "SELECT status, COUNT(*) FROM release_lookup_queue GROUP BY status"
            ))

    def record_attempt(self, report: ReleaseReport) -> None:
        if not _key(report.number):
            return
        with self._connect() as conn:
            conn.execute("INSERT INTO release_attempt VALUES (?,?,?) ON CONFLICT(number) "
                         "DO UPDATE SET report=excluded.report,queried_at=excluded.queried_at",
                         (_key(report.number), _dump(report), report.queried_at or _now()))

    def latest_attempt(self, number: str) -> ReleaseReport | None:
        with self._connect() as conn:
            row = conn.execute("SELECT report FROM release_attempt WHERE number=?",
                               (_key(number),)).fetchone()
        if row:
            try:
                return _load(row[0])
            except (ValueError, TypeError, AttributeError, KeyError):
                return None
        return None

    def set_queue_status(self, number: str, status: str) -> None:
        if status not in {"pending", "running", "failed", "done"}:
            raise ValueError("无效的检索队列状态")
        with self._connect() as conn:
            conn.execute("UPDATE release_lookup_queue SET status=?,updated_at=datetime('now') "
                         "WHERE number=?", (status, _key(number)))

    def retry_failed(self) -> int:
        with self._connect() as conn:
            return conn.execute("UPDATE release_lookup_queue SET status='pending' "
                                "WHERE status IN ('failed','running')").rowcount
