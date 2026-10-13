"""后台扫描 Worker — 通用的文件系统遍历与视频发现。"""

from pathlib import Path
from typing import List, Set, Optional

from PySide6.QtCore import QThread, Signal


class FileScannerWorker(QThread):
    """遍历目录，收集符合条件的文件路径。

    支持暂停/停止；支持传入已检查集合实现断点续传。
    """

    progress = Signal(str, int)  # 当前文件名, 进度百分比
    file_found = Signal(str, str)  # 文件名, 绝对路径
    skipped = Signal(int)  # 跳过的附属文件数（预告片/主题视频等）
    finished = Signal(list, list)  # 找到的文件路径列表, 已检查路径列表

    def __init__(
        self,
        directories: List[str],
        extensions: Set[str],
        min_size_bytes: int = 0,
        checked_files: Optional[Set[str]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._dirs = directories
        self._exts = {e.lower() for e in extensions}
        self._min_size = min_size_bytes
        self._checked: Set[str] = set(checked_files or [])
        self._paused = False
        self._stopped = False
        #: 被跳过的附属文件数（预告片 / 主题视频），供界面如实汇报
        self._skipped = 0

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stopped = True
        self._paused = False

    @property
    def cancelled(self) -> bool:
        return self._stopped

    def run(self) -> None:
        from utils.file_scan import iter_media_files

        results = []

        def cancelled():
            while self._paused and not self._stopped:
                self.msleep(100)
            return self._stopped

        try:
            candidates = []
            for path in iter_media_files(self._dirs, video_exts=self._exts, should_stop=cancelled):
                candidates.append(Path(path))
                self.progress.emit(Path(path).name, 0)
            total = len(candidates)
            for i, fp in enumerate(candidates):
                if cancelled():
                    break
                key = str(fp)
                if key not in self._checked:
                    try:
                        if not self._min_size or fp.stat().st_size >= self._min_size:
                            results.append(key)
                            self.file_found.emit(fp.name, key)
                        self._checked.add(key)
                    except OSError:
                        pass  # Transient read failure is not a completed checkpoint.
                self.progress.emit(fp.name, int((i + 1) / total * 100) if total else 100)
        finally:
            self.finished.emit(results, list(self._checked))
