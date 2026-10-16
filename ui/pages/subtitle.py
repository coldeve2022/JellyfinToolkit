"""字幕缺失检测页面 — 扫描视频目录，找出没有字幕文件的视频。"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page, events, after_stopped

import json
import os

from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QProgressBar,
    QMessageBox,
    QAbstractItemView,
)

from config import ToolkitConfig
from ui.widgets import (
    DragDropListWidget,
    InfoCard,
    FileInputActions,
    accept_url_drag,
    receive_url_drop,
)
from utils import subtitle_clean
from workers.scanner import FileScannerWorker


class SubtitlePage(QWidget):
    """后台扫描视频目录，检测缺少同名或带语言标记字幕的视频。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._directories: set[str] = set()
        self._checked: set[str] = set()  # 已检查路径（断点续传）
        self._aux_skipped = 0  # 本次扫描被忽略的预告片/主题视频数
        self._worker: FileScannerWorker | None = None
        self.setAcceptDrops(True)
        self._setup_ui()
        self._load_state()
        events.media_changed.connect(lambda _: self._refresh_missing())

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
            "目录和结果自动保存；每次扫描重新核对，识别新增或删除的字幕。"
        )
        layout.addWidget(hint)

        set_row = QHBoxLayout()
        set_row.addWidget(QLabel("视频文件 / 文件夹 (支持拖入):"))
        set_row.addStretch()
        set_row.addWidget(QLabel("最小文件 (MB):"))
        self.spin_size = QSpinBox()
        self.spin_size.setRange(0, 10240)
        self.spin_size.setValue(self.cfg.min_video_size_mb)
        self.spin_size.setFixedWidth(80)
        set_row.addWidget(self.spin_size)
        layout.addLayout(set_row)

        self.dir_list = DragDropListWidget()
        self.dir_list.paths_changed.connect(self._sync_directories)
        self.dir_list.setMaximumHeight(120)
        self.dir_list.files_dropped.connect(self._on_dirs_dropped)
        layout.addWidget(FileInputActions(self, self._on_dirs_dropped))
        layout.addWidget(self.dir_list)

        btn_row = QHBoxLayout()
        self.btn_add = self.findChild(FileInputActions).folders_button
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

        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_pause)
        btn_row.addWidget(self.btn_stop)
        self.btn_generate = QPushButton("把缺字幕清单发送到生成 →")
        self.btn_generate.clicked.connect(self._send_to_generator)
        btn_row.addWidget(self.btn_generate)
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
        self.result_list.setAcceptDrops(False)
        self.result_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.result_list)

    # ── 拖拽支持（页面级 + 列表级） ──
    def _sync_directories(self, paths: list) -> None:
        self._directories = set(paths)

    def _refresh_missing(self):
        formats = [str(e).lstrip(".") for e in self.cfg.subtitle_extensions]
        index = subtitle_clean.SubtitleIndex()
        paths = self.result_list.get_all_paths()
        remaining = []
        for path in paths:
            if os.path.exists(path) and not subtitle_clean.find_subtitles(
                path, formats, self.cfg.whisper_output_dir or None, index=index
            ):
                remaining.append(path)
        if remaining != paths:
            self.result_list.clear()
            self._on_missing_batch(remaining)

    def _send_to_generator(self):
        if busy(self):
            return
        self._refresh_missing()
        paths = self.result_list.get_all_paths()
        if paths:
            events.subtitle_ready.emit(paths)

    def dragEnterEvent(self, event):
        accept_url_drag(self, event)

    def dropEvent(self, event):
        receive_url_drop(self, event, self._on_dirs_dropped)

    def _on_dirs_dropped(self, paths: list[str]) -> None:
        if busy(self):
            return
        for raw in paths:
            p = os.path.normpath(raw)  # 统一路径形式，保证 set/去重一致
            if os.path.isdir(p) or os.path.isfile(p):
                self._directories.add(p)
                self.dir_list.add_item(os.path.basename(p) or p, p)

    def _add_folder(self) -> None:
        if busy(self):
            return
        from PySide6.QtWidgets import QFileDialog

        d = QFileDialog.getExistingDirectory(self, "选择视频文件夹")
        if d:
            self._directories.add(d)
            self.dir_list.add_item(os.path.basename(d) or d, d)

    def _clear_all(self) -> None:
        if busy(self):
            return
        self._directories.clear()
        self.dir_list.clear()
        self.result_list.clear()
        self._checked.clear()
        self.progress.setValue(0)
        self.status_label.setText("就绪")

    # ── 扫描逻辑 ──
    def _start_scan(self) -> None:
        if busy(self):
            return
        dirs = self.dir_list.get_all_paths()
        if not dirs:
            QMessageBox.warning(self, "提示", "请先添加或拖入视频文件夹！")
            return

        self._checked.clear()
        self.result_list.clear()
        self.progress.setValue(0)
        self.status_label.setText("正在枚举视频文件…")

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("暂停")

        exts = set(self.cfg.video_extensions)
        min_bytes = self.spin_size.value() * 1024 * 1024

        self._worker = FileScannerWorker(
            dirs,
            exts,
            min_bytes,
            checked_files=self._checked,
            subtitle_formats=self.cfg.subtitle_extensions or [".srt"],
            subtitle_output_dir=self.cfg.whisper_output_dir or None,
        )
        self._worker.skipped.connect(self._on_skipped)
        self._worker.progress.connect(self._on_progress)
        self._worker.missing_batch.connect(self._on_missing_batch)
        self._worker.finished.connect(self._on_finished)
        start_task(self, self._worker)

    def _on_skipped(self, count: int) -> None:
        """预告片/主题视频本来就不需要字幕，跳过是正常的 —— 但要说明白。

        注意：**本页没有 `log_panel`**（那是「批量生成字幕」页的控件），
        这里只能用 `status_label`。曾经误用 log_panel 导致扫描一碰到预告片就抛
        AttributeError。
        """
        self._aux_skipped = count
        self.status_label.setText(f"已忽略 {count} 个预告片 / 主题视频（无需字幕）")

    def _on_progress(self, filename: str, pct: int) -> None:
        self.status_label.setText(f"正在检查: {filename}")
        self.progress.setValue(pct)

    def _on_file_found(self, filename: str, filepath: str) -> None:
        """找到视频后，检查是否已有字幕。

        **统一走 `subtitle_clean.find_subtitles`**，不要在这里自己拼
        ``base + ext`` —— 那样只认 ``xxx.srt``，会把带语言标记的
        ``xxx.chs.srt`` 判成"没有字幕"（整库视频被误报，用户实际撞到过）。
        语言标记的写法太多，这个问题在仓库里被重复犯过三次，所以只留一处实现。
        """
        formats = [str(e).lstrip(".") for e in (self.cfg.subtitle_extensions or [".srt"])]
        if subtitle_clean.find_subtitles(filepath, formats, self.cfg.whisper_output_dir or None):
            return  # 有字幕，跳过
        self.result_list.add_item(filename, filepath)

    def _on_missing_batch(self, paths):
        self.result_list.add_items((os.path.basename(path), path) for path in paths)

    def _on_finished(self, _files: list, checked: list) -> None:
        self._checked.update(checked)
        self.status_label.setText(f"扫描完成！共发现 {self.result_list.count()} 个缺失字幕的视频")
        stopped = self._worker and getattr(self._worker, "_stopped", False)
        if stopped:
            self.status_label.setText("扫描已取消；已发现的结果仍保留。")
        else:
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
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.status_label.setText("扫描已停止")
        self._save_state()
        after_stopped(self, lambda: self.btn_start.setEnabled(True))

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
                if os.path.exists(d):
                    self._directories.add(d)
                    self.dir_list.add_item(os.path.basename(d) or d, d)
            self._checked = set(state.get("checked_files", []))
            self.result_list.add_items(
                (os.path.basename(fp), fp) for fp in state.get("missing", []) if os.path.exists(fp)
            )
            if self._directories:
                self.status_label.setText("已恢复上次扫描状态")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def shutdown(self) -> None:
        stop_page(self)
