"""NFO 关联修复页面 — 修复 .ts → .mp4 后的 NFO 文件和关联资源。"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page

import os
from pathlib import Path
import copy
from ui.services import CallWorker

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QFileDialog,
    QMessageBox,
)

from ui.widgets import (
    LogPanel,
    InfoCard,
    FileInputActions,
    accept_url_drag,
    receive_url_drop,
    report_input_error,
)
from utils.nfo import read_text_any_encoding


class NFOWorker(QThread):
    """后台 NFO 修复线程。"""

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(int, int)  # 内容修正数, 关联文件重命名数

    def __init__(self, directory: str, parent=None):
        super().__init__(parent)
        self._dir = directory
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        from utils.nfo import preview_nfo_fixes, execute_nfo_fixes

        try:
            plan = getattr(self, "plan", None)
            if plan is None:
                plan = preview_nfo_fixes([self._dir], lambda: self._stopped)
            result = execute_nfo_fixes(plan, lambda: self._stopped, self.log.emit)
            self.stats = result
            self.progress.emit(100 if not result["cancelled"] and not result.get("fail") else 0)
            self.finished.emit(result["fixed"], result["renamed"])
        except Exception as exc:
            self.stats = {"error": str(exc)}
            self.log.emit("NFO修复失败：" + str(exc))
            self.finished.emit(0, 0)


class NFOPage(QWidget):
    """NFO 关联修复页面 — 支持拖入目录。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        # 必须保存：_ai_check_nfo() 会读 self.cfg.ai_enabled / ai_base_url 等
        self.cfg = cfg
        self._dir: str | None = None
        self._worker: NFOWorker | None = None
        self._plan = None
        self.setAcceptDrops(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("NFO 关联修复")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "在 .ts 转码为 .mp4 后，关联的 NFO 文件、封面图片仍指向旧文件名。\n"
            "本工具会自动：① 重命名关联文件 (.ts.nfo → .mp4.nfo)\n"
            "② 修正 NFO 文件内部的 .ts 引用为 .mp4\n"
            "拖入媒体库根目录，或点击下方按钮选择目录。"
        )
        layout.addWidget(hint)

        dir_row = QHBoxLayout()
        self.dir_label = QLabel("未选择目录")
        self.dir_label.setProperty("cssClass", "path-display")
        dir_row.addWidget(self.dir_label, 1)

        dir_row.addWidget(FileInputActions(self, self._receive_directories, files=False))
        layout.addLayout(dir_row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        layout.addWidget(QLabel("处理日志:"))
        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel)

        btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("只读生成修复计划")
        self.btn_preview.clicked.connect(self._preview)
        btn_row.addWidget(self.btn_preview)
        self.btn_start = QPushButton("开始修复")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        self.btn_start.setEnabled(False)
        btn_row.addWidget(self.btn_start)

        self.btn_ai = QPushButton("🤖 冲突 NFO 检查")
        self.btn_ai.setToolTip(
            "扫描目录，找出同一视频存在多个 NFO 的目录，用 AI 判断哪个最优、哪些冗余（只给建议，不自动删）。"
        )
        self.btn_ai.clicked.connect(self._ai_check_nfo)
        btn_row.addWidget(self.btn_ai)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    # ── AI 冲突 NFO 检查 ──
    def _preview(self):
        if busy(self) or not self._dir:
            return
        from utils.nfo import preview_nfo_fixes

        directory = self._dir
        self._preview_worker = CallWorker(lambda cancel: preview_nfo_fixes([directory], cancel))
        self._preview_worker.result.connect(self._show_plan)
        self._preview_worker.error.connect(self.log_panel.log_error)
        start_task(self, self._preview_worker, "生成NFO/关联资源修复计划")

    def _show_plan(self, plan):
        self._plan = plan
        self.log_panel.clear()
        for row in plan:
            self.log_panel.log_info(row["source"] + " → " + row["target"] + " | " + row["status"])
        self.btn_start.setEnabled(bool(plan) and all(row["status"] == "ready" for row in plan))

    def _ai_check_nfo(self) -> None:
        if busy(self):
            return
        if not self.cfg.ai_enabled or not self._dir:
            QMessageBox.information(self, "准备AI检查", "先选择目录并在设置启用AI。")
            return
        cfg, directory = copy.deepcopy(self.cfg), self._dir

        def check(cancel):
            from collections import defaultdict
            from workers.ai import query_llm

            conflicts = []
            for root, _, files in os.walk(directory):
                if cancel():
                    return []
                by_video = defaultdict(list)
                for filename in files:
                    if filename.lower().endswith(".nfo"):
                        stem = Path(filename).stem.lower()
                        for suffix in (".ts", ".mp4", ".mkv"):
                            if stem.endswith(suffix):
                                stem = stem[: -len(suffix)]
                        by_video[stem].append(filename)
                for names in by_video.values():
                    if len(names) >= 2:
                        conflicts.append((root, names))
            output = []
            for root, names in conflicts[:8]:
                if cancel():
                    break
                records = [
                    name
                    + "\n"
                    + (read_text_any_encoding(os.path.join(root, name)) or "无法读取")[:4000]
                    for name in names
                ]
                prompt = (
                    "以下为同一视频不同NFO的真实内容片段。比较完整性/引用，仅给出建议与不确定性，不执行删除：\n"
                    + "\n".join(records)
                )
                output.append(
                    query_llm(
                        cfg.ai_base_url,
                        cfg.ai_key,
                        cfg.ai_model,
                        [{"role": "user", "content": prompt}],
                        timeout=90,
                    )
                )
            return output

        self._ai_nfo_worker = CallWorker(check)
        self._ai_nfo_worker.result.connect(
            lambda rows: self.log_panel.log_info("\n".join(rows) or "未发现同一视频的NFO冲突。")
        )
        self._ai_nfo_worker.error.connect(self.log_panel.log_error)
        start_task(self, self._ai_nfo_worker, "比较同一视频的NFO内容")

    # ── 拖拽支持 ──
    def dragEnterEvent(self, event):
        accept_url_drag(self, event)

    def dropEvent(self, event):
        receive_url_drop(self, event, self._receive_directories)

    def _receive_directories(self, paths):
        if busy(self):
            return
        if len(paths) != 1 or not os.path.isdir(paths[0]):
            report_input_error(
                self, "本页一次处理一个媒体库文件夹。请单独添加文件夹，不要选择文件或多个目录。"
            )
            return
        self._set_dir(paths[0])

    def _set_dir(self, d: str) -> None:
        if busy(self):
            return
        self._dir = d
        self.dir_label.setText(d)
        self._plan = None
        self.btn_start.setEnabled(False)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择媒体库目录")
        if d:
            self._set_dir(d)

    def _start(self) -> None:
        if busy(self, mutating=True):
            return
        if not self._dir:
            return
        if not self._plan or any(row["status"] != "ready" for row in self._plan):
            QMessageBox.information(self, "先审阅计划", "请先只读生成计划，排除关联文件冲突。")
            return
        detail = "\n".join(row["source"] + " → " + row["target"] for row in self._plan)
        if (
            QMessageBox.question(
                self,
                "确认NFO修复计划",
                detail + "\n\n原文件会保留备份。执行以上同一计划？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.clear()
        self.progress.setValue(0)

        self._worker = NFOWorker(self._dir)
        self._worker.plan = list(self._plan)
        self._worker.log.connect(self.log_panel.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        start_task(self, self._worker)

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def shutdown(self) -> None:
        stop_page(self)

    def _on_finished(self, fixed: int, renamed: int) -> None:
        self._plan = None
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.log_panel.log("─" * 50)
        stats = getattr(self._worker, "stats", {})
        if stats.get("cancelled") or getattr(self._worker, "_stopped", False):
            self.progress.setValue(0)
            self.log_panel.log_warning(f"已取消；内容修正 {fixed} 项，关联重命名 {renamed} 项。")
        elif stats.get("error") or stats.get("fail"):
            self.progress.setValue(0)
            self.log_panel.log_error(
                "NFO修复失败或部分失败：" + str(stats.get("error") or stats.get("fails") or "")
            )
        else:
            self.log_panel.log_success(
                f"NFO修复完成：内容修正 {fixed} 项，关联重命名 {renamed} 项。"
            )
