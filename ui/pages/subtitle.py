"""字幕缺失检测页面 — 扫描视频目录，找出没有字幕文件的视频。"""
from __future__ import annotations

import json
import os

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSpinBox, QProgressBar, QMessageBox, QAbstractItemView,
)

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, InfoCard
from workers.scanner import FileScannerWorker


class SubtitlePage(QWidget):
    """扫描视频目录，检测缺少同名字幕文件的视频。支持断点续传。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._directories: set[str] = set()
        self._checked: set[str] = set()   # 已检查路径（断点续传）
        self._worker: FileScannerWorker | None = None
        self.setAcceptDrops(True)
        self._setup_ui()
        self._load_state()

    @staticmethod
    def _state_file() -> str:
        """断点续传状态文件路径。

        不写成类常量：``from config import DATA_DIR`` 是导入期绑定，
        运行时重定向数据目录后（测试/便携模式）类常量会指向旧路径。
        """
        from config import DATA_DIR
        return str(DATA_DIR / "subtitle_state.json")

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("字幕缺失检测")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "拖拽视频文件夹到下方列表，点击「开始扫描」查找缺少字幕的视频。\n"
            "支持 .srt / .vtt / .ass 字幕格式，可设置最小文件大小过滤。\n"
            "扫描状态自动保存，下次扫描自动跳过已检查文件。"
        )
        layout.addWidget(hint)

        set_row = QHBoxLayout()
        set_row.addWidget(QLabel("目录列表 (支持拖入):"))
        set_row.addStretch()
        set_row.addWidget(QLabel("最小文件 (MB):"))
        self.spin_size = QSpinBox()
        self.spin_size.setRange(0, 10240)
        self.spin_size.setValue(self.cfg.min_video_size_mb)
        self.spin_size.setFixedWidth(80)
        set_row.addWidget(self.spin_size)
        layout.addLayout(set_row)

        self.dir_list = DragDropListWidget()
        self.dir_list.setMaximumHeight(120)
        self.dir_list.files_dropped.connect(self._on_dirs_dropped)
        layout.addWidget(self.dir_list)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("添加文件夹")
        self.btn_add.clicked.connect(self._add_folder)
        self.btn_start = QPushButton("开始扫描")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start_scan)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop_scan)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.clicked.connect(self._clear_all)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_pause)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_clear)
        layout.addLayout(btn_row)

        self.status_label = QLabel("就绪")
        self.status_label.setProperty("cssClass", "muted-label")
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        res_label = QLabel("扫描结果（可多选拖出到资源管理器）:")
        layout.addWidget(res_label)
        self.result_list = DragDropListWidget()
        self.result_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.result_list)

    # ── 拖拽支持（页面级 + 列表级） ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        try:
            paths = [u.toLocalFile() for u in event.mimeData().urls()]
            if not paths:
                event.ignore()
                return
            event.acceptProposedAction()
            # 延迟到拖拽结束后再处理，避免在 drop 回调内操作 view 触发 Qt 崩溃
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, lambda: self._on_dirs_dropped(paths))
        except Exception:
            event.ignore()

    def _on_dirs_dropped(self, paths: list[str]) -> None:
        for raw in paths:
            p = os.path.normpath(raw)  # 统一路径形式，保证 set/去重一致
            if len(p) > 240:
                continue  # 跳过超长路径（避免 Windows MAX_PATH 触发异常/崩溃）
            if os.path.isdir(p):
                self._directories.add(p)
                self.dir_list.add_item(os.path.basename(p) or p, p)
            elif os.path.isfile(p):
                d = os.path.dirname(p)
                self._directories.add(d)
                self.dir_list.add_item(os.path.basename(d) or d, d)

    def _add_folder(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "选择视频文件夹")
        if d:
            self._directories.add(d)
            self.dir_list.add_item(os.path.basename(d) or d, d)

    def _clear_all(self) -> None:
        self._directories.clear()
        self.dir_list.clear()
        self.result_list.clear()
        self._checked.clear()
        self.progress.setValue(0)
        self.status_label.setText("就绪")

    # ── 扫描逻辑 ──
    def _start_scan(self) -> None:
        dirs = list(self._directories)
        if not dirs:
            QMessageBox.warning(self, "提示", "请先添加或拖入视频文件夹！")
            return

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("暂停")

        exts = set(self.cfg.video_extensions)
        min_bytes = self.spin_size.value() * 1024 * 1024

        self._worker = FileScannerWorker(
            dirs, exts, min_bytes, checked_files=self._checked,
        )
        self._worker.skipped.connect(self._on_skipped)
        self._worker.progress.connect(self._on_progress)
        self._worker.file_found.connect(self._on_file_found)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_skipped(self, count: int) -> None:
        """预告片/主题视频本来就不需要字幕，跳过是正常的 —— 但要说明白。"""
        self.log_panel.log_info(
            f"已忽略 {count} 个预告片 / 主题视频（它们不需要字幕）")

    def _on_progress(self, filename: str, pct: int) -> None:
        self.status_label.setText(f"正在检查: {filename}")
        self.progress.setValue(pct)

    def _on_file_found(self, filename: str, filepath: str) -> None:
        """找到视频后，检查是否存在同名字幕。"""
        base = os.path.splitext(filepath)[0]
        for sub_ext in self.cfg.subtitle_extensions:
            if os.path.exists(base + sub_ext):
                return  # 有字幕，跳过
        self.result_list.add_item(filename, filepath)

    def _on_finished(self, _files: list, checked: list) -> None:
        self._checked.update(checked)
        self.status_label.setText(f"扫描完成！共发现 {self.result_list.count()} 个缺失字幕的视频")
        self.progress.setValue(100)
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self._save_state()

    def _toggle_pause(self) -> None:
        if self._worker and self._worker.isRunning():
            if self._worker.is_paused:
                self._worker.resume()
                self.btn_pause.setText("暂停")
            else:
                self._worker.pause()
                self.btn_pause.setText("继续")

    def _stop_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.status_label.setText("扫描已停止")
        self._save_state()

    # ── 状态持久化（断点续传） ──
    def _save_state(self) -> None:
        state = {
            "directories": list(self._directories),
            "checked_files": sorted(self._checked),
            "missing": self.result_list.get_all_paths(),
        }
        try:
            from config import DATA_DIR
            path = self._state_file()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _load_state(self) -> None:
        if not os.path.exists(self._state_file()):
            return
        try:
            with open(self._state_file(), "r", encoding="utf-8") as f:
                state = json.load(f)
            for d in state.get("directories", []):
                if os.path.isdir(d):
                    self._directories.add(d)
                    self.dir_list.add_item(os.path.basename(d) or d, d)
            self._checked = set(state.get("checked_files", []))
            for fp in state.get("missing", []):
                if os.path.exists(fp):
                    self.result_list.add_item(os.path.basename(fp), fp)
            if self._directories:
                self.status_label.setText("已恢复上次扫描状态")
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    def shutdown(self) -> None:
        """应用退出时调用：停止后台扫描线程并保存状态。"""
        self._stop_scan()
        self._save_state()
