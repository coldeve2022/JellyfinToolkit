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


class LabelStore:
    """持久化人工打标。线程安全：每次操作独立短连接。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
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
        with self._connect() as conn:
            row = conn.execute(
                "SELECT label FROM labels WHERE item_id=?", (item_id,)
            ).fetchone()
            return row["label"] if row else None

    def all(self) -> dict[str, str]:
        with self._connect() as conn:
            return {r["item_id"]: r["label"] for r in conn.execute("SELECT item_id,label FROM labels")}

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]

    def remove(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM labels WHERE item_id=?", (item_id,))

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM labels")

    def summary(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT label, COUNT(*) c FROM labels GROUP BY label").fetchall()
            return {r["label"]: r["c"] for r in rows}
