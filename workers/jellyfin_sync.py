"""Jellyfin 同步后台线程 - 清洗脏前缀标签（带备份/回滚）+ 全库刷新。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QThread, Signal

from config import DATA_DIR
from utils import jellyfin_api


def latest_backup_path() -> str | None:
    """返回最新一份清洗备份的路径（data/jellyfin_clean_backup_*.json），无则 None。"""
    files = sorted((DATA_DIR).glob("jellyfin_clean_backup_*.json"))
    return str(files[-1]) if files else None


class JellyfinCleanWorker(QThread):
    """扫描→备份→清洗→刷新。清洗前必先落盘原始 Tags/Genres 到 data/。"""

    log = Signal(str)
    finished = Signal(dict)   # {"scanned":n, "cleaned":n, "backup":path, "error":str|None}

    def __init__(self, server: str, api_key: str, refresh: bool = True,
                 parent=None):
        super().__init__(parent)
        self.server = server
        self.api_key = api_key
        self.refresh = refresh

    def run(self) -> None:
        try:
            self.log.emit("第 1 步：扫描全库脏前缀标签（只读，不改数据）…")
            dirty = jellyfin_api.scan_dirty(self.server, self.api_key, log=self.log.emit)
            self.log.emit(f"发现 {len(dirty)} 条含脏前缀标签。")
            if not dirty:
                self.finished.emit({"scanned": 0, "cleaned": 0, "backup": None,
                                    "error": None, "empty": True})
                return

            # 第 2 步：备份（清洗前必做）
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = str(DATA_DIR / f"jellyfin_clean_backup_{ts}.json")
            jellyfin_api.save_dirty_backup(dirty, backup)
            self.log.emit(f"✅ 已备份 {len(dirty)} 条原始标签 → {backup}")

            # 第 3 步：应用清洗
            cleaned = jellyfin_api.apply_clean(self.server, self.api_key, dirty,
                                               log=self.log.emit)
            self.log.emit(f"已清洗 {cleaned}/{len(dirty)} 条。")

            # 第 4 步（可选）：全库刷新
            if self.refresh:
                ok, msg = jellyfin_api.refresh_library(self.server, self.api_key)
                self.log.emit(("✅ " if ok else "⚠️ ") + msg)

            self.finished.emit({"scanned": len(dirty), "cleaned": cleaned,
                                "backup": backup, "error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ Jellyfin 同步失败: {e}")
            self.finished.emit({"scanned": 0, "cleaned": 0, "backup": None,
                                "error": str(e)})


class JellyfinRestoreWorker(QThread):
    """从最新备份把 Tags/Genres 恢复回 Jellyfin（回滚清洗）。"""

    log = Signal(str)
    finished = Signal(dict)   # {"restored":n, "backup":path, "error":str|None}

    def __init__(self, server: str, api_key: str, backup_path: str,
                 refresh: bool = True, parent=None):
        super().__init__(parent)
        self.server = server
        self.api_key = api_key
        self.backup_path = backup_path
        self.refresh = refresh

    def run(self) -> None:
        try:
            dirty = jellyfin_api.load_dirty_backup(self.backup_path)
            self.log.emit(f"读取备份 {len(dirty)} 条 ← {self.backup_path}")
            restored = jellyfin_api.restore_backup(
                self.server, self.api_key, dirty, log=self.log.emit)
            self.log.emit(f"已恢复 {restored}/{len(dirty)} 条。")
            if self.refresh:
                ok, msg = jellyfin_api.refresh_library(self.server, self.api_key)
                self.log.emit(("✅ " if ok else "⚠️ ") + msg)
            self.finished.emit({"restored": restored, "backup": self.backup_path,
                                "error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ 恢复失败: {e}")
            self.finished.emit({"restored": 0, "backup": self.backup_path,
                                "error": str(e)})
