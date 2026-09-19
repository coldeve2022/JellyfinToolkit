"""视频修复转码页面 — 合并了坏帧修复 + Jellyfin 兼容性转码两个功能。"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QCheckBox, QMessageBox, QComboBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)
from PySide6.QtGui import QColor, QBrush

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, LogPanel, InfoCard
from utils.library import is_junk_attachment_path
from workers.ffmpeg import FFprobeAnalyzer, FFmpegRepairWorker


class RepairPage(QWidget):
    """视频修复 + 转码 — 分析并修复损坏视频，转码不兼容格式。

    交互约定：
    - 文件列表是唯一数据源（拖入文件/文件夹）
    - 扫描检测（体检报告）与开始修复互斥；**扫描结果会被保存复用**，
      修复时不再重新分析（解决"扫一遍又扫一遍"的重复低效）
    - 扫描完成自动汇总：损坏/不兼容/正常 各多少、具体原因，展示在结果表格
    """

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setAcceptDrops(True)
        self._worker: FFmpegRepairWorker | None = None
        self._analyzer: FFprobeAnalyzer | None = None
        self._scanning = False
        self._scan_files_list: list[str] = []   # 当前扫描批次的文件（已过滤附属）
        self._scan_results: dict[str, dict] = {}   # 扫描结果 {path: analyze_video 结果}
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("视频修复转码")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "拖拽视频文件或文件夹到下方列表。支持：\n"
            "• 扫描检测 — 分析视频兼容性和损坏状态（体检报告，结果会保存）\n"
            "• 修复坏帧 — GPU 加速跳过解码错误，重新编码\n"
            "• 不兼容转码 — 将非标准格式转为 Jellyfin 友好的 MP4\n"
            "勾选两项时自动决策：损坏→修复，不兼容→转码，正常→跳过。\n"
            "注意：扫描完成后点「开始修复」直接复用扫描结果，不会重复分析。"
        )
        layout.addWidget(hint)

        layout.addWidget(QLabel("待处理列表 (支持拖入文件/文件夹):"))
        self.file_list = DragDropListWidget()
        self.file_list.setMaximumHeight(150)
        self.file_list.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.file_list)

        opt_row = QHBoxLayout()
        self.cb_repair = QCheckBox("修复坏帧 (GPU加速)")
        self.cb_repair.setChecked(True)
        opt_row.addWidget(self.cb_repair)

        self.cb_transcode = QCheckBox("不兼容转码")
        self.cb_transcode.setChecked(True)
        opt_row.addWidget(self.cb_transcode)

        self.cb_skip_small = QCheckBox("跳过 <20MB")
        self.cb_skip_small.setChecked(True)
        opt_row.addWidget(self.cb_skip_small)

        opt_row.addStretch()
        layout.addLayout(opt_row)

        gpu_row = QHBoxLayout()
        gpu_row.addWidget(QLabel("GPU 编码器:"))
        self.combo_codec = QComboBox()
        # data 为 "" = 自动；执行时按本机**实测**结果挑选（不看编译期列表）
        self.combo_codec.addItem("自动（推荐）", "")
        for name in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv",
                     "h264_amf", "hevc_amf", "libx264", "libx265"):
            self.combo_codec.addItem(name, name)
        idx = self.combo_codec.findData(self.cfg.ffmpeg_gpu_codec or "")
        self.combo_codec.setCurrentIndex(idx if idx >= 0 else 0)
        gpu_row.addWidget(self.combo_codec)

        gpu_row.addWidget(QLabel("硬解加速:"))
        self.combo_hw = QComboBox()
        self.combo_hw.addItem("自动（推荐）", "")
        for name in ("cuda", "d3d11va", "dxva2", "qsv", "amf", "vaapi", "none"):
            self.combo_hw.addItem(name, name)
        idx = self.combo_hw.findData(self.cfg.ffmpeg_hardware_accel or "")
        self.combo_hw.setCurrentIndex(idx if idx >= 0 else 0)
        gpu_row.addWidget(self.combo_hw)

        gpu_row.addWidget(QLabel("质量 CRF:"))
        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(15, 35)
        self.spin_crf.setValue(self.cfg.transcode_crf)
        self.spin_crf.setFixedWidth(60)
        gpu_row.addWidget(self.spin_crf)

        gpu_row.addStretch()
        layout.addLayout(gpu_row)

        self.status_label = QLabel("就绪")
        self.status_label.setProperty("cssClass", "muted-label")
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # ── 扫描结果汇总卡片 ──
        self.summary_label = QLabel("尚未扫描")
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("cssClass", "panel-card")
        layout.addWidget(self.summary_label)

        # ── 扫描结果表格（直观查看每个文件状态/原因）──
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["文件", "状态", "编码", "原因 / 说明"])
        hh = self.result_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.result_table.setColumnWidth(0, 260)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setMaximumHeight(200)
        layout.addWidget(self.result_table)

        layout.addWidget(QLabel("处理日志:"))
        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel)

        btn_row = QHBoxLayout()
        self.btn_scan = QPushButton("扫描检测")
        self.btn_scan.clicked.connect(self._scan_files)
        btn_row.addWidget(self.btn_scan)

        self.btn_start = QPushButton("开始修复")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self._start_repair)
        btn_row.addWidget(self.btn_start)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_row.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_stop)

        btn_row.addStretch()
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

    # ── 拖拽 ──
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
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self, lambda: self._on_files_dropped(paths))
        except Exception:
            event.ignore()

    def _on_files_dropped(self, paths: list[str]) -> None:
        exts = {e.lower() for e in self.cfg.video_extensions}
        skipped_junk = 0
        for p in paths:
            pp = Path(p)
            if len(p) > 240:
                continue  # 跳过超长路径（避免 Windows MAX_PATH 触发异常/崩溃）
            if pp.is_file():
                if pp.suffix.lower() not in exts:
                    continue
                # 排除 Jellyfin 生成的附属文件(theme/backdrop/fanart/trailer 等)，
                # 它们用于缩略图/预告片，与视频本体无关，不应当"正片"修复/转码
                if is_junk_attachment_path(str(pp)):
                    skipped_junk += 1
                    continue
                self.file_list.add_item(pp.name, str(pp))
            elif pp.is_dir():
                try:
                    for f in pp.rglob("*"):
                        if f.is_file() and f.suffix.lower() in exts:
                            if is_junk_attachment_path(str(f)):
                                skipped_junk += 1
                                continue
                            self.file_list.add_item(f.name, str(f))
                except OSError:
                    continue
        if skipped_junk:
            self.log_panel.log_info(
                f"已自动排除 {skipped_junk} 个 Jellyfin 附属文件(theme/backdrop/fanart/trailer，非正片)。")
        # 拖入文件即可直接修复，无需先扫描
        self.btn_start.setEnabled(True)

    # ── 扫描检测 ──
    def _filtered_files(self) -> list[str]:
        """过滤掉 Jellyfin 附属文件(theme/backdrop/fanart/trailer，非正片)。

        拖入时已过滤，但旧列表可能残留（历史版本），这里兜底排除，
        避免 theme_video.mp4 等被当成"损坏视频"分析/修复。
        """
        files = self.file_list.get_all_paths()
        keep = [f for f in files if not is_junk_attachment_path(f)]
        skipped = len(files) - len(keep)
        if skipped:
            self.log_panel.log_info(f"已排除 {skipped} 个 Jellyfin 附属文件(theme/backdrop/fanart/trailer，非正片)。")
        return keep

    def _select_repair_queue(self, mode: str) -> list[str]:
        """扫描结果已知时，只挑出**需要处理**的文件，正常文件完全不进队列。

        这是"复用扫描结果"的终极形态：不再是遍历全部文件再静默跳过，
        而是扫描完就知道谁要修，直接把问题文件送进修复队列，
        正常的（兼容且正常）一个都不再触碰——既省时间又干净。

        Args:
            mode: auto/repair/transcode（决定挑哪些文件）。

        Returns:
            要处理的文件列表（损坏→修复、不兼容→转码；正常的不进）。
            无扫描结果时返回空列表，由调用方回退到全量修复。
        """
        if not self._scan_results:
            return []
        queue = []
        for path, info in self._scan_results.items():
            if mode == "transcode":
                # 只转码"不兼容"的
                if not info.get("is_corrupted") and not info.get("is_compatible"):
                    queue.append(path)
            elif mode == "repair":
                # 只修"损坏"的
                if info.get("is_corrupted"):
                    queue.append(path)
            else:  # auto
                # 损坏→修复，不兼容→转码；正常的不进队列
                if info.get("is_corrupted") or not info.get("is_compatible"):
                    queue.append(path)
        return queue

    def _scan_files(self) -> None:
        files = self._filtered_files()
        if not files:
            QMessageBox.warning(self, "提示", "请先添加视频文件！")
            return
        if self._scanning:
            return

        self._scanning = True
        self._scan_files_list = files   # 记住本批要扫描的文件（已过滤附属）
        self._scan_results = {}         # 清空旧扫描结果
        self.result_table.setRowCount(0)
        self.summary_label.setText("扫描中…")
        self.btn_scan.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.log_panel.clear()
        self.log_panel.log_info(f"开始分析 {len(files)} 个文件...")
        self._analyze_next(0)

    def _analyze_next(self, idx: int) -> None:
        # 扫描被停止后，丢弃仍排队的链式调用
        if not self._scanning:
            return
        files = getattr(self, "_scan_files_list", None) or self.file_list.get_all_paths()
        if idx >= len(files):
            self._on_scan_done()   # 汇总 + 更新 UI
            return

        fp = files[idx]
        self.status_label.setText(f"分析: {os.path.basename(fp)}")

        self._analyzer = FFprobeAnalyzer(fp)
        self._analyzer.result.connect(self._on_analyzed)
        self._analyzer.finished.connect(lambda: self._analyze_next(idx + 1))
        self._analyzer.start()

    def _on_analyzed(self, filepath: str, info: dict) -> None:
        self._scan_results[filepath] = info   # 保存扫描结果供修复复用（不重复分析）
        name = os.path.basename(filepath)
        if info["is_corrupted"]:
            status = "❌ 损坏"
            reason = info.get('error') or '结构异常'
            self.log_panel.log_error(f"❌ 损坏: {name} — {reason}")
        elif not info["is_compatible"]:
            codec = info.get("video_codec") or "未知"
            status = "⚠️ 不兼容"
            reason = f"编码 {codec}（将自动转码）"
            self.log_panel.log_warning(f"⚠️ 不兼容: {name} (编码: {codec}) — 将自动转码")
        else:
            status = "✅ 正常"
            reason = "兼容且可播放"
            self.log_panel.log_success(f"✅ 正常: {name}")
        self._append_result_row(filepath, status, info.get("video_codec") or "", reason)

    def _append_result_row(self, filepath: str, status: str, codec: str, reason: str) -> None:
        """往结果表格追加一行，状态列着色。"""
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        name_cell = QTableWidgetItem(os.path.basename(filepath))
        name_cell.setToolTip(filepath)
        status_cell = QTableWidgetItem(status)
        codec_cell = QTableWidgetItem(codec)
        reason_cell = QTableWidgetItem(reason)
        reason_cell.setToolTip(reason)
        # 状态着色（随主题）
        try:
            from ui import theme
            if status.startswith("❌"):
                color = theme.color("danger")
            elif status.startswith("⚠"):
                color = theme.color("warning")
            else:
                color = theme.color("success")
            status_cell.setForeground(QBrush(QColor(color)))
        except Exception:  # noqa: BLE001
            pass
        self.result_table.setItem(row, 0, name_cell)
        self.result_table.setItem(row, 1, status_cell)
        self.result_table.setItem(row, 2, codec_cell)
        self.result_table.setItem(row, 3, reason_cell)

    def _theme_c(self, role: str) -> str:
        """取主题色 hex 值，供汇总卡片 HTML 使用；失败时返回主题无关的兜底色。"""
        try:
            from ui import theme
            return theme.color(role)
        except Exception:  # noqa: BLE001
            fallback = {"danger": "#e06c75", "warning": "#e5c07b",
                        "success": "#98c379", "primary": "#61afef"}.get(role)
            return fallback or "#9aa0a6"

    def _on_scan_done(self) -> None:
        """扫描全部完成——汇总结果：损坏/不兼容/正常 各多少、原因，并更新 UI。

        同时把本批 `_scan_results` 缓存下来，供「开始修复」直接用 precomputed
        复用，避免修复时又重复 ffprobe 分析一遍。
        """
        if not self._scanning:
            return
        self._scanning = False
        self.btn_scan.setEnabled(True)
        self.btn_start.setEnabled(bool(self.file_list.get_all_paths()))

        total = len(self._scan_files_list)
        results = self._scan_results
        n_ok = sum(1 for k, v in results.items() if not v.get("is_corrupted")
                   and v.get("is_compatible"))
        n_bad = sum(1 for v in results.values() if v.get("is_corrupted"))
        n_comp = sum(1 for v in results.values()
                     if not v.get("is_corrupted") and not v.get("is_compatible"))

        if not results:
            self.summary_label.setText("未扫描到任何有效文件。")
            self.status_label.setText("扫描完成，无有效文件")
            return

        # 汇总卡片（HTML，随主题着色）
        c_danger = self._theme_c("danger")
        c_warn = self._theme_c("warning")
        c_ok = self._theme_c("success")
        c_primary = self._theme_c("primary")
        summary_html = (
            f"<span style='color:{c_primary};font-weight:600;'>扫描完成</span>"
            f"　共 {total} 个文件　"
            f"<span style='color:{c_danger};'>❌ 损坏 {n_bad}</span>　"
            f"<span style='color:{c_warn};'>⚠️ 不兼容 {n_comp}</span>　"
            f"<span style='color:{c_ok};'>✅ 正常 {n_ok}</span>"
        )
        self.summary_label.setText(summary_html)
        self.status_label.setText(
            f"扫描完成: 共 {total} 个 | 损坏 {n_bad} | 不兼容 {n_comp} | 正常 {n_ok}")

        self.log_panel.log_info(
            "─" * 50)
        self.log_panel.log_info(
            f"📋 扫描汇总: 共 {total} 个文件 → "
            f"❌ 损坏 {n_bad} 个 | ⚠️ 不兼容 {n_comp} 个 | ✅ 正常 {n_ok} 个")
        # 具体问题文件列表（便于排查）
        for name, v in results.items():
            if v.get("is_corrupted"):
                self.log_panel.log_error(
                    f"  · {os.path.basename(name)} — {v.get('error') or '结构异常'}")
            elif not v.get("is_compatible"):
                self.log_panel.log_warning(
                    f"  · {os.path.basename(name)} — 编码 "
                    f"{v.get('video_codec') or '未知'}，将自动转码")
        self.log_panel.log_info(
            "─" * 50)
        self.log_panel.log_info(
            "✅ 扫描结果已保存，点「开始修复」将直接复用，不再重复分析。")

    # ── 修复/转码 ──
    def _start_repair(self) -> None:
        files = self._filtered_files()
        if not files:
            QMessageBox.warning(self, "提示", "请先添加视频文件！")
            return
        if self._scanning:
            QMessageBox.warning(self, "提示", "扫描进行中，请稍候或先停止扫描！")
            return
        if not self.cb_repair.isChecked() and not self.cb_transcode.isChecked():
            QMessageBox.warning(self, "提示", "请至少勾选「修复坏帧」或「不兼容转码」之一再开始。")
            return

        self.btn_start.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)

        self.cfg.ffmpeg_gpu_codec = self.combo_codec.currentData() or ""
        self.cfg.ffmpeg_hardware_accel = self.combo_hw.currentData() or ""
        self.cfg.transcode_crf = self.spin_crf.value()

        if self.cb_repair.isChecked() and self.cb_transcode.isChecked():
            mode = "auto"
        elif self.cb_transcode.isChecked():
            mode = "transcode"
        else:
            mode = "repair"

        min_bytes = 20 * 1024 * 1024 if self.cb_skip_small.isChecked() else 0

        # 已扫描过 → 只挑出**需要处理**的文件（损坏→修复，不兼容→转码），
        # 正常文件完全不进队列、不遍历、不刷日志，直接奔着问题文件去。
        self._repair_queue = files
        if self._scan_results:
            queue = self._select_repair_queue(mode)
            if queue:
                self._repair_queue = queue
                files = queue
                self.log_panel.log_info(
                    f"🎯 已从扫描结果中筛出 {len(queue)} 个待处理文件 "
                    f"(损坏/不兼容)，其余正常文件不再遍历。")
            else:
                QMessageBox.information(
                    self, "无需处理",
                    "扫描结果显示所有文件均兼容且正常，没有需要修复/转码的。")
                self._on_finished()
                return

        self._worker = FFmpegRepairWorker(
            files, mode,
            self.cfg.ffmpeg_gpu_codec,
            self.cfg.ffmpeg_hardware_accel,
            self.cfg.transcode_crf,
            min_size_bytes=min_bytes,
            precomputed=self._scan_results,   # 复用扫描结果，不重复 ffprobe 分析
            timeout=self.cfg.ffmpeg_timeout,  # 单文件处理超时，防损坏文件无限挂起
            log_skips=False,                  # 只处理问题文件，无需再刷"跳过"日志
        )
        self._worker.log.connect(self.log_panel.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _toggle_pause(self) -> None:
        if self._worker and self._worker.isRunning():
            if self._worker.is_paused:
                self._worker.resume()
                self.btn_pause.setText("暂停")
            else:
                self._worker.pause()
                self.btn_pause.setText("继续")

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning("任务已停止，已处理的文件保持不变")
        elif self._scanning and self._analyzer and self._analyzer.isRunning():
            self._analyzer.stop()
            self._scanning = False
            self.log_panel.log_warning("扫描已停止")
        self._on_finished()

    def _on_finished(self) -> None:
        self._scanning = False
        self.btn_start.setEnabled(bool(self.file_list.get_all_paths()))
        self.btn_scan.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("暂停")
        if not self._worker and not self.progress.value():
            self.status_label.setText("就绪")

    def _clear(self) -> None:
        self.file_list.clear()
        self.log_panel.clear()
        self.progress.setValue(0)
        self.status_label.setText("就绪")
        self.btn_start.setEnabled(False)
        self._scan_results = {}
        self._scan_files_list = []
        self.result_table.setRowCount(0)
        self.summary_label.setText("尚未扫描")

    def shutdown(self) -> None:
        """应用退出时调用：停止所有后台线程。"""
        self._stop()
