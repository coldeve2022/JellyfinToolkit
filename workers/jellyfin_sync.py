"""Reviewable server tag plans delegate to the same pure transaction as CLI/automation."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
from PySide6.QtCore import QThread, Signal
from config import DATA_DIR
from utils import jellyfin_api


def latest_backup_path(server: str = ""):
    for path in reversed(sorted(DATA_DIR.glob("jellyfin_clean_backup_*.json"))):
        try:
            jellyfin_api.load_dirty_backup(str(path), server=server)
            return str(path)
        except Exception:
            continue
    return None


class _SyncWorker(QThread):
    log = Signal(str)
    finished = Signal(dict)

    def __init__(self, server, api_key, parent=None):
        super().__init__(parent)
        self.server, self.api_key = server, api_key
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()


class JellyfinScanWorker(_SyncWorker):
    def run(self):
        try:
            plan = jellyfin_api.scan_dirty(self.server, self.api_key, log=self.log.emit,
                                          should_stop=lambda: self._stopped)
            if self._stopped:
                self.finished.emit({"cancelled": True, "status": "cancelled", "error": "扫描已取消"})
            else:
                self.finished.emit({"plan": plan, "server": jellyfin_api._base(self.server),
                                    "scanned": len(plan), "error": None, "status": "succeeded"})
        except Exception as exc:
            self.finished.emit({"error": str(exc), "status": "failed"})


class JellyfinCleanWorker(_SyncWorker):
    def __init__(self, server, api_key, refresh=True, parent=None, dirty_plan=None):
        super().__init__(server, api_key, parent)
        self.refresh = refresh
        self.dirty_plan = deepcopy(dirty_plan)

    def run(self):
        try:
            dirty = self.dirty_plan if self.dirty_plan is not None else jellyfin_api.scan_dirty(
                self.server, self.api_key, log=self.log.emit, should_stop=lambda: self._stopped)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = str(DATA_DIR / f"jellyfin_clean_backup_{timestamp}.json")
            result = jellyfin_api.execute_clean_plan(self.server, self.api_key, dirty, backup,
                refresh=self.refresh, should_stop=lambda: self._stopped, log=self.log.emit)
            self.finished.emit(result)
        except Exception as exc:
            self.finished.emit({"scanned": 0, "cleaned": 0, "backup": None, "error": str(exc),
                                "cancelled": self._stopped,
                                "status": "cancelled" if self._stopped else "failed"})


class JellyfinRestoreWorker(_SyncWorker):
    def __init__(self, server, api_key, backup_path, refresh=True, parent=None):
        super().__init__(server, api_key, parent)
        self.backup_path, self.refresh = backup_path, refresh

    def run(self):
        result = jellyfin_api.execute_restore_plan(self.server, self.api_key, self.backup_path,
            refresh=self.refresh, should_stop=lambda: self._stopped, log=self.log.emit)
        self.finished.emit(result)
