"""数据库操作 — Jellyfin SQLite 跨库数据转移。

核心逻辑 `transfer_userdata()` 为纯函数（不依赖 Qt），可单元测试；
`CrossDBTransferWorker` 仅负责线程包装与信号发射。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal


def transfer_userdata(
    old_db: str | Path,
    current_db: str | Path,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """从旧库备份转移 .ts 视频的播放记录到新库对应的 .mp4 记录。

    流程：
    1. 从旧库读取所有 .ts 的 BaseItems 记录与全部 UserData（多用户）
    2. 在新库中按路径 .ts → .mp4 查找对应记录
    3. 清空新记录的空数据，注入旧播放记录（进度/收藏/评分等）

    Args:
        old_db: 旧库备份路径
        current_db: 当前 jellyfin.db 路径
        log: 日志回调（可选）
        progress: 进度回调 0-100（可选）
        should_stop: 停止检查回调，返回 True 时中断（可选）

    Returns:
        dict: {"matched": 配对数, "transferred": 转移记录数, "not_found": 未找到数,
               "error": 异常信息（可选）}

    Raises:
        FileNotFoundError: 任一数据库文件不存在
    """

    def _log(msg: str) -> None:
        if log:
            log(msg)

    old_db = Path(old_db)
    current_db = Path(current_db)
    if not old_db.exists():
        raise FileNotFoundError(f"旧库不存在: {old_db}")
    if not current_db.exists():
        raise FileNotFoundError(f"新库不存在: {current_db}")

    stats = {"matched": 0, "transferred": 0, "not_found": 0, "preserved": 0, "cancelled": False}

    fields = [
        "ItemId",
        "UserId",
        "CustomDataKey",
        "AudioStreamIndex",
        "IsFavorite",
        "LastPlayedDate",
        "Likes",
        "PlayCount",
        "PlaybackPositionTicks",
        "Played",
        "Rating",
        "SubtitleStreamIndex",
        "RetentionDate",
    ]

    def columns(conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def require_schema(conn):
        if not {"Id", "Path"} <= columns(conn, "BaseItems") or not {
            "ItemId",
            "UserId",
            "CustomDataKey",
        } <= columns(conn, "UserData"):
            raise sqlite3.DatabaseError(
                "当前数据库结构不支持安全播放记录迁移（需 BaseItems/UserData 身份字段），已拒绝写入"
            )

    def meaningful(ud):
        return any(ud[i] not in (None, "", 0, False) for i in (4, 5, 6, 7, 8, 9, 10))

    def rank(ud):
        return (str(ud[5] or ""), int(ud[9] or 0), int(ud[7] or 0), int(ud[8] or 0))

    conn_old = sqlite3.connect(str(old_db))
    c_old = conn_old.cursor()
    try:
        require_schema(conn_old)
        c_old.execute("SELECT Id, Path FROM BaseItems WHERE Path LIKE '%.ts'")
        old_items = c_old.fetchall()
        _log(f"📋 从旧库读取到 {len(old_items)} 条 .ts 记录")

        old_columns = columns(conn_old, "UserData")
        c_old.execute(
            "SELECT "
            + ",".join(f if f in old_columns else f"NULL AS {f}" for f in fields)
            + " FROM UserData"
        )
        old_userdata: dict = defaultdict(list)
        for row in c_old.fetchall():
            old_userdata[row[0]].append(row)
    finally:
        conn_old.close()

    conn_new = sqlite3.connect(str(current_db))
    c_new = conn_new.cursor()
    try:
        require_schema(conn_new)
        new_columns = columns(conn_new, "UserData")
        has_key = "PresentationUniqueKey" in columns(conn_new, "BaseItems")
        conn_new.execute("BEGIN IMMEDIATE")
        for i, (old_id, old_path) in enumerate(old_items):
            if should_stop and should_stop():
                conn_new.rollback()
                stats.update(cancelled=True, transferred=0)
                _log("已停止，当前迁移事务已回滚")
                return stats
            if not old_path or not str(old_path).lower().endswith(".ts"):
                continue

            new_path = str(old_path)[:-3] + ".mp4"
            c_new.execute(
                "SELECT Id,"
                + ("PresentationUniqueKey" if has_key else "NULL")
                + " FROM BaseItems WHERE Path = ? COLLATE NOCASE",
                (new_path,),
            )
            row = c_new.fetchone()
            if not row:
                stats["not_found"] += 1
                _log(f"  ⚠️ 未找到: {Path(new_path).name}")
                continue

            stats["matched"] += 1
            new_id = row[0]
            new_key = row[1] or (
                uuid.UUID(bytes_le=new_id).hex
                if isinstance(new_id, bytes) and len(new_id) == 16
                else str(new_id).lower()
            )

            if old_id in old_userdata:
                users = {}
                for ud in old_userdata[old_id]:
                    if ud[1] not in users or rank(ud) > rank(users[ud[1]]):
                        users[ud[1]] = ud
                for ud in users.values():
                    user_id = ud[1]
                    existing = c_new.execute(
                        "SELECT "
                        + ",".join(f if f in new_columns else f"NULL AS {f}" for f in fields)
                        + " FROM UserData WHERE ItemId=? AND UserId=?",
                        (new_id, user_id),
                    ).fetchall()
                    if any(meaningful(current) for current in existing):
                        stats["preserved"] += 1
                        continue
                    actual_key = next((current[2] for current in existing if current[2]), new_key)
                    c_new.execute(
                        "DELETE FROM UserData WHERE ItemId = ? AND UserId = ?",
                        (new_id, user_id),
                    )
                    values = (new_id, user_id, actual_key, *ud[3:])
                    writable = [(f, value) for f, value in zip(fields, values) if f in new_columns]
                    c_new.execute(
                        "INSERT INTO UserData ("
                        + ",".join(f for f, _ in writable)
                        + ") VALUES ("
                        + ",".join("?" for _ in writable)
                        + ")",
                        tuple(value for _, value in writable),
                    )
                    stats["transferred"] += 1

            if progress and old_items:
                progress(int((i + 1) / len(old_items) * 100))

        if should_stop and should_stop():
            conn_new.rollback()
            stats.update(cancelled=True, transferred=0)
        else:
            conn_new.commit()
    except Exception:
        conn_new.rollback()
        raise
    finally:
        conn_new.close()

    _log("─" * 50)
    _log(
        f"✅ 完成！配对: {stats['matched']} | 转移播放记录: {stats['transferred']} "
        f"| 未匹配: {stats['not_found']}"
    )
    return stats


class CrossDBTransferWorker(QThread):
    """ts → mp4 跨库播放记录转移 Worker（包装 transfer_userdata）。"""

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # 统计结果

    def __init__(self, data_dir: str, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        current_db = self.data_dir / "jellyfin.db"
        if not current_db.exists():
            self.log.emit(f"❌ 找不到数据库: {current_db}")
            self.finished.emit({"error": "db_not_found"})
            return

        backups = sorted(self.data_dir.glob("jellyfin.db.backup_*"))
        if not backups:
            self.log.emit("❌ 找不到任何历史备份！")
            self.finished.emit({"error": "no_backup"})
            return

        oldest = backups[-1]
        self.log.emit(f"📦 旧库: {oldest.name}")
        self.log.emit(f"📦 新库: {current_db.name}")

        # 操作前先为当前库做安全备份
        safe_backup = current_db.with_name(
            f"jellyfin.db.cross_backup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        )
        try:
            from utils.file_ops import snapshot_sqlite

            snapshot_sqlite(current_db, safe_backup)
            self.log.emit(f"✅ 当前库已备份至: {safe_backup.name}")
        except OSError as e:
            self.log.emit(f"❌ 备份当前库失败，已中止: {e}")
            self.finished.emit({"error": f"backup_failed: {e}"})
            return

        try:
            stats = transfer_userdata(
                oldest,
                current_db,
                log=self.log.emit,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
            )
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ 异常: {e}")
            stats = {"matched": 0, "transferred": 0, "not_found": 0, "error": str(e)}

        self.finished.emit(stats)
