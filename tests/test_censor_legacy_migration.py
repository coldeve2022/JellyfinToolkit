"""旧库升级后，存量扫描结果不能被当成"没跑完"。

`status` 列的默认值是 `partial`（表示"已入库但还没分析过"）。老版本的
`censor_probe` 只在分析跑完后才写行，所以升级时新建的这一列必须回填成
`completed`；否则整个历史库都会显示"未跑完"，并且严格读缓存一律 miss
（每次打开都全库重扫）。
"""

from __future__ import annotations

import sqlite3

from censor.models import ProcessingStatus
from censor.store import VerdictStore

#: v2 之前的 censor_probe：没有 status / analysis_signature / file_identity 等列
_LEGACY_SCHEMA = """
CREATE TABLE censor_probe (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    duration REAL NOT NULL DEFAULT 0,
    number TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'unknown',
    confidence REAL NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    candidates TEXT NOT NULL DEFAULT '[]',
    visual_score REAL NOT NULL DEFAULT 0,
    elapsed REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""


def _legacy_db(path, rows=3):
    connection = sqlite3.connect(path)
    connection.executescript(_LEGACY_SCHEMA)
    connection.executemany(
        "INSERT INTO censor_probe(path, state) VALUES (?, 'censored')",
        [(f"old-{index}.mp4",) for index in range(rows)],
    )
    connection.commit()
    connection.close()


def test_migration_backfills_status_completed_for_legacy_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, rows=3)

    store = VerdictStore(db)
    stats = store.stats()

    assert stats.total == 3
    assert stats.incomplete == 0, "升级上来的历史行应视为已完整分析"

    with sqlite3.connect(db) as connection:
        statuses = {row[0] for row in connection.execute("SELECT status FROM censor_probe")}
    assert statuses == {ProcessingStatus.COMPLETED.value}


def test_migration_does_not_mark_newly_inserted_rows_completed(tmp_path):
    """迁移只对"升级时新建这一列"的存量行回填，不能影响之后插入的裸行。"""
    db = tmp_path / "fresh.db"
    store = VerdictStore(db)  # 全新库，迁移正常跑一遍

    with store._connect() as connection:
        connection.execute("INSERT INTO censor_probe(path) VALUES ('later.mp4')")

    assert store.stats().incomplete == 1
