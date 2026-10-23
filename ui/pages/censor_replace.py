"""找替换页 —— 阶段二：对**已确认有码**的视频，按番号去网上查无码版。

为什么单独一页
--------------
"检查有没有码"和"上网找替换"是两件独立的事（见 docs/像素化状态与工作流.md）。
把它们塞在一页里，检查页的表格既要显示结论又要显示检索状态、还要放候选面板，
最后两边都不好用。

这一页只干一件事：**拿着你已经标记为「有码」的番号，去站点查有没有无码版。**
好处是：

- 检查页的表格可以精简回"文件名 / 结论 / 处理 / 记录来源"；
- 已确认有码的视频在这里**按番号聚合**（一部片多个副本只占一行），
  可以反复重查、可以导出清单；
- 检索失败、站点被拦这些事不会干扰检查流程。

数据来源：``VerdictStore.confirmed_censored()`` —— 只认真人标记，不认算法猜测。
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QSplitter, QMenu, QLineEdit, QComboBox,
)

from censor import CensorService
from censor.releases import ReleaseReport, ReleaseService
from censor.releases.models import normalize_number
from ui.censor_release import ReleaseLookupWorker, ReleasePanel
from ui.services import events, start_task
from ui.widgets import make_number_item
from ui import theme

__all__ = ["CensorReplacePage"]

_COLUMNS = ["番号", "片数", "检索结果", "候选", "最大种子", "最近检索"]

_STATE_COLOR = {
    "confirmed": "#1e8449",   # 确有
    "likely": "#b9770e",      # 疑似
    "none": "#7f8c8d",        # 未见
    "unknown": "#7f8c8d",     # 未查
    "todo": "#d35400",        # 还没查
}
_STATE_TOKEN = {"confirmed": "success", "likely": "warning", "none": "text_secondary",
                "unknown": "text_secondary", "todo": "warning"}


@dataclass
class NumberRow:
    """一个番号（可能对应多个文件副本）。"""

    number: str
    paths: list[str] = field(default_factory=list)
    report: Optional[ReleaseReport] = None

    @property
    def key(self) -> str:
        return normalize_number(self.number) or "（无番号）"

    @property
    def searchable(self) -> bool:
        return bool(normalize_number(self.number))

    @property
    def state_key(self) -> str:
        return "todo" if self.report is None else self.report.stage

    @property
    def state_text(self) -> str:
        if self.report is None:
            return "未查" if self.searchable else "无番号"
        return self.report.as_text()

    @property
    def best_magnet_text(self) -> str:
        if self.report is None:
            return "—"
        best = self.report.best
        if best is None or best.best_magnet is None:
            return "—"
        return best.best_magnet.size_text


class CensorReplacePage(QWidget):
    """阶段二工作台：已确认有码 → 查无码版。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._censor = CensorService(cfg)
        self._release = ReleaseService(cfg)
        self._rows: dict[str, NumberRow] = {}
        self._worker: Optional[ReleaseLookupWorker] = None
        self._refresh_worker = None
        self._refresh_version = 0
        self._refresh_dirty = False
        self._auto_paths = []
        self._refresh_owner = QWidget(self)
        self._refresh_owner.hide()
        self._current_key: str = ""
        self._setup_ui()
        events.censor_changed.connect(self._on_censor_changed)
        events.media_changed.connect(self._media_changed)

    def _media_changed(self, paths):
        self.refresh()

    def _on_censor_changed(self, paths):
        self._auto_paths = list(dict.fromkeys(self._auto_paths + list(paths)))
        self.refresh()

    def _run_auto(self):
        if self._worker is not None and self._worker.isRunning():
            return
        numbers = getattr(self, '_auto_numbers', [])
        self._auto_numbers = []
        if numbers:
            self._lookup(force=False, numbers=numbers)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        toolbar = QHBoxLayout()
        title = QLabel("找替换 · 已确认有码")
        title.setProperty("cssClass", "page-title")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.btn_refresh = QPushButton("刷新清单")
        self.btn_refresh.setToolTip("重新读库里的「已确认有码」记录")
        self.btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(self.btn_refresh)
        self.btn_lookup_new = QPushButton("检索未查与过期")
        self.btn_lookup_new.setToolTip("只查还没有结果或结果已过期的番号")
        self.btn_lookup_new.setProperty("cssClass", "accent")
        self.btn_lookup_new.clicked.connect(lambda: self._lookup(force=False))
        toolbar.addWidget(self.btn_lookup_new)
        self.btn_lookup_all = QPushButton("全部重查")
        self.btn_lookup_all.setToolTip("忽略缓存，把所有番号重新查一遍")
        self.btn_lookup_all.clicked.connect(lambda: self._lookup(force=True))
        toolbar.addWidget(self.btn_lookup_all)
        retry = QPushButton("重试失败")
        retry.clicked.connect(self._retry)
        toolbar.addWidget(retry)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        toolbar.addWidget(self.btn_stop)
        self.btn_export = QPushButton("导出清单")
        self.btn_export.setToolTip("把番号与检索结果导出为 CSV，方便在别处反复检索")
        self.btn_export.clicked.connect(self._export)
        toolbar.addWidget(self.btn_export)
        settings = QPushButton("检索设置…")
        settings.clicked.connect(self._settings)
        toolbar.addWidget(settings)
        more = QPushButton('更多')
        menu = QMenu(more)
        for button in (self.btn_refresh, self.btn_lookup_all, retry, self.btn_export, settings):
            toolbar.removeWidget(button)
            button.hide()
            menu.addAction(button.text(), button.click)
        more.setMenu(menu)
        toolbar.addWidget(more)
        selected = QPushButton('检索所选')
        selected.clicked.connect(self._lookup_selected)
        toolbar.insertWidget(2, selected)
        layout.addLayout(toolbar)

        self.lbl_summary = QLabel("正在读取已确认有码的记录…")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setProperty("cssClass", "muted-label")
        layout.addWidget(self.lbl_summary)

        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索番号或文件路径…')
        self.search.setClearButtonEnabled(True)
        self.search.setProperty('taskInteractive', True)
        self.search.textChanged.connect(self._filter)
        filter_row.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        self.state_filter.setProperty('taskInteractive', True)
        for label, key in (('全部', ''), ('确有', 'confirmed'), ('疑似', 'likely'),
                           ('未见', 'none'), ('未查/未完成', 'unknown')):
            self.state_filter.addItem(label, key)
        self.state_filter.currentIndexChanged.connect(self._filter)
        filter_row.addWidget(self.state_filter)
        layout.addLayout(filter_row)
        body = QSplitter(Qt.Horizontal)
        self.splitter = body
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setProperty("taskInteractive", True)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(60)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsMovable(True)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 100)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        body.addWidget(self.table)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(6, 0, 0, 0)
        self.lbl_detail = QLabel("选择一个番号查看候选与文件")
        self.lbl_detail.setWordWrap(True)
        right_lay.addWidget(self.lbl_detail)
        self.panel = ReleasePanel(self)
        self.panel.btn_lookup.clicked.connect(self._lookup_current)
        right_lay.addWidget(self.panel)
        self.files = QTableWidget(0, 1)
        self.files.setHorizontalHeaderLabels(["包含的文件"])
        self.files.verticalHeader().hide()
        self.files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        right_lay.addWidget(self.files, 1)
        local = QPushButton("导入已下载候选并审阅替换…")
        local.clicked.connect(self._import_candidate)
        right_lay.addWidget(local)
        body.addWidget(right)
        body.setSizes([580, 420])
        layout.addWidget(body, 1)
        from ui.workspace_preferences import WorkspacePreferences
        self.workspace = WorkspacePreferences(self, 'release', self.table, splitter=body,
            filters={'search': self.search, 'state': self.state_filter})
        menu.addMenu(self.workspace.menu(more)).setText('工作区布局与偏好')

    # ── 数据 ──
    def refresh(self) -> None:
        """从库里重建清单：已确认有码 → 按番号聚合。"""
        self._refresh_version += 1
        if self._refresh_worker is not None:
            self._refresh_dirty = True
            self._refresh_worker.stop()
            return
        from ui.services import CallWorker
        version = self._refresh_version
        censor, release = self._censor, self._release
        worker = CallWorker(lambda stopped: self._collect_rows(censor, release, stopped))
        # The manager retains the thread even if a view is destroyed while a
        # read is in flight. Bound slots disconnect automatically with the view.
        from ui.services import TaskManager
        worker.setParent(TaskManager.instance())
        worker.setProperty('taskAutoDelete', True)
        worker.refresh_version = version
        self._refresh_worker = worker
        worker.result.connect(self._refresh_result)
        worker.error.connect(self._refresh_error)
        worker.finished.connect(self._refresh_finished)
        start_task(self._refresh_owner, worker, '刷新已确认作品清单')

    @Slot(object)
    def _refresh_result(self, rows):
        self._accept_refresh(self.sender().refresh_version, rows)

    @Slot(str)
    def _refresh_error(self, error):
        self.lbl_summary.setText(f'读取失败：{error}')

    @staticmethod
    def _collect_rows(censor, release, stopped):
        records = censor.store.confirmed_censored()
        grouped: dict[str, NumberRow] = {}
        for item in records:
            if stopped():
                return {}
            key = normalize_number(item.number) or "（无番号）"
            row = grouped.setdefault(key, NumberRow(number=item.number or ""))
            if not row.number and item.number:
                row.number = item.number
            row.paths.append(item.path)
        reports = release.store.get_many(grouped)
        attempts = release.store.latest_attempts(grouped)
        for key, row in grouped.items():
            cached = reports.get(key)
            attempt = attempts.get(key)
            if attempt and (not cached or attempt.queried_at >= cached.queried_at):
                cached = attempt
            row.report = cached
        return grouped

    def _accept_refresh(self, version, grouped):
        if version != self._refresh_version:
            return
        # Preserve reports delivered while the list query was running.
        for key, row in grouped.items():
            current = self._rows.get(key)
            if current and current.report and (not row.report or current.report.queried_at > row.report.queried_at):
                row.report = current.report
        self._rows = grouped
        self._fill_table()
        self._update_summary()
        paths, self._auto_paths = set(self._auto_paths), []
        numbers = [r.number for r in grouped.values() if r.searchable and paths.intersection(r.paths)]
        if numbers:
            self._release.queue(numbers, note='人工确认有码')
        from censor.releases.transport import offline
        if numbers and self.cfg.release_enabled and self.cfg.release_auto_lookup and not offline():
            self._auto_numbers = list(dict.fromkeys(getattr(self, '_auto_numbers', []) + numbers))
            self._run_auto()

    @Slot()
    def _refresh_finished(self):
        worker = self.sender()
        if self._refresh_worker is worker:
            self._refresh_worker = None
        if self._refresh_dirty:
            self._refresh_dirty = False
            self.refresh()

    def _fill_table(self) -> None:
        selected = self._current_key
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for row in sorted(self._rows.values(), key=lambda r: r.key):
            index = self.table.rowCount()
            self.table.insertRow(index)
            values = [
                row.number or "（无番号）",
                str(len(row.paths)),
                row.state_text,
                str(len(row.report.candidates)) if row.report else "—",
                row.best_magnet_text,
                self._queried_at(row),
            ]
            for col, text in enumerate(values):
                item = make_number_item(text, len(row.paths) if col == 1 else
                                        len(row.report.candidates) if row.report else 0) \
                    if col in (1, 3) else QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.UserRole, row.key)
                if col == 2:
                    item.setForeground(QColor(theme.color(_STATE_TOKEN.get(row.state_key, "text_primary"))))
                self.table.setItem(index, col, item)
        self.table.setSortingEnabled(True)
        self._filter()
        if selected in self._rows:
            for index in range(self.table.rowCount()):
                if self.table.item(index, 0).data(Qt.UserRole) == selected:
                    self.table.selectRow(index)
                    break
        else:
            self._current_key = ""
            self.panel.clear()
            self.files.setRowCount(0)
            self.lbl_detail.setText("选择一个番号查看候选与文件")

    @staticmethod
    def _queried_at(row: NumberRow) -> str:
        if row.report is None or not row.report.queried_at:
            return "—"
        from datetime import datetime

        return datetime.fromtimestamp(row.report.queried_at).strftime("%m-%d %H:%M")

    def _update_summary(self) -> None:
        rows = list(self._rows.values())
        searchable = [r for r in rows if r.searchable]
        counts = {"todo": 0, "confirmed": 0, "likely": 0, "none": 0, "unknown": 0}
        # 无番号的片子无法检索，不能算进"待查" —— 否则待查数永远降不到 0
        for row in searchable:
            counts[row.state_key] += 1
        self.lbl_summary.setText(
            f"已确认有码 {len(rows)} 个番号（{sum(len(r.paths) for r in rows)} 个文件）"
            f" · 可检索 {len(searchable)} · 待查 {counts['todo']}"
            f" · 确有 {counts['confirmed']} · 疑似 {counts['likely']}"
            f" · 未见 {counts['none']} · 未查 {counts['unknown']}"
            + ("（没有番号的片子无法检索）" if len(rows) > len(searchable) else ""))

    # ── 选择 ──
    def _on_row_selected(self) -> None:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return
        item = self.table.item(indexes[0].row(), 0)
        key = item.data(Qt.UserRole) if item else None
        row = self._rows.get(key)
        if row is None:
            return
        self._current_key = key
        self.lbl_detail.setText(
            f"{row.number or '（无番号）'} · {len(row.paths)} 个文件"
            f" · {row.state_text}")
        self.panel.show_report(row.report)
        self._fill_files(row)

    def _fill_files(self, row: NumberRow) -> None:
        self.files.setRowCount(0)
        for path in row.paths:
            index = self.files.rowCount()
            self.files.insertRow(index)
            item = QTableWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            self.files.setItem(index, 0, item)

    # ── 检索 ──
    def _filter(self):
        words, state = self.search.text().casefold().split(), self.state_filter.currentData()
        hidden_current = False
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            row = self._rows.get(item.data(Qt.UserRole)) if item else None
            show = row is not None and (not state or row.state_key == state or
                    (state == 'unknown' and row.state_key == 'todo'))
            if row:
                show = show and all(word in (row.number + ' ' + ' '.join(row.paths)).casefold() for word in words)
            self.table.setRowHidden(index, not show)
            hidden_current |= not show and row is not None and row.key == self._current_key
        if hidden_current:
            self.table.clearSelection()
            self._current_key = ''
            self.files.setRowCount(0)
            self.panel.clear()
            self.lbl_detail.setText('选择一个番号查看候选与文件')

    def _lookup_selected(self):
        indexes = self.table.selectionModel().selectedRows()
        numbers = [self._rows[self.table.item(index.row(), 0).data(Qt.UserRole)].number
                   for index in indexes if not self.table.isRowHidden(index.row())]
        if numbers:
            self._lookup(force=True, numbers=numbers)
        else:
            self.lbl_summary.setText('先选择一个或多个作品')

    def lookup_targets(self, force=False) -> list[str]:
        return [row.number for row in self._rows.values()
                if row.searchable and (force or self._release.fresh(row.number) is None)]

    def _lookup(self, *, force: bool, numbers: Optional[list[str]] = None) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.lbl_summary.setText("检索进行中，先等它结束或点「停止」")
            return
        targets = numbers
        if targets is None:
            self._prepare_lookup(force)
            return
        targets = list(dict.fromkeys(t for t in targets if t))
        if not targets:
            self.lbl_summary.setText(
                "没有需要检索的番号" if force else "都已查过 —— 要重查请用「全部重查」")
            return
        worker = ReleaseLookupWorker(
            self._release, targets,
            ttl_days=float(self.cfg.release_cache_days), force=force, parent=self)
        worker.report_ready.connect(self._on_report)
        worker.progressed.connect(self._on_progress)
        worker.logged.connect(lambda text: self.lbl_summary.setText(text))
        worker.all_done.connect(self._on_done)
        worker.finished.connect(lambda: self._lookup_finished(worker))
        self._worker = worker
        self.btn_stop.setEnabled(True)
        self.btn_lookup_new.setEnabled(False)
        self.btn_lookup_all.setEnabled(False)
        self.lbl_summary.setText(f"正在检索 {len(targets)} 个番号…")
        if not start_task(self, worker, "无码版本检索", mutating=False):
            self._worker = None
            self.btn_stop.setEnabled(False)
            self.btn_lookup_new.setEnabled(True)
            self.btn_lookup_all.setEnabled(True)

    def _prepare_lookup(self, force):
        from copy import copy, deepcopy
        from ui.services import CallWorker, busy, TaskManager
        if busy(self):
            self.lbl_summary.setText('正在准备或检索，请等待完成')
            return
        numbers = [row.number for row in self._rows.values() if row.searchable]
        if force:
            self._lookup(force=True, numbers=numbers)
            return
        service = copy(self._release)
        service.cfg = deepcopy(self.cfg)
        def prepare(stopped):
            fresh = service.fresh_many(numbers)
            return [n for n in numbers if normalize_number(n) not in fresh] if not stopped() else []
        worker = CallWorker(prepare)
        worker.setParent(TaskManager.instance())
        worker.setProperty('taskAutoDelete', True)
        worker.request_config = {k: deepcopy(v) for k, v in vars(self.cfg).items() if k.startswith('release_')}
        self._lookup_prepare = worker
        worker.result.connect(self._lookup_prepared)
        worker.error.connect(self._refresh_error)
        self.lbl_summary.setText('后台校验检索缓存与待办…')
        start_task(self, worker, '校验检索缓存与待办')

    @Slot(object)
    def _lookup_prepared(self, targets):
        worker = self.sender()
        if worker.request_config != {k: v for k, v in vars(self.cfg).items() if k.startswith('release_')}:
            self.lbl_summary.setText('检索设置已变化，请重新开始')
            return
        self._lookup(force=False, numbers=targets)

    def _lookup_finished(self, worker):
        if self._worker is worker:
            self._worker = None
        from PySide6.QtCore import QTimer
        if getattr(self, '_auto_numbers', []):
            QTimer.singleShot(60, self, self._run_auto)

    def _retry(self):
        self._release.store.retry_failed()
        self._lookup(force=False)

    def _lookup_current(self) -> None:
        row = self._rows.get(self._current_key)
        if row is None:
            self.lbl_summary.setText("先选一个番号")
            return
        if not row.searchable:
            self.lbl_summary.setText("这一行没有番号，无法按番号检索")
            return
        self._lookup(force=True, numbers=[row.number])

    def _stop(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self.lbl_summary.setText("已请求停止，当前番号查完就停")

    def _on_report(self, report: ReleaseReport) -> None:
        key = normalize_number(report.number)
        row = self._rows.get(key)
        if row is not None:
            row.report = report
        if row is not None:
            self._update_row_cells(row)
        if key == self._current_key:
            self.panel.show_report(report)
            self.lbl_detail.setText(
                f"{row.number if row else report.number}"
                f" · {len(row.paths) if row else 0} 个文件 · {report.as_text()}")

    def _update_row_cells(self, row: NumberRow) -> None:
        """只刷这一行的 4 列，**不重建整表** —— 重建会丢掉当前选中。"""
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            if item and item.data(Qt.UserRole) == row.key:
                sorting = self.table.isSortingEnabled()
                self.table.setSortingEnabled(False)
                cells = [row.state_text,
                         str(len(row.report.candidates)) if row.report else "—",
                         row.best_magnet_text,
                         self._queried_at(row)]
                for offset, text in enumerate(cells, start=2):
                    cell = self.table.item(index, offset)
                    if cell is not None:
                        cell.setText(text)
                        if offset == 3:
                            cell.setData(Qt.UserRole, len(row.report.candidates) if row.report else 0)
                        if offset == 2:
                            cell.setForeground(QColor(theme.color(_STATE_TOKEN.get(row.state_key, "text_primary"))))
                self.table.setSortingEnabled(sorting)
                self._filter()
                return

    def _on_progress(self, done: int, total: int) -> None:
        self.lbl_summary.setText(f"正在检索 {done}/{total} 个番号…")

    def _on_done(self, count: int) -> None:
        self.btn_stop.setEnabled(False)
        self.btn_lookup_new.setEnabled(True)
        self.btn_lookup_all.setEnabled(True)
        self._fill_table()
        self._update_summary()
        stats = getattr(self._worker, 'stats', {})
        self.lbl_summary.setText(self.lbl_summary.text() +
            f"（本次尝试 {count} 个 · 完成 {stats.get('ok', 0)} · 未完成 {stats.get('fail', 0)}"
            + (" · 已取消" if stats.get('cancelled') else "") + "）")

    # ── 导出 ──
    def _import_candidate(self):
        row = self._rows.get(self._current_key)
        if row is None:
            self.lbl_detail.setText("先选择一个作品，再导入它的本地候选视频")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择已下载候选", "",
            "视频文件 (" + " ".join("*" + ext for ext in self.cfg.video_extensions) + ")")
        if paths:
            events.replacement_ready.emit(paths, list(row.paths))

    def _export(self) -> None:
        if not self._rows:
            self.lbl_summary.setText("清单是空的，没有可导出的内容")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单", "已确认有码-找替换.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["番号", "文件数", "检索结果", "候选数", "最大种子", "磁链", "文件"])
                for row in sorted(self._rows.values(), key=lambda r: r.key):
                    best = row.report.best if row.report else None
                    magnet = best.best_magnet.uri if (best and best.best_magnet) else ""
                    writer.writerow([row.number, len(row.paths), row.state_text,
                                     len(row.report.candidates) if row.report else 0,
                                     row.best_magnet_text, magnet,
                                     " | ".join(row.paths)])
            self.lbl_summary.setText(f"已导出 {len(self._rows)} 个番号 → {path}")
        except OSError as exc:
            self.lbl_summary.setText(f"导出失败：{exc}")

    # ── 生命周期 ──
    def _settings(self):
        from ui.release_settings import edit_release_settings
        edit_release_settings(self.cfg, self)

    def on_config_changed(self) -> None:
        self._release = ReleaseService(self.cfg)
        self.refresh()

    def apply_theme(self):
        for row in self._rows.values():
            self._update_row_cells(row)

    def shutdown(self) -> None:
        self.workspace.save()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(3000)
