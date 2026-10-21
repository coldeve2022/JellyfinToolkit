"""视频有码检测页面 —— 拖入文件/文件夹，产出「有码 / 无码 / 未定」并支持一键复核。

与旧版的区别（重要）
--------------------
旧版只按**文件名关键词**分类，界面上却写着"有码检测"，容易让人误以为它看过画面。
这一版把结论拆成三种状态，并把"凭什么是这个结论"逐条摊开给你看：

- **有码**：画面里检出了马赛克网格（唯一能证明"文件里确实还有码"的证据）；
- **无码**：番号属于天生无码厂牌，或你手动确认过；
- **未定**：现有证据不足以判定 —— 进复核队列，点一下就成了定论。

复核面板直接给候选帧缩略图 + 时间点，点「有码 / 无码」即刻入库，
下次同一部片不再问你（除非文件被替换，size/mtime 变了会自动重算）。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from censor import CensorOptions, CensorService, CensorState, VideoVerdict
from censor.fusion import explain
from config import ToolkitConfig
from ui.services import busy, start_task, stop_page
from ui.widgets import FileInputActions, InfoCard

__all__ = ["CensorshipPage"]

_COLUMNS = ["文件名", "番号", "结论", "置信度", "画面分", "依据"]
class _DropTable(QTableWidget):
    """结果表格 —— 直接往表格上拖文件也能开始检测（与其它页面一致的手感）。"""

    files_dropped = Signal(list)

    def __init__(self, rows: int, cols: int, parent=None):
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)


_STATE_COLOR = {
    CensorState.CENSORED: "#c0392b",
    CensorState.UNCENSORED: "#1e8449",
    CensorState.UNKNOWN: "#7f8c8d",
}


class CensorScanWorker(QThread):
    """后台批量检测 —— 多路并行跑 ffmpeg + numpy，最长的一部决定整批时间。"""

    item_done = Signal(object)      # VideoVerdict
    progressed = Signal(int, int)   # 已完成, 总数
    logged = Signal(str)
    all_done = Signal(int)

    def __init__(self, service: CensorService, paths: list[str],
                 options: CensorOptions, workers: int = 4, parent=None):
        super().__init__(parent)
        self._service = service
        self._paths = list(paths)
        self._options = options
        self._workers = max(1, min(int(workers or 1), 8))
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _should_stop(self) -> bool:
        return self._stopped

    def run(self) -> None:
        total = len(self._paths)
        done = 0
        if not total:
            self.all_done.emit(0)
            return
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(self._service.probe, path, self._options,
                            should_stop=self._should_stop, log=self.logged.emit): path
                for path in self._paths
            }
            for future in as_completed(futures):
                if self._stopped:
                    break
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - 单片失败不该中断整批
                    result = VideoVerdict(path=futures[future], error=str(exc))
                done += 1
                self.item_done.emit(result)
                self.progressed.emit(done, total)
        self.all_done.emit(done)


class CensorshipPage(QWidget):
    """有码检测 + 人工复核。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setAcceptDrops(True)
        self._worker: Optional[CensorScanWorker] = None
        self._service = CensorService(cfg)
        self._results: dict[str, VideoVerdict] = {}
        self._submitted: list[str] = []
        self._row_of: dict[str, int] = {}
        self._current: Optional[VideoVerdict] = None
        self._setup_ui()
        self._refresh_stats()

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("视频有码检测（画面级 + 证据融合）")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)
        layout.addWidget(FileInputActions(self, self._start_scan))

        layout.addWidget(InfoCard(
            "拖入视频或文件夹开始检测。结论分三种：有码 = 画面检出马赛克网格；"
            "无码 = 天生无码厂牌或你已确认；未定 = 证据不足，请在右侧复核。\n"
            "画面检测走「只解关键帧」，一部 3.6 小时的片约 30 秒；多部自动并行。"
            "结果按文件大小+修改时间缓存，片子被替换过会自动重算。"
        ))

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 6, 0)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示:"))
        self.combo_filter = QComboBox()
        self.combo_filter.addItems(["全部", "有码", "无码", "未定", "只看待复核"])
        self.combo_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.combo_filter)
        filter_row.addStretch()
        left_lay.addLayout(filter_row)

        self.table = _DropTable(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.files_dropped.connect(self._start_scan)
        left_lay.addWidget(self.table)
        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 0, 0, 0)
        self.lbl_review_title = QLabel("复核面板：选中左侧任意一行")
        self.lbl_review_title.setWordWrap(True)
        right_lay.addWidget(self.lbl_review_title)

        self.thumb_row = QHBoxLayout()
        self._thumb_labels: list[QLabel] = []
        for _ in range(3):
            lbl = QLabel("")
            lbl.setFixedSize(200, 113)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet("border: 1px solid #ccc; color: #999;")
            self.thumb_row.addWidget(lbl)
            self._thumb_labels.append(lbl)
        right_lay.addLayout(self.thumb_row)

        self.txt_evidence = QPlainTextEdit()
        self.txt_evidence.setReadOnly(True)
        self.txt_evidence.setPlaceholderText("这里会列出这条结论的每条依据")
        right_lay.addWidget(self.txt_evidence)

        btn_row = QHBoxLayout()
        self.btn_censored = QPushButton("✓ 确认为有码")
        self.btn_censored.setProperty("cssClass", "danger")
        self.btn_censored.clicked.connect(lambda: self._label(CensorState.CENSORED))
        self.btn_uncensored = QPushButton("✓ 确认为无码")
        self.btn_uncensored.setProperty("cssClass", "accent")
        self.btn_uncensored.clicked.connect(lambda: self._label(CensorState.UNCENSORED))
        btn_row.addWidget(self.btn_censored)
        btn_row.addWidget(self.btn_uncensored)
        right_lay.addLayout(btn_row)
        splitter.addWidget(right)

        splitter.setSizes([760, 460])
        layout.addWidget(splitter)

        bottom = QHBoxLayout()
        self._stats_label = QLabel("就绪")
        self._stats_label.setProperty("cssClass", "muted-label")
        bottom.addWidget(self._stats_label)
        bottom.addStretch()
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        bottom.addWidget(self.btn_stop)
        clear_btn = QPushButton("清空列表")
        clear_btn.clicked.connect(self._clear)
        bottom.addWidget(clear_btn)
        layout.addLayout(bottom)

    # ── 拖拽 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [os.path.normpath(u.toLocalFile()) for u in event.mimeData().urls()]
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._start_scan(paths)

    # ── 扫描 ──
    def queued_paths(self) -> list[str]:
        """已提交去检测的文件（按提交顺序）。供自动化测试与外部脚本查询。"""
        return list(self._submitted)

    def _collect_files(self, paths: list[str]) -> list[str]:
        """把拖进来的文件/文件夹展开成"确实是视频"的文件列表。"""
        from utils.file_scan import iter_media_files

        return list(iter_media_files(
            paths,
            video_exts={e.lower() for e in self.cfg.video_extensions},
            exclude_keywords=self.cfg.exclude_path_keywords,
        ))

    def _start_scan(self, paths: list[str]) -> None:
        # 运行中再拖进来：不许替换正在跑的线程（历史 bug：新线程顶掉旧线程 → 漏扫）
        if self._worker is not None and self._worker.isRunning():
            self._stats_label.setText("扫描进行中，请等待完成后再添加")
            return
        if busy(self):
            self._stats_label.setText("已有任务在跑，先等它结束")
            return

        files = self._collect_files([str(p) for p in paths])
        files = [f for f in files if f not in self._results]
        if not files:
            self._stats_label.setText("没有新增视频可检测")
            return
        self._set_running(True)
        self._submitted.extend(files)
        self._worker = CensorScanWorker(
            self._service, files,
            CensorOptions(detect_visual=bool(self.cfg.censor_visual_enabled)),
            workers=int(getattr(self.cfg, "censor_workers", 4) or 4),
        )
        self._worker.item_done.connect(self._on_item)
        self._worker.progressed.connect(self._on_progress)
        self._worker.logged.connect(self._on_log)
        self._worker.all_done.connect(self._on_all_done)
        start_task(self, self._worker, "有码检测")

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._stats_label.setText("正在停止…已完成的片会保留结果")

    def _set_running(self, running: bool) -> None:
        self.btn_stop.setEnabled(running)

    def shutdown(self) -> None:
        stop_page(self)

    # ── 回填 ──
    def _on_item(self, result: VideoVerdict) -> None:
        self._results[result.path] = result
        self._insert_row(result)
        self._refresh_stats()

    def _insert_row(self, result: VideoVerdict) -> None:
        self.table.setSortingEnabled(False)
        row = self.table.rowCount()
        self.table.insertRow(row)
        evidence_text = "；".join(t for _, t in explain(result.verdict))
        values = [
            os.path.basename(result.path),
            result.number or "—",
            result.state.label,
            f"{result.verdict.confidence:.2f}" if result.is_decided else "—",
            f"{result.visual_score:.1f}" if result.visual_score else "—",
            evidence_text[:120],
        ]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if col == 2:
                item.setForeground(QColor(_STATE_COLOR.get(result.state, "#333")))
            if col == 0:
                item.setData(Qt.UserRole, result.path)
                item.setToolTip(result.path)
            self.table.setItem(row, col, item)
        self._row_of[result.path] = row
        self.table.setSortingEnabled(True)
        self._apply_filter()

    def _on_progress(self, done: int, total: int) -> None:
        self._stats_label.setText(f"检测中 {done}/{total}")

    def _on_log(self, message: str) -> None:
        self._stats_label.setText(message[:120])

    def _on_all_done(self, count: int) -> None:
        self._set_running(False)
        self._refresh_stats()

    # ── 筛选 ──
    def _apply_filter(self) -> None:
        mode = self.combo_filter.currentText()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            path = item.data(Qt.UserRole) if item else None
            result = self._results.get(path)
            visible = True
            if result is not None:
                if mode == "有码":
                    visible = result.state is CensorState.CENSORED
                elif mode == "无码":
                    visible = result.state is CensorState.UNCENSORED
                elif mode == "未定":
                    visible = result.state is CensorState.UNKNOWN
                elif mode == "只看待复核":
                    visible = result.verdict.needs_review
            self.table.setRowHidden(row, not visible)

    # ── 复核 ──
    def _on_row_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return
        item = self.table.item(rows[0].row(), 0)
        path = item.data(Qt.UserRole) if item else None
        result = self._results.get(path)
        if result is None:
            return
        self._current = result
        self.lbl_review_title.setText(
            f"{os.path.basename(result.path)}\n"
            f"结论：{result.state.label} · 画面分 {result.visual_score:.1f} · "
            f"耗时 {result.elapsed:.0f}s")
        self._show_thumbs(result)
        lines = [f"{src}: {text}" for src, text in explain(result.verdict)]
        if result.error:
            lines.append(f"错误: {result.error}")
        self.txt_evidence.setPlainText("\n".join(lines) or "（没有可用依据）")

    def _show_thumbs(self, result: VideoVerdict) -> None:
        for index, label in enumerate(self._thumb_labels):
            candidate = result.candidates[index] if index < len(result.candidates) else None
            if candidate is None or not candidate.thumb_path or not os.path.exists(candidate.thumb_path):
                label.setPixmap(QPixmap())
                label.setText("无候选帧" if index == 0 else "")
                continue
            pix = QPixmap(candidate.thumb_path)
            if pix.isNull():
                label.setText("缩略图丢失")
                continue
            label.setPixmap(pix.scaled(label.size(), Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))
            label.setToolTip(f"{candidate.hms} · 分数 {candidate.score:.1f} · "
                             f"块边长 {candidate.block_size}px")

    def _label(self, state: CensorState) -> None:
        if self._current is None:
            return
        path = self._current.path
        refreshed = self._service.label(path, state, "界面人工确认", base=self._current)
        if refreshed is not None:
            self._results[path] = refreshed
            self._replace_row(refreshed)
        self._current = self._results.get(path)
        self._refresh_stats()

    def _replace_row(self, result: VideoVerdict) -> None:
        row = self._row_of.get(result.path)
        if row is None or row >= self.table.rowCount():
            return
        self.table.setSortingEnabled(False)
        self.table.item(row, 2).setText(result.state.label)
        self.table.item(row, 3).setText(
            f"{result.verdict.confidence:.2f}" if result.is_decided else "—")
        self.table.item(row, 5).setText(
            "；".join(t for _, t in explain(result.verdict))[:120])
        self.table.setSortingEnabled(True)
        self._apply_filter()

    # ── 杂项 ──
    def _clear(self) -> None:
        if busy(self):
            return
        self.table.setRowCount(0)
        self._results.clear()
        self._submitted.clear()
        self._row_of.clear()
        self._current = None
        for label in self._thumb_labels:
            label.setPixmap(QPixmap())
            label.setText("")
        self.txt_evidence.clear()
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        stats = self._service.stats()
        self._stats_label.setText(stats.as_text())
