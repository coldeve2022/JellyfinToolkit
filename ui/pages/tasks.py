"""Actual task monitoring and recoverable operation history."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QTabWidget,
    QMessageBox,
    QTextEdit,
)
from ui.services import TaskManager, CallWorker, start_task, stop_page, busy
from ui.services import events, analysis_worker, accept_snapshot, state_context

STATUS = {
    "running": "进行中",
    "stopping": "正在停止",
    "cancelled": "已取消",
    "failed": "失败",
    "succeeded": "完成",
    "interrupted": "上次运行中断",
}


class TaskCenterPage(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.manager = TaskManager.instance()
        self._worker = None
        root = QVBoxLayout(self)
        title = QLabel("任务中心")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)
        hint = QLabel(
            "这里显示实际任务、停止阶段和可恢复备份。文件恢复有冲突时会保留当前文件并说明原因。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)
        actions = QHBoxLayout()
        stop = self.btn_stop = QPushButton("停止选中任务")
        stop.clicked.connect(self._stop_selected)
        actions.addWidget(stop)
        refresh = QPushButton("刷新备份记录")
        refresh.clicked.connect(self._load_backups)
        actions.addWidget(refresh)
        restore = QPushButton("恢复选中备份…")
        restore.clicked.connect(self._restore_selected)
        actions.addWidget(restore)
        actions.addStretch()
        root.addLayout(actions)
        tabs = QTabWidget()
        self.tasks = self._table(["任务", "状态", "进度", "开始", "结束"])
        self.backups = self._table(["动作", "状态", "原路径", "目标 / 备份", "记录时间"])
        tabs.addTab(self.tasks, "任务与历史")
        tabs.addTab(self.backups, "文件备份与删除恢复")
        migration_page = QWidget()
        migration_layout = QVBoxLayout(migration_page)
        self.migration_hint = QLabel(
            "先采集当前库及观看用户，再预览旧标签；仅勾选匹配项迁入当前用户，原记录保留。"
        )
        self.migration_hint.setWordWrap(True)
        migration_layout.addWidget(self.migration_hint)
        migration_actions = QHBoxLayout()
        preview = QPushButton("扫描并预览旧标签")
        preview.clicked.connect(self._preview_legacy)
        migration_actions.addWidget(preview)
        self.btn_adopt_legacy = QPushButton("采纳勾选旧标签…")
        self.btn_adopt_legacy.setEnabled(False)
        self.btn_adopt_legacy.clicked.connect(self._adopt_legacy)
        migration_actions.addWidget(self.btn_adopt_legacy)
        migration_layout.addLayout(migration_actions)
        self.migration = self._table(["采纳", "作品", "旧标签", "媒体ID"])
        migration_layout.addWidget(self.migration, 1)
        tabs.addTab(migration_page, "旧反馈迁移")
        self._migration_items = []
        self._migration_context = None
        events.config_changed.connect(self._invalidate_migration)
        root.addWidget(tabs, 1)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(150)
        root.addWidget(self.details)
        self.tasks.itemSelectionChanged.connect(self._show_details)
        self.manager.changed.connect(self._refresh_tasks)
        self._refresh_tasks()

    def _table(self, headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().hide()
        return table

    def _refresh_tasks(self):
        selected = {item.data(Qt.UserRole) for item in self.tasks.selectedItems()}
        self.tasks.setRowCount(0)
        for record in reversed(self.manager.records):
            row = self.tasks.rowCount()
            self.tasks.insertRow(row)
            values = [
                record["title"],
                STATUS.get(record["status"], record["status"]),
                str(record["progress"]) + "%",
                record["started"],
                record.get("ended", "—"),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, record["id"])
                self.tasks.setItem(row, column, item)
            if record["id"] in selected:
                self.tasks.selectRow(row)

    def _show_details(self):
        selected = self.tasks.selectedItems()
        if selected:
            task_id = selected[0].data(Qt.UserRole)
            record = next((r for r in self.manager.records if r["id"] == task_id), {})
            self.details.setPlainText(
                record.get("error", "") + "\n" + "\n".join(record.get("logs", []))
            )

    def _stop_selected(self):
        for task_id in {item.data(Qt.UserRole) for item in self.tasks.selectedItems()}:
            self.manager.stop(task_id)

    def _load_backups(self):
        if busy(self):
            return

        def load(cancel):
            from utils.file_ops import OperationJournal
            from utils.trash import list_quarantine

            entries = []
            sessions = set()
            for entry in OperationJournal().all():
                session = entry.get("session_id")
                if session:
                    if session not in sessions:
                        sessions.add(session)
                        entries.append(
                            ("merge_session", dict(entry, id=session, kind="整组分集合并恢复"))
                        )
                elif entry["kind"] != "quarantine":
                    entries.append(("operation", entry))
            entries += [("quarantine", entry) for entry in list_quarantine()]
            return entries

        self._worker = CallWorker(load)
        self._worker.result.connect(self._render_backups)
        self._worker.error.connect(lambda message: self.details.setPlainText(message))
        start_task(self, self._worker, "读取备份记录")

    def _render_backups(self, entries):
        self.backups.setRowCount(0)
        for kind, entry in entries:
            row = self.backups.rowCount()
            self.backups.insertRow(row)
            values = [
                entry.get("kind", kind),
                entry.get("state", ""),
                entry.get("source") or entry.get("path", ""),
                entry.get("backup_path") or entry.get("target", ""),
                entry.get("created_at", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, (kind, entry["id"]))
                self.backups.setItem(row, column, item)

    def _restore_selected(self):
        if busy(self, mutating=True):
            return
        selected = {item.data(Qt.UserRole) for item in self.backups.selectedItems()}
        if not selected:
            return
        if (
            QMessageBox.question(
                self,
                "审核恢复计划",
                f"恢复选中的 {len(selected)} 个备份。现有文件冲突时会拒绝覆盖。继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

        def restore(cancel):
            from utils.file_ops import OperationJournal
            from utils.trash import restore_quarantined

            output = []
            for kind, operation_id in selected:
                if cancel():
                    break
                if kind == "quarantine":
                    ok, detail = restore_quarantined(operation_id, should_stop=cancel)
                    output.append({"ok": ok, "error": "" if ok else detail, "id": operation_id})
                elif kind == "merge_session":
                    from utils.merge_archive import restore_session

                    stats = restore_session(operation_id, should_stop=cancel)
                    output.append(
                        {
                            "ok": not stats["fail"] and not stats["cancelled"],
                            "error": "；".join(str(reason) for _, reason in stats["fails"]),
                            "id": operation_id,
                        }
                    )
                else:
                    output.append(OperationJournal().restore(operation_id, should_stop=cancel))
            return output

        self._worker = CallWorker(restore)
        self._worker.result.connect(self._on_restored)
        self._worker.error.connect(self.details.setPlainText)
        start_task(self, self._worker, "恢复选中的文件备份", mutating=True)

    def _on_restored(self, result):
        self.details.setPlainText(
            "\n".join(
                "已恢复" if row.get("ok") else str(row.get("error") or "恢复失败") for row in result
            )
        )
        if any(row.get("ok") for row in result):
            events.media_changed.emit([])

    def shutdown(self):
        stop_page(self)

    def _invalidate_migration(self):
        self._migration_items = []
        self._migration_context = None
        self.migration.setRowCount(0)
        self.btn_adopt_legacy.setEnabled(False)

    def _preview_legacy(self):
        if busy(self):
            return
        self._invalidate_migration()
        self._worker = analysis_worker(self.cfg, {})
        self._worker.finished.connect(self._on_legacy_preview)
        start_task(self, self._worker, "采集当前身份并预览旧标签")

    def _on_legacy_preview(self, data):
        if not accept_snapshot(self, data):
            self.migration_hint.setText(str(data.get("error") or "扫描已取消。"))
            return
        from config import DATA_DIR
        from utils.labels import preview_legacy_labels

        try:
            rows = preview_legacy_labels(DATA_DIR / "smart.db", self.cfg, data.get("items", []))
        except ValueError as exc:
            self.migration_hint.setText(str(exc))
            return
        self._migration_items = data.get("items", [])
        self._migration_context = state_context(self.cfg)
        self.migration.setRowCount(0)
        for row in rows:
            index = self.migration.rowCount()
            self.migration.insertRow(index)
            checkbox = QTableWidgetItem()
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            checkbox.setCheckState(Qt.Unchecked)
            checkbox.setData(Qt.UserRole, row["item_id"])
            self.migration.setItem(index, 0, checkbox)
            for column, key in enumerate(("name", "label", "item_id"), 1):
                self.migration.setItem(index, column, QTableWidgetItem(row[key]))
        self.btn_adopt_legacy.setEnabled(bool(rows))
        self.migration_hint.setText(
            f"匹配 {len(rows)} 条旧反馈。已有当前标签的作品不会覆写；未采纳前旧保留/喜欢仍提供安全保护。"
        )

    def _adopt_legacy(self):
        if busy(self, mutating=True):
            return
        if self._migration_context != state_context(self.cfg):
            self._invalidate_migration()
            self.migration_hint.setText("来源或观看用户已改变，请重新预览。")
            return
        ids = [
            self.migration.item(row, 0).data(Qt.UserRole)
            for row in range(self.migration.rowCount())
            if self.migration.item(row, 0).checkState() == Qt.Checked
        ]
        if not ids:
            return
        detail = "\n".join(
            self.migration.item(row, 1).text() + " | " + self.migration.item(row, 2).text()
            for row in range(self.migration.rowCount())
            if self.migration.item(row, 0).checkState() == Qt.Checked
        )
        if (
            QMessageBox.question(
                self,
                "审核旧反馈迁移",
                detail + "\n\n将以上标签采纳到当前库及观看用户，保留旧记录。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        from config import DATA_DIR
        from utils.labels import adopt_legacy_labels

        try:
            count = adopt_legacy_labels(DATA_DIR / "smart.db", self.cfg, self._migration_items, ids)
        except ValueError as exc:
            self.migration_hint.setText(str(exc))
            return
        events.labels_changed.emit()
        self._invalidate_migration()
        self.migration_hint.setText(f"已采纳 {count} 条旧标签；原记录已保留。")
