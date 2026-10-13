"""自动化工作台页面。"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page, events


from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QProgressBar,
    QTextEdit,
    QMessageBox,
    QFileDialog,
)

from config import ToolkitConfig
from ui.widgets import LogPanel
from workers.automation import AutomationWorker
from ui.services import accept_snapshot, state_context


class AutomationPage(QWidget):
    """把全软件功能串成一条自动化流水线，默认只读，勾选后才执行写操作。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: AutomationWorker | None = None
        self._failed = False
        self._preview_plan = None
        self._setup_ui()
        events.config_changed.connect(self._invalidate_preview)

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
        self.cb_dedup = QCheckBox("重复版本建议删除者 → 可恢复隔离区")
        self.cb_delete = QCheckBox("低分未看作品 → 可恢复隔离区")
        self.cb_jellyfin = QCheckBox("清洗 Jellyfin 脏标签")
        for cb in (
            self.cb_scan_video,
            self.cb_check_old,
            self.cb_merge,
            self.cb_dedup,
            self.cb_delete,
            self.cb_jellyfin,
        ):
            root.addWidget(cb)
            cb.toggled.connect(self._invalidate_preview)

        buttons = QHBoxLayout()
        self.btn_audit = QPushButton("只读审计")
        self.btn_audit.setProperty("cssClass", "accent")
        self.btn_audit.clicked.connect(lambda: self._start(False))
        buttons.addWidget(self.btn_audit)
        self.btn_run = QPushButton("执行自动化")
        self.btn_run.setProperty("cssClass", "danger")
        self.btn_run.clicked.connect(lambda: self._start(True))
        self.btn_run.setEnabled(False)
        buttons.addWidget(self.btn_run)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        buttons.addWidget(self.btn_stop)
        buttons.addStretch()
        root.addLayout(buttons)
        templates = QHBoxLayout()
        save = QPushButton("保存工作流模板…")
        save.clicked.connect(lambda: self._save_workflow())
        templates.addWidget(save)
        load = QPushButton("载入工作流模板…")
        load.clicked.connect(lambda: self._load_workflow())
        templates.addWidget(load)
        templates.addStretch()
        root.addLayout(templates)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self._failed = False
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
            "scan_video": self.cb_scan_video.isChecked(),
            "check_old": self.cb_check_old.isChecked(),
            "apply_merge": apply_all and self.cb_merge.isChecked(),
            "apply_dedup": apply_all and self.cb_dedup.isChecked(),
            "apply_delete": apply_all and self.cb_delete.isChecked(),
            "apply_jellyfin_clean": apply_all and self.cb_jellyfin.isChecked(),
            "refresh_library": True,
            "selected_actions": [
                name
                for name, checkbox in (
                    ("merge", self.cb_merge),
                    ("dedup", self.cb_dedup),
                    ("delete", self.cb_delete),
                    ("jellyfin_clean", self.cb_jellyfin),
                )
                if checkbox.isChecked()
            ],
            "approved_plan_id": self._preview_plan["id"]
            if apply_all and self._preview_plan
            else "",
        }

    def _invalidate_preview(self, *args):
        self._preview_plan = None
        if hasattr(self, "btn_run"):
            self.btn_run.setEnabled(False)

    def _start(self, apply_all: bool) -> None:
        if busy(self, mutating=apply_all):
            return
        if self._worker and self._worker.isRunning():
            return
        if apply_all:
            if not self._preview_plan:
                QMessageBox.information(
                    self,
                    "先生成计划",
                    "先执行只读审计并审阅具体目标；更改配置/选项后需要重新生成计划。",
                )
                return
            actions = self._preview_plan.get("actions", [])
            if not actions:
                QMessageBox.information(self, "没有操作", "本次计划没有可执行的写入动作。")
                return
            detail = "\n".join(
                str(action.get("kind"))
                + " | "
                + str(action.get("path") or action.get("target"))
                + " | "
                + str(action.get("reason", ""))
                for action in actions
            )
            answer = QMessageBox.question(
                self,
                "二次确认",
                "确认执行刚才审阅的同一计划：\n" + detail,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.btn_audit.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self._failed = False
        self.log_panel.clear()
        self.report_box.clear()

        self._worker = AutomationWorker(self.cfg, self._options(apply_all))
        self._worker.log.connect(self.log_panel.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.report_ready.connect(self._on_report)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        start_task(
            self,
            self._worker,
            "执行已审核自动化计划" if apply_all else "全库只读审计",
            mutating=apply_all,
        )

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning("已请求停止，等待当前阶段结束。")

    def _on_report(self, report: dict) -> None:
        previous_context = state_context(self.cfg)
        score = report.get("stages", {}).get("score")
        if isinstance(score, dict):
            if not accept_snapshot(self, score):
                report = dict(report, status="failed", error=score.get("error", "库结果已失效"))
        changed_context = previous_context != state_context(self.cfg)
        self._failed = report.get("status") == "failed" or bool(report.get("error"))
        self._preview_plan = (
            report.get("plan")
            if report.get("status") == "planned" and not changed_context
            else None
        )
        rows = [
            "状态：" + str(report.get("status", "完成")),
            "摘要：" + str(report.get("summary", "")),
        ]
        for action in (report.get("plan") or {}).get("actions", []):
            rows.append(
                str(action.get("kind"))
                + " | "
                + str(action.get("path") or action.get("target"))
                + " | "
                + str(action.get("reason", ""))
            )
        if report.get("error"):
            rows.append("错误：" + str(report["error"]))
        if changed_context:
            rows.append("已确认实际媒体库/观看用户。旧配置创建的计划已失效，请重新执行只读审计。")
        self.report_box.setPlainText("\n".join(rows))

    def _on_error(self, message: str) -> None:
        self._failed = True
        self.log_panel.log_error(f"自动化失败：{message}")

    def _on_finished(self) -> None:
        self.btn_audit.setEnabled(True)
        self.btn_run.setEnabled(bool(self._preview_plan))
        self.btn_stop.setEnabled(False)
        if self._failed:
            self.progress.setValue(0)
            self.log_panel.log_error("自动化失败，请查看执行报告和失败步骤。")
        elif self._worker and getattr(self._worker, "_stopped", False):
            self.log_panel.log_warning("自动化已取消。")
        else:
            self.progress.setValue(100)
            self.log_panel.log_success("自动化已完成。")

    def shutdown(self) -> None:
        stop_page(self)

    def _save_workflow(self, path=None):
        if busy(self):
            return
        import json
        from pathlib import Path

        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "保存工作流模板", "", "JSON (*.json)")
        if not path:
            return
        flags = {
            name: getattr(self, "cb_" + name).isChecked()
            for name in ("scan_video", "check_old", "merge", "dedup", "delete", "jellyfin")
        }
        try:
            Path(path).write_text(
                json.dumps({"version": 1, "steps": flags}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def _load_workflow(self, path=None):
        if busy(self):
            return
        import json
        from pathlib import Path

        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "载入工作流模板", "", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            flags = data["steps"]
            names = ("scan_video", "check_old", "merge", "dedup", "delete", "jellyfin")
            if (
                data.get("version") != 1
                or not isinstance(flags, dict)
                or any(type(flags.get(name)) is not bool for name in names)
            ):
                raise ValueError("不支持的工作流模板。")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "载入失败", str(exc))
            return
        self._invalidate_preview()
        for name in names:
            getattr(self, "cb_" + name).setChecked(flags[name])
        self.log_panel.log("已载入步骤模板；请先只读审计，审阅本次实际目标。")
