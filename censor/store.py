"""检测结果缓存 —— 第二次打开同一部片，不该再花 25 秒。

设计
----
- **键是路径，有效性看 (size, mtime)**：文件被替换过（比如你做了
  破解替换）自然失效重算，不需要用户手动清缓存。
- 证据与候选帧以 JSON 存一行，表结构扁平，方便直接用 sqlite 工具查。
- 另建一张 ``censor_label`` 专门存**人工结论**：它是训练/调参语料，
  也是唯一"永不自动覆盖"的表 —— 算法改版重算时它必须活下来。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from censor.models import (
    CensorState, Evidence, EvidenceSource, FrameCandidate, Verdict, VideoVerdict,
)

__all__ = ["VerdictStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS censor_probe (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    duration    REAL    NOT NULL DEFAULT 0,
    number      TEXT    NOT NULL DEFAULT '',
    provider    TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'unknown',
    confidence  REAL    NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 1,
    reason      TEXT    NOT NULL DEFAULT '',
    evidence    TEXT    NOT NULL DEFAULT '[]',
    candidates  TEXT    NOT NULL DEFAULT '[]',
    visual_score REAL   NOT NULL DEFAULT 0,
    elapsed     REAL    NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_censor_probe_state ON censor_probe(state);

CREATE TABLE IF NOT EXISTS censor_label (
    path        TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    labeled_at  TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dump_evidence(evidence: Iterable[Evidence]) -> str:
    return json.dumps(
        [{"source": ev.source.value, "state": ev.state.value,
          "confidence": ev.confidence, "detail": ev.detail} for ev in evidence],
        ensure_ascii=False,
    )


def _load_evidence(raw: str) -> list[Evidence]:
    out: list[Evidence] = []
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return out
    for item in items or []:
        try:
            out.append(Evidence(
                source=EvidenceSource(item["source"]),
                state=CensorState(item["state"]),
                confidence=float(item.get("confidence", 0.0)),
                detail=str(item.get("detail", "")),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _dump_candidates(candidates: Iterable[FrameCandidate]) -> str:
    return json.dumps(
        [{"timestamp": c.timestamp, "score": c.score, "block_size": c.block_size,
          "box": list(c.box) if c.box else None, "skin_ratio": c.skin_ratio,
          "thumb_path": c.thumb_path} for c in candidates],
        ensure_ascii=False,
    )


def _load_candidates(raw: str) -> list[FrameCandidate]:
    out: list[FrameCandidate] = []
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return out
    for item in items or []:
        try:
            box = item.get("box")
            out.append(FrameCandidate(
                timestamp=float(item.get("timestamp", 0.0)),
                score=float(item.get("score", 0.0)),
                block_size=int(item.get("block_size", 0) or 0),
                box=tuple(box) if box else None,
                skin_ratio=float(item.get("skin_ratio", 0.0) or 0.0),
                thumb_path=str(item.get("thumb_path", "") or ""),
            ))
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class StoreStats:
    """给界面显示"扫了多少、定了多少"。"""

    total: int = 0
    censored: int = 0
    uncensored: int = 0
    unknown: int = 0
    review: int = 0
    labeled: int = 0

    def as_text(self) -> str:
        return (f"已扫 {self.total} · 有码 {self.censored} · 无码 {self.uncensored} · "
                f"未定 {self.unknown} · 待复核 {self.review} · 人工 {self.labeled}")


class VerdictStore:
    """SQLite 结果缓存。线程安全靠"每次操作新建连接"，简单且够用。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ── 基础设施 ──
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── 读写 ──
    def get(self, path: str, size: int = -1, mtime: float = -1.0) -> Optional[VideoVerdict]:
        """读缓存。传了 size/mtime 时会校验是否过期，过期返回 None。"""
        key = str(path)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT size, mtime, duration, number, provider, state, confidence,"
                " needs_review, reason, evidence, candidates, visual_score, elapsed, error"
                " FROM censor_probe WHERE path = ?", (key,),
            ).fetchone()
        if not row:
            return None
        (db_size, db_mtime, duration, number, provider, state, confidence,
         needs_review, reason, evidence, candidates, visual_score, elapsed, error) = row
        if size >= 0 and int(db_size) != int(size):
            return None
        if mtime >= 0 and abs(float(db_mtime) - float(mtime)) > 1e-6:
            return None
        verdict = Verdict(
            state=CensorState(state), confidence=float(confidence),
            evidence=_load_evidence(evidence), needs_review=bool(needs_review),
            reason=reason or "",
        )
        return VideoVerdict(
            path=key, size=int(db_size), mtime=float(db_mtime),
            duration=float(duration), number=number or "", provider=provider or "",
            verdict=verdict, candidates=_load_candidates(candidates),
            visual_score=float(visual_score), elapsed=float(elapsed), error=error or "",
        )

    def put(self, result: VideoVerdict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO censor_probe (path, size, mtime, duration, number, provider,"
                " state, confidence, needs_review, reason, evidence, candidates,"
                " visual_score, elapsed, error, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,"
                " number=excluded.number, provider=excluded.provider, state=excluded.state,"
                " confidence=excluded.confidence, needs_review=excluded.needs_review,"
                " reason=excluded.reason, evidence=excluded.evidence,"
                " candidates=excluded.candidates, visual_score=excluded.visual_score,"
                " elapsed=excluded.elapsed, error=excluded.error, updated_at=excluded.updated_at",
                (str(result.path), int(result.size), float(result.mtime),
                 float(result.duration), result.number, result.provider,
                 result.verdict.state.value, float(result.verdict.confidence),
                 1 if result.verdict.needs_review else 0, result.verdict.reason,
                 _dump_evidence(result.verdict.evidence),
                 _dump_candidates(result.candidates),
                 float(result.visual_score), float(result.elapsed), result.error or "",
                 _now()),
            )

    def delete(self, path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_probe WHERE path = ?", (str(path),))

    def clear_probes(self) -> None:
        """清空自动检测结果，**保留人工结论**。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_probe")

    # ── 人工标注 ──
    def set_label(self, path: str, state: CensorState, note: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO censor_label (path, state, note, labeled_at) VALUES (?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET state=excluded.state,"
                " note=excluded.note, labeled_at=excluded.labeled_at",
                (str(path), state.value, note, _now()),
            )

    def get_label(self, path: str) -> Optional[tuple[CensorState, str]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, note FROM censor_label WHERE path = ?", (str(path),),
            ).fetchone()
        if not row:
            return None
        return CensorState(row[0]), row[1] or ""

    def labels(self) -> dict[str, CensorState]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, state FROM censor_label").fetchall()
        return {path: CensorState(state) for path, state in rows}

    def clear_labels(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_label")

    # ── 统计 ──
    def stats(self) -> StoreStats:
        stats = StoreStats()
        with self._connect() as conn:
            for state, count in conn.execute(
                "SELECT state, COUNT(*) FROM censor_probe GROUP BY state"
            ):
                stats.total += int(count)
                if state == CensorState.CENSORED.value:
                    stats.censored = int(count)
                elif state == CensorState.UNCENSORED.value:
                    stats.uncensored = int(count)
                else:
                    stats.unknown = int(count)
            stats.review = int(conn.execute(
                "SELECT COUNT(*) FROM censor_probe WHERE needs_review = 1"
            ).fetchone()[0])
            stats.labeled = int(conn.execute(
                "SELECT COUNT(*) FROM censor_label"
            ).fetchone()[0])
        return stats

    def review_queue(self, limit: int = 500) -> list[VideoVerdict]:
        """按"分数从高到低"给出待复核列表 —— 最可疑的排前面。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path FROM censor_probe WHERE needs_review = 1"
                " ORDER BY visual_score DESC, updated_at DESC LIMIT ?", (int(limit),),
            ).fetchall()
        out = []
        for (path,) in rows:
            item = self.get(path)
            if item is not None:
                out.append(item)
        return out
