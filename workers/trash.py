"""批量移入回收站后台线程。"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from utils.trash import DeleteJournal, trash_file


class TrashWorker(QThread):
    """逐条把文件移入回收站，带进度/日志/统计。"""

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # {"ok": int, "fail": int, "fails": [(path, err)]}

    def __init__(self, targets: list, journal: DeleteJournal | None = None,
                 reason: str = "", parent=None):
        """targets: list[tuple(path, size_bytes|None)]"""
        super().__init__(parent)
        self.targets = targets
        self.journal = journal
        self.reason = reason
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        ok = fail = 0
        fails: list = []
        total = len(self.targets)
        for i, (path, size) in enumerate(self.targets):
            if self._stopped:
                break
            succ, err = trash_file(path)
            if succ:
                ok += 1
                if self.journal:
                    self.journal.record(path, size, self.reason)
                self.log.emit(f"🗑️ 已移入回收站: {path}")
            else:
                fail += 1
                fails.append((path, err))
                self.log.emit(f"❌ 失败(已保留原文件): {path} — {err}")
            if total:
                self.progress.emit(int((i + 1) / total * 100))
        self.log.emit(f"完成：成功 {ok} / 失败 {fail}")
        self.finished.emit({"ok": ok, "fail": fail, "fails": fails})
