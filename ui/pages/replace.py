"""破解视频自动替换页面 — 匹配 restored 视频并覆盖媒体库原文件。"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page

import os
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
)

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, LogPanel, InfoCard, FileInputActions
from utils.censor_tags import update_jellyfin_db_tags
from utils.nfo import update_nfo_censor_tags
from utils.nfo_source import find_nfo
from ui.services import CallWorker


class ReplaceWorker(QThread):
    """后台替换线程 — 索引原库后匹配 restored 文件。

    安全策略：执行已审阅的同一匹配计划；保留事务备份和日志，
    失败回滚，成功后可在任务中心恢复。
    """

    log = Signal(str)
    finished = Signal(int, int)  # 成功数, 未匹配数

    def __init__(
        self,
        restored_paths: list[str],
        library_paths: list[str],
        marker: str = "restored",
        cfg: ToolkitConfig | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._restored = restored_paths
        self._library = library_paths
        self._marker = marker
        self._cfg = cfg
        self._db_backed_up = False
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        from utils.replacement import preview_replacements, execute_replacements

        try:
            plan = getattr(self, "plan", None)
            if plan is None:
                plan = preview_replacements(
                    self._restored, self._library, self._marker, lambda: self._stopped
                )
            stats = execute_replacements(plan, should_stop=lambda: self._stopped, log=self.log.emit)
            self.stats = stats
            for operation in stats.get("operations", []):
                if operation.get("ok") and not self._stopped:
                    self._sync_censor_metadata(operation["target"])
            self.finished.emit(stats["ok"], stats["fail"])
        except Exception as exc:
            self.stats = {"error": str(exc)}
            self.log.emit(f"替换失败：{exc}")
            self.finished.emit(0, 1)

    def _sync_censor_metadata(self, target: str) -> None:
        """替换成功后同步 NFO 与 Jellyfin 数据库的“有码 → 无码破解”。"""
        nfo = find_nfo(target)
        if nfo:
            ok, detail = update_nfo_censor_tags(nfo)
            self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"NFO 标签：{detail}")
        else:
            self.log.emit("  ⚠️ 未找到同名 NFO，跳过 NFO 标签同步")

        if not self._cfg:
            self.log.emit("  ⚠️ 未提供配置，跳过 Jellyfin 数据库标签同步")
            return
        db_path = getattr(self._cfg, "jellyfin_db_path", "")
        if not db_path or not Path(db_path).exists():
            self.log.emit(
                "  ⚠️ 未找到本地库文件，跳过数据库标签同步"
                f"（配置值：{db_path or '空'}）。\n"
                "     换名后的条目会在服务器下次扫描时自动更新；"
                "若使用 Emby 或「服务器 API」模式，请在服务器端刷新媒体库。"
            )
            return
        backup = not self._db_backed_up
        self._db_backed_up = True
        ok, detail = update_jellyfin_db_tags(db_path, target, backup=backup, log=self.log.emit)
        self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"Jellyfin 数据库：{detail}")


class ReplacePage(QWidget):
    """破解视频自动替换页面。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: ReplaceWorker | None = None
        self._plan = None
        self._setup_ui()
        self.list_restored.paths_changed.connect(self._invalidate_plan)
        self.list_library.paths_changed.connect(self._invalidate_plan)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("破解视频自动替换")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "工作原理：自动识别文件名中含 'restored' 的新视频，去除标记后匹配媒体库原文件并替换。\n"
            "左侧拖入 Lada 等工具破解完成的新视频 | 右侧拖入原始媒体库文件/文件夹\n"
            "替换前会自动备份原文件，失败时自动回滚，安全可靠。"
        )
        layout.addWidget(hint)

        splitter = QSplitter()
        left_w = QWidget()
        left_l = QVBoxLayout(left_w)
        left_l.setContentsMargins(0, 0, 6, 0)
        left_l.addWidget(QLabel("📦 破解完成的视频/文件夹:"))
        left_l.addWidget(FileInputActions(self, self._add_to_restored, role="restored"))
        self.list_restored = DragDropListWidget()
        self.list_restored.files_dropped.connect(self._add_to_restored)
        left_l.addWidget(self.list_restored)
        splitter.addWidget(left_w)

        right_w = QWidget()
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(6, 0, 0, 0)
        right_l.addWidget(QLabel("📚 原始媒体库 视频/文件夹:"))
        right_l.addWidget(FileInputActions(self, self._add_to_library, role="library"))
        self.list_library = DragDropListWidget()
        self.list_library.files_dropped.connect(self._add_to_library)
        right_l.addWidget(self.list_library)
        splitter.addWidget(right_w)
        splitter.setSizes([400, 400])
        layout.addWidget(splitter)
        self.pairs = QTableWidget(0, 3)
        self.pairs.setHorizontalHeaderLabels(["新视频", "待替换原文件", "配对状态"])
        self.pairs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.pairs.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.pairs.horizontalHeader().setStretchLastSection(True)
        self.pairs.setMaximumHeight(200)
        layout.addWidget(self.pairs)

        layout.addWidget(QLabel("处理日志:"))
        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(200)
        layout.addWidget(self.log_panel)

        btn_row = QHBoxLayout()
        self.btn_preview = QPushButton("生成配对计划")
        self.btn_preview.clicked.connect(self._preview)
        btn_row.addWidget(self.btn_preview)
        self.btn_start = QPushButton("🚀 开始自动替换")
        self.btn_start.setText("执行已审核替换计划…")
        self.btn_start.setEnabled(False)
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        btn_row.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_stop)

        btn_row.addStretch()
        clear_btn = QPushButton("清空队列")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self.stats = QLabel("")
        self.stats.setProperty("cssClass", "muted-label")
        layout.addWidget(self.stats)

    # ── 拖拽接收 ──
    def _invalidate_plan(self, *args):
        self._plan = None
        self.btn_start.setEnabled(False)
        self.pairs.setRowCount(0)

    def _preview(self):
        if busy(self):
            return
        from utils.replacement import preview_replacements

        restored = self.list_restored.get_all_paths()
        library = self.list_library.get_all_paths()
        marker = self.cfg.restored_suffix_marker
        self._worker = CallWorker(
            lambda cancel: preview_replacements(restored, library, marker, cancel)
        )
        self._worker.result.connect(self._show_plan)
        self._worker.error.connect(self.log_panel.log_error)
        start_task(self, self._worker, "生成恢复视频配对计划")

    def _show_plan(self, plan):
        self._plan = plan
        self.pairs.setRowCount(0)
        for entry in plan:
            row = self.pairs.rowCount()
            self.pairs.insertRow(row)
            for col, text in enumerate(
                (
                    entry["source"],
                    entry["target"] or " / ".join(entry["candidates"]),
                    entry["status"],
                )
            ):
                cell = QTableWidgetItem(str(text))
                cell.setToolTip(str(text))
                self.pairs.setItem(row, col, cell)
        self.btn_start.setEnabled(bool(plan) and all(entry["status"] == "ready" for entry in plan))
        self.stats.setText(f"配对计划 {len(plan)} 项；有冲突/多候选时请调整输入再重新生成计划。")

    def receive_outputs(self, restored, originals):
        if busy(self):
            return
        self._clear()
        self._add_to_restored(restored)
        self._add_to_library(originals)
        self._preview()

    def _add_to_restored(self, paths: list[str]) -> None:
        if busy(self):
            return
        for p in paths:
            self.list_restored.add_item(os.path.basename(p) or p, p)

    def _add_to_library(self, paths: list[str]) -> None:
        if busy(self):
            return
        for p in paths:
            self.list_library.add_item(os.path.basename(p) or p, p)

    def _start(self) -> None:
        if busy(self, mutating=True):
            return
        if self.list_restored.count() == 0 or self.list_library.count() == 0:
            QMessageBox.warning(self, "提示", "请确保两侧都拖入了文件或文件夹！")
            return
        if not self._plan or any(entry["status"] != "ready" for entry in self._plan):
            QMessageBox.information(self, "先审阅配对", "请先生成配对计划，排除多候选或冲突。")
            return
        detail = "\n".join(entry["source"] + " → " + entry["target"] for entry in self._plan)
        if (
            QMessageBox.question(
                self,
                "确认替换计划",
                detail + "\n\n原文件会保留可恢复备份。确认执行以上同一计划？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.clear()

        restored = self.list_restored.get_all_paths()
        library = self.list_library.get_all_paths()

        self._worker = ReplaceWorker(restored, library, self.cfg.restored_suffix_marker, self.cfg)
        self._worker.plan = list(self._plan)
        self._worker.log.connect(self.log_panel.log)
        self._worker.finished.connect(self._on_finished)
        start_task(self, self._worker)

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning("任务已停止")
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(False)

    def _on_finished(self, replaced: int, not_found: int) -> None:
        self.btn_start.setEnabled(False)
        self._plan = None
        self.btn_stop.setEnabled(False)
        self.log_panel.log("─" * 50)
        stats = getattr(self._worker, "stats", {})
        if stats.get("cancelled") or getattr(self._worker, "_stopped", False):
            self.log_panel.log_warning(f"已取消；已完成替换 {replaced} 项。")
        elif not_found or stats.get("error") or stats.get("fail"):
            self.log_panel.log_error(
                f"替换失败或部分失败：成功 {replaced} / 失败 {not_found}。"
                + str(stats.get("error") or stats.get("fails") or "")
            )
        else:
            self.log_panel.log_success(f"替换完成：{replaced} 项。")
        self.stats.setText(f"本次: 成功 {replaced} | 未匹配 {not_found}")

    def _clear(self) -> None:
        if busy(self):
            return
        self.list_restored.clear()
        self.list_library.clear()
        self.log_panel.clear()
        self.stats.setText("")

    def shutdown(self) -> None:
        stop_page(self)
