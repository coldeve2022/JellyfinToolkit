"""人工打标存储 - SQLite(data/smart.db)，画像反馈的依据。
纯函数/轻依赖，路径可注入，方便测试。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.scoring import LABEL_DELETE, LABEL_DISLIKE, LABEL_LIKE, LABLE_KEEP, LABEL_REVIEW

ALL_LABELS = (LABLE_KEEP, LABEL_LIKE, LABEL_DISLIKE, LABEL_DELETE, LABEL_REVIEW)


def legacy_protection_labels(path, items, labels=None):
    """Unconfirmed old positive feedback protects entities but never teaches a new viewer."""
    from utils.scoring import POSITIVE_LABELS, entity_key
    labels = labels or {}
    legacy = LabelStore(path).all()
    protected = {entity_key(it.path) for it in items if legacy.get(it.id) in POSITIVE_LABELS}
    return {it.id: LABLE_KEEP for it in items
            if it.id not in labels and (legacy.get(it.id) in POSITIVE_LABELS
                                      or entity_key(it.path) in protected)}


def _migration_context(cfg, items):
    from utils.state_context import state_context, resolved_identity
    if not items or any(not it.source_identity or not it.behavior_user_id for it in items):
        raise ValueError("需先采集并确认实际库来源和观看用户，才能采纳旧反馈")
    user, source = resolved_identity(items, cfg)
    context = state_context(cfg, items)
    if not user or not source or source == "source:unknown" or context != state_context(cfg):
        raise ValueError("当前来源/观看用户与旧预览不一致，请重新采集并预览")
    return context


def preview_legacy_labels(path, cfg, items):
    """Review only old records matching a confirmed current snapshot, without copying them."""
    context = _migration_context(cfg, items)
    current, legacy = LabelStore(path, context=context).all(), LabelStore(path).all()
    seen, preview = set(), []
    for it in items:
        if it.id not in current and it.id in legacy and it.id not in seen:
            preview.append({"item_id": it.id, "name": it.name, "label": legacy[it.id]})
            seen.add(it.id)
    return preview


def adopt_legacy_labels(path, cfg, items, item_ids):
    """Explicit selection; atomic insert-only per record, legacy data stays intact."""
    preview = preview_legacy_labels(path, cfg, items)
    context = _migration_context(cfg, items)
    selected = set(item_ids)
    target = LabelStore(path, context=context)._scoped
    return sum(target.set_if_absent(row["item_id"], {"label": row["label"], "note": "用户确认采纳旧反馈"})
               for row in preview if row["item_id"] in selected)


class LabelStore:
    """持久化人工打标。线程安全：每次操作独立短连接。"""

    def __init__(self, path: str | Path, context: str = ""):
        self.path = Path(path)
        self.context = context
        from utils.state_context import ScopedState
        self._scoped = ScopedState(path, "labels", context) if context else None
        if self._scoped:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS labels(
                    item_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )

    def set(self, item_id: str, label: str, note: str = "") -> None:
        if label not in ALL_LABELS:
            raise ValueError(f"非法标签: {label}")
        if self._scoped:
            self._scoped.set(item_id, {"label": label, "note": note})
            return
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO labels(item_id, label, note, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(item_id)
                   DO UPDATE SET label=excluded.label, note=excluded.note,
                                 updated_at=excluded.updated_at""",
                (item_id, label, note, datetime.now().isoformat(timespec="seconds")),
            )

    def get(self, item_id: str) -> Optional[str]:
        if self._scoped:
            value = self._scoped.get(item_id)
            return value["label"] if value else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT label FROM labels WHERE item_id=?", (item_id,)
            ).fetchone()
            return row["label"] if row else None

    def all(self) -> dict[str, str]:
        if self._scoped:
            return {key: value["label"] for key, value in self._scoped.all().items()}
        with self._connect() as conn:
            return {r["item_id"]: r["label"] for r in conn.execute("SELECT item_id,label FROM labels")}

    def count(self) -> int:
        if self._scoped:
            return len(self.all())
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]

    def remove(self, item_id: str) -> None:
        if self._scoped:
            self._scoped.remove(item_id)
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM labels WHERE item_id=?", (item_id,))

    def clear(self) -> None:
        if self._scoped:
            self._scoped.clear()
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM labels")

    def summary(self) -> dict[str, int]:
        if self._scoped:
            from collections import Counter
            return dict(Counter(self.all().values()))
        with self._connect() as conn:
            rows = conn.execute("SELECT label, COUNT(*) c FROM labels GROUP BY label").fetchall()
            return {r["label"]: r["c"] for r in rows}
