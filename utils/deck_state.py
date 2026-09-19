"""快速打标队列状态 - SQLite(data/smart.db)，记录排序模式与"跳过(搁置)"集合。

与 LabelStore 同库但独立表，语义清晰：
- 排序顺序：命中度升序(先处理不太可能喜欢的) 或 随机。
- 跳过(搁置)：暂时不看、不删、不打标；落库后下次打开不再按原顺序被拉出来，
  可随时"恢复跳过的"重新纳入队列（配合随机顺序，避免一毛一样从头处理）。
纯函数/轻依赖，路径可注入，方便测试。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

ORDER_SCORE = "score"      # 按命中度升序(先处理不太可能喜欢的)
ORDER_RANDOM = "random"    # 随机洗牌


class DeckState:
    """持久化打标队列的排序模式与跳过状态。线程安全：每次操作独立短连接。"""

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
                """CREATE TABLE IF NOT EXISTS deck_state(
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS deck_skipped(
                    item_id TEXT PRIMARY KEY,
                    skipped_at TEXT NOT NULL
                )"""
            )

    def _get(self, key: str, default=None):
        with self._connect() as conn:
            row = conn.execute("SELECT v FROM deck_state WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def _set(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO deck_state(k, v) VALUES(?,?)
                   ON CONFLICT(k) DO UPDATE SET v=excluded.v""",
                (key, value),
            )

    # ── 排序模式 ──
    def get_order(self) -> str:
        return self._get("order", ORDER_SCORE)

    def set_order(self, mode: str) -> None:
        if mode not in (ORDER_SCORE, ORDER_RANDOM):
            mode = ORDER_SCORE
        self._set("order", mode)

    # ── 跳过(搁置) ──
    def skipped(self) -> set[str]:
        with self._connect() as conn:
            return {r["item_id"] for r in conn.execute("SELECT item_id FROM deck_skipped")}

    def add_skipped(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO deck_skipped(item_id, skipped_at) VALUES(?,?)
                   ON CONFLICT(item_id) DO NOTHING""",
                (item_id, datetime.now().isoformat(timespec="seconds")),
            )

    def remove_skipped(self, item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM deck_skipped WHERE item_id=?", (item_id,))

    def clear_skipped(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM deck_skipped")
