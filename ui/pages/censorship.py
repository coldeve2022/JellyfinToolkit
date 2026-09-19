"""视频有码检测页面 — 拖入文件/文件夹，自动分类有码/无码视频。"""

import datetime
import os
from pathlib import Path
from typing import List, Set

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSplitter,
)

from config import ToolkitConfig
from ui.widgets import DragDropTableWidget, InfoCard
from utils.naming import is_uncensored_filename


class CensorshipScanWorker(QThread):
    """后台扫描线程 — 递归收集视频文件并按文件名分类，避免大目录拖拽卡死 UI。"""

    file_classified = Signal(str, bool)  # 文件路径, 是否无码
    progress = Signal(int, int)          # 已分类数, 总数
    finished = Signal(int, int)          # 无码数量, 有码数量

    def __init__(self, paths: List[str], extensions: Set[str],
                 keywords: List[str], parent=None):
        super().__init__(parent)
        self._paths = paths
        self._exts = {e.lower() for e in extensions}
        self._keywords = keywords
        self._total = 0
        self._done = 0
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _count_videos(self) -> int:
        """预统计视频文件总数（用于进度）。"""
        total = 0
        for p in self._paths:
            pp = Path(p)
            if pp.is_file() and pp.suffix.lower() in self._exts:
                total += 1
            elif pp.is_dir():
                try:
                    total += sum(1 for f in pp.rglob("*")
                                 if f.is_file() and f.suffix.lower() in self._exts)
                except OSError:
                    continue
        return total

    def run(self) -> None:
        self._total = self._count_videos()
        uncensored = 0
        censored = 0
        for p in self._paths:
            if self._stopped:
                return
            pp = Path(p)
            if pp.is_file():
                ok = self._classify(str(pp))
                if ok:
                    uncensored += 1
                else:
                    censored += 1
                self._tick()
            elif pp.is_dir():
                try:
                    for f in pp.rglob("*"):
                        if self._stopped:
                            return
                        if f.is_file() and f.suffix.lower() in self._exts:
                            ok = self._classify(str(f))
                            if ok:
                                uncensored += 1
                            else:
                                censored += 1
                            self._tick()
                except OSError:
                    continue
        self.finished.emit(uncensored, censored)

    def _tick(self) -> None:
        """发射进度（节流：每 10 个文件一次，避免刷屏）。"""
        self._done += 1
        if self._done % 10 == 0 or self._done >= self._total:
            self.progress.emit(self._done, self._total)

    def _classify(self, filepath: str) -> bool:
        """分类单个文件并发射信号，返回是否无码。"""
        is_unc = is_uncensored_filename(os.path.basename(filepath), self._keywords)
        self.file_classified.emit(filepath, is_unc)
        return is_unc


class CensorshipPage(QWidget):
    """拖拽视频文件/文件夹，按文件名关键词分类有码/无码。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setAcceptDrops(True)
        self._worker: CensorshipScanWorker | None = None
        self._known: Set[str] = set()  # 已展示路径（去重）
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("视频有码检测")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "拖拽视频文件或包含视频的文件夹到下方表格区域，自动按文件名关键词分类。\n"
            "左侧 = 天然无码/破解版（含 -U 后缀）| 右侧 = 常规有码视频\n"
            "支持右键打开文件夹、Delete 键删除、排序、多选拖出。"
        )
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)

        left_w = QWidget()
        left_lay = QVBoxLayout(left_w)
        left_lay.setContentsMargins(0, 0, 6, 0)
        left_lay.addWidget(QLabel("✅ 天生无码 / 破解版（无需修复）"))
        self.table_uncensored = DragDropTableWidget(["文件名", "修改日期"])
        left_lay.addWidget(self.table_uncensored)
        splitter.addWidget(left_w)

        right_w = QWidget()
        right_lay = QVBoxLayout(right_w)
        right_lay.setContentsMargins(6, 0, 0, 0)
        right_lay.addWidget(QLabel("🛠️ 常规有码（拖入 Lada 等工具修复）"))
        self.table_censored = DragDropTableWidget(["文件名", "修改日期"])
        right_lay.addWidget(self.table_censored)
        splitter.addWidget(right_w)

        splitter.setSizes([500, 500])
        layout.addWidget(splitter)

        btn_row = QHBoxLayout()
        self._stats_label = QLabel("就绪")
        self._stats_label.setProperty("cssClass", "muted-label")
        btn_row.addWidget(self._stats_label)
        btn_row.addStretch()

        clear_btn = QPushButton("清空所有面板")
        clear_btn.setProperty("cssClass", "danger")
        clear_btn.clicked.connect(self._clear_all)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

    # ── 拖拽事件 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        try:
            paths = []
            for url in event.mimeData().urls():
                p = os.path.normpath(url.toLocalFile())
                if p and os.path.exists(p):
                    paths.append(p)
            if not paths:
                event.ignore()
                return
            event.acceptProposedAction()
            # 延迟到拖拽结束后启动扫描，避免在 drop 回调内创建/启动线程触发 Qt 崩溃
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, lambda: self._start_scan_worker(paths))
        except Exception:
            event.ignore()

    def _start_scan_worker(self, paths: list[str]) -> None:
        self._stats_label.setText("正在扫描...")
        self._worker = CensorshipScanWorker(
            paths,
            set(self.cfg.video_extensions),
            list(self.cfg.uncensored_keywords),
        )
        self._worker.file_classified.connect(self._add_file)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished.connect(self._scan_done)
        self._worker.start()

    def _on_scan_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._stats_label.setText(f"正在扫描: {done}/{total}")
        else:
            self._stats_label.setText(f"正在扫描... 已发现 {done} 个")

    # ── 分类结果回填（主线程执行） ──
    def _add_file(self, filepath: str, is_uncensored: bool) -> None:
        if filepath in self._known:
            return
        self._known.add(filepath)

        filename = os.path.basename(filepath)
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            date_str = mtime.strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            date_str = "未知"

        table = self.table_uncensored if is_uncensored else self.table_censored
        table.add_file_row(filename, date_str, filepath)

    def _scan_done(self, uncensored: int, censored: int) -> None:
        self._update_stats()

    def shutdown(self) -> None:
        """应用退出时调用：停止后台扫描线程。"""
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)

    def _clear_all(self) -> None:
        self.table_uncensored.clear_all()
        self.table_censored.clear_all()
        self._known.clear()
        self._update_stats()

    def _update_stats(self) -> None:
        u = self.table_uncensored.rowCount()
        c = self.table_censored.rowCount()
        self._stats_label.setText(f"无码: {u}  有码: {c}")
