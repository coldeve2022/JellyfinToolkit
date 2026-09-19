"""自动化工作台页面。"""
from __future__ import annotations

import json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QProgressBar, QTextEdit, QMessageBox,
)

from config import ToolkitConfig
from ui.widgets import LogPanel
from workers.automation import AutomationWorker


class AutomationPage(QWidget):
    """把全软件功能串成一条自动化流水线，默认只读，勾选后才执行写操作。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: AutomationWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(12)

        title = QLabel("自动化工作台")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "默认执行全库只读审计：采集、评分、字幕/去重报告、分集合并计划。\n"
            "勾选下方写操作后，会按顺序执行对应自动化动作。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        self.cb_scan_video = QCheckBox("逐文件检查损坏/不兼容（较慢）")
        self.cb_check_old = QCheckBox("检查旧合并是否损坏")
        self.cb_merge = QCheckBox("合并 ready 分集")
        self.cb_dedup = QCheckBox("重复版本建议删除者 → 回收站")
        self.cb_delete = QCheckBox("低分未看作品 → 回收站")
        self.cb_jellyfin = QCheckBox("清洗 Jellyfin 脏标签")
        for cb in (self.cb_scan_video, self.cb_check_old, self.cb_merge,
                   self.cb_dedup, self.cb_delete, self.cb_jellyfin):
            root.addWidget(cb)

        buttons = QHBoxLayout()
        self.btn_audit = QPushButton("只读审计")
        self.btn_audit.setProperty("cssClass", "accent")
        self.btn_audit.clicked.connect(lambda: self._start(False))
        buttons.addWidget(self.btn_audit)
        self.btn_run = QPushButton("执行自动化")
        self.btn_run.setProperty("cssClass", "danger")
        self.btn_run.clicked.connect(lambda: self._start(True))
        buttons.addWidget(self.btn_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch()
        root.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        root.addWidget(self.progress)

        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(220)
        root.addWidget(self.log_panel)

        report_label = QLabel("执行报告:")
        root.addWidget(report_label)
        self.report_box = QTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setPlaceholderText("完成后这里显示 JSON 报告")
        root.addWidget(self.report_box, 1)

    def _options(self, apply_all: bool) -> dict:
        return {
            "scan_video": apply_all and self.cb_scan_video.isChecked(),
            "check_old": apply_all and self.cb_check_old.isChecked(),
            "apply_merge": apply_all and self.cb_merge.isChecked(),
            "apply_dedup": apply_all and self.cb_dedup.isChecked(),
            "apply_delete": apply_all and self.cb_delete.isChecked(),
            "apply_jellyfin_clean": apply_all and self.cb_jellyfin.isChecked(),
            "refresh_library": True,
        }

    def _start(self, apply_all: bool) -> None:
        if self._worker and self._worker.isRunning():
            return
        if apply_all and (
            self.cb_dedup.isChecked() or self.cb_delete.isChecked()
        ):
            QMessageBox.warning(
                self, "二次确认",
                "回收站操作虽可恢复，但会移动文件。请确认勾选项后再执行。",
            )
        self.btn_audit.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.log_panel.clear()
        self.report_box.clear()

        self._worker = AutomationWorker(self.cfg, self._options(apply_all))
        self._worker.log.connect(self.log_panel.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.report_ready.connect(self._on_report)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning("已请求停止，等待当前阶段结束。")

    def _on_report(self, report: dict) -> None:
        self.report_box.setPlainText(
            json.dumps(report, ensure_ascii=False, indent=2))

    def _on_error(self, message: str) -> None:
        self.log_panel.log_error(f"自动化失败：{message}")

    def _on_finished(self) -> None:
        self.btn_audit.setEnabled(True)
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self.log_panel.log_success("自动化流水线执行完毕。")

    def shutdown(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
