"""后台扫描 Worker — 通用的文件系统遍历与视频发现。"""

from pathlib import Path
from typing import List, Set, Optional

from PySide6.QtCore import QThread, Signal

from utils.library import is_junk_attachment_path


class FileScannerWorker(QThread):
    """遍历目录，收集符合条件的文件路径。

    支持暂停/停止；支持传入已检查集合实现断点续传。
    """

    progress = Signal(str, int)            # 当前文件名, 进度百分比
    file_found = Signal(str, str)          # 文件名, 绝对路径
    skipped = Signal(int)                  # 跳过的附属文件数（预告片/主题视频等）
    finished = Signal(list, list)          # 找到的文件路径列表, 已检查路径列表

    def __init__(self, directories: List[str], extensions: Set[str],
                 min_size_bytes: int = 0,
                 checked_files: Optional[Set[str]] = None,
                 parent=None):
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

    def run(self) -> None:
        # 第一遍：收集所有候选文件（仅统计，不做 IO 过滤）
        all_files: List[Path] = []
        for d in self._dirs:
            p = Path(d)
            try:
                if p.is_file():
                    if p.suffix.lower() in self._exts and len(str(p)) <= 240:
                        all_files.append(p)
                elif p.is_dir():
                    for f in p.rglob("*"):
                        # rglob 对超长路径可能抛 OSError，跳过而非崩溃
                        if not f.is_file() or len(str(f)) > 240:
                            continue
                        if f.suffix.lower() in self._exts:
                            all_files.append(f)
            except OSError:
                continue  # 无权限/超长路径目录跳过，不中断整体扫描

        total = len(all_files)
        results: List[str] = []

        for i, fp in enumerate(all_files):
            while self._paused and not self._stopped:
                self.msleep(100)
            if self._stopped:
                return

            path_str = str(fp)
            if path_str in self._checked:
                # 已检查过，但仍计入进度
                self.progress.emit(fp.name, int((i + 1) / total * 100))
                continue

            # 预告片 / 主题视频（trailers\、theme_video 等）是 Jellyfin 生成给界面用的，
            # 不是正片 —— 给它们生成字幕既没意义又会把列表淹掉。
            if is_junk_attachment_path(path_str):
                self._skipped += 1
                self._checked.add(path_str)
                self.progress.emit(fp.name, int((i + 1) / total * 100))
                continue

            try:
                if self._min_size > 0 and fp.stat().st_size < self._min_size:
                    self._checked.add(path_str)
                    self.progress.emit(fp.name, int((i + 1) / total * 100))
                    continue
                results.append(path_str)
                self.file_found.emit(fp.name, path_str)
            except OSError:
                pass

            self._checked.add(path_str)
            pct = int((i + 1) / total * 100)
            self.progress.emit(fp.name, pct)

        if self._skipped:
            self.skipped.emit(self._skipped)
        self.finished.emit(results, list(self._checked))
