"""数据库操作 — Jellyfin SQLite 跨库数据转移。

核心逻辑 `transfer_userdata()` 为纯函数（不依赖 Qt），可单元测试；
`CrossDBTransferWorker` 仅负责线程包装与信号发射。
"""

import shutil
import sqlite3
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

    stats = {"matched": 0, "transferred": 0, "not_found": 0}

    conn_old = sqlite3.connect(str(old_db))
    c_old = conn_old.cursor()
    try:
        c_old.execute("SELECT Id, Path FROM BaseItems WHERE Path LIKE '%.ts'")
        old_items = c_old.fetchall()
        _log(f"📋 从旧库读取到 {len(old_items)} 条 .ts 记录")

        c_old.execute(
            """SELECT ItemId, UserId, CustomDataKey, AudioStreamIndex,
                      IsFavorite, LastPlayedDate, Likes, PlayCount,
                      PlaybackPositionTicks, Played, Rating,
                      SubtitleStreamIndex, RetentionDate
               FROM UserData"""
        )
        old_userdata: dict = defaultdict(list)
        for row in c_old.fetchall():
            old_userdata[row[0]].append(row)
    finally:
        conn_old.close()

    conn_new = sqlite3.connect(str(current_db))
    c_new = conn_new.cursor()
    try:
        for i, (old_id, old_path) in enumerate(old_items):
            if should_stop and should_stop():
                break
            if not old_path or not str(old_path).lower().endswith(".ts"):
                continue

            new_path = str(old_path)[:-3] + ".mp4"
            c_new.execute(
                "SELECT Id, PresentationUniqueKey FROM BaseItems WHERE Path = ? COLLATE NOCASE",
                (new_path,),
            )
            row = c_new.fetchone()
            if not row:
                stats["not_found"] += 1
                _log(f"  ⚠️ 未找到: {Path(new_path).name}")
                continue

            stats["matched"] += 1
            new_id = row[0]
            new_key = row[1] if row[1] else new_id.lower()

            if old_id in old_userdata:
                for ud in old_userdata[old_id]:
                    user_id = ud[1]
                    c_new.execute(
                        "DELETE FROM UserData WHERE ItemId = ? AND UserId = ?",
                        (new_id, user_id),
                    )
                    c_new.execute(
                        """INSERT INTO UserData (
                            ItemId, UserId, CustomDataKey, AudioStreamIndex,
                            IsFavorite, LastPlayedDate, Likes, PlayCount,
                            PlaybackPositionTicks, Played, Rating,
                            SubtitleStreamIndex, RetentionDate
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (new_id, user_id, new_key, ud[3], ud[4],
                         ud[5], ud[6], ud[7], ud[8],
                         ud[9], ud[10], ud[11], ud[12]),
                    )
                    stats["transferred"] += 1

            if progress and old_items:
                progress(int((i + 1) / len(old_items) * 100))

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
    finished = Signal(dict)    # 统计结果

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

        oldest = backups[0]
        self.log.emit(f"📦 旧库: {oldest.name}")
        self.log.emit(f"📦 新库: {current_db.name}")

        # 操作前先为当前库做安全备份
        safe_backup = current_db.with_name(
            f"jellyfin.db.cross_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            shutil.copy2(str(current_db), str(safe_backup))
            self.log.emit(f"✅ 当前库已备份至: {safe_backup.name}")
        except OSError as e:
            self.log.emit(f"❌ 备份当前库失败，已中止: {e}")
            self.finished.emit({"error": f"backup_failed: {e}"})
            return

        try:
            stats = transfer_userdata(
                oldest, current_db,
                log=self.log.emit,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
            )
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ 异常: {e}")
            stats = {"matched": 0, "transferred": 0, "not_found": 0, "error": str(e)}

        self.finished.emit(stats)
