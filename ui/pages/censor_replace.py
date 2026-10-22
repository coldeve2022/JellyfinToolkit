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

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from censor import CensorService
from censor.releases import ReleaseReport, ReleaseService
from censor.releases.models import normalize_number
from ui.censor_release import ReleaseLookupWorker, ReleasePanel

__all__ = ["CensorReplacePage"]

_COLUMNS = ["番号", "片数", "检索结果", "候选", "最大种子", "最近检索"]

_STATE_COLOR = {
    "confirmed": "#1e8449",   # 确有
    "likely": "#b9770e",      # 疑似
    "none": "#7f8c8d",        # 未见
    "unknown": "#7f8c8d",     # 未查
    "todo": "#d35400",        # 还没查
}


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
        self._current_key: str = ""
        self._setup_ui()
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
        self.btn_lookup_new = QPushButton("检索未查")
        self.btn_lookup_new.setToolTip("只查还没有结果或结果已过期的番号")
        self.btn_lookup_new.setProperty("cssClass", "accent")
        self.btn_lookup_new.clicked.connect(lambda: self._lookup(force=False))
        toolbar.addWidget(self.btn_lookup_new)
        self.btn_lookup_all = QPushButton("全部重查")
        self.btn_lookup_all.setToolTip("忽略缓存，把所有番号重新查一遍")
        self.btn_lookup_all.clicked.connect(lambda: self._lookup(force=True))
        toolbar.addWidget(self.btn_lookup_all)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        toolbar.addWidget(self.btn_stop)
        self.btn_export = QPushButton("导出清单")
        self.btn_export.setToolTip("把番号与检索结果导出为 CSV，方便在别处反复检索")
        self.btn_export.clicked.connect(self._export)
        toolbar.addWidget(self.btn_export)
        layout.addLayout(toolbar)

        self.lbl_summary = QLabel("正在读取已确认有码的记录…")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setProperty("cssClass", "muted-label")
        layout.addWidget(self.lbl_summary)

        body = QHBoxLayout()
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
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
        body.addWidget(self.table, 3)

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
        body.addWidget(right, 2)
        layout.addLayout(body, 1)

    # ── 数据 ──
    def refresh(self) -> None:
        """从库里重建清单：已确认有码 → 按番号聚合。"""
        try:
            records = self._censor.store.confirmed_censored()
        except Exception as exc:  # noqa: BLE001 - 读库失败要能看见原因
            self.lbl_summary.setText(f"读取失败：{exc}")
            return
        grouped: dict[str, NumberRow] = {}
        for item in records:
            key = normalize_number(item.number) or "（无番号）"
            row = grouped.setdefault(key, NumberRow(number=item.number or ""))
            if not row.number and item.number:
                row.number = item.number
            row.paths.append(item.path)
        for row in grouped.values():
            cached = self._release.cached(row.number) if row.searchable else None
            row.report = cached
        self._rows = grouped
        self._fill_table()
        self._update_summary()

    def _fill_table(self) -> None:
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
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.UserRole, row.key)
                if col == 2:
                    item.setForeground(QColor(_STATE_COLOR.get(row.state_key, "#333")))
                self.table.setItem(index, col, item)
        self.table.setSortingEnabled(True)

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
    def _lookup(self, *, force: bool, numbers: Optional[list[str]] = None) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.lbl_summary.setText("检索进行中，先等它结束或点「停止」")
            return
        targets = numbers
        if targets is None:
            targets = [r.number for r in self._rows.values()
                       if r.searchable and (force or r.report is None)]
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
        worker.finished.connect(lambda: setattr(self, "_worker", None))
        self._worker = worker
        self.btn_stop.setEnabled(True)
        self.btn_lookup_new.setEnabled(False)
        self.btn_lookup_all.setEnabled(False)
        self.lbl_summary.setText(f"正在检索 {len(targets)} 个番号…")
        worker.start()

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
                cells = [row.state_text,
                         str(len(row.report.candidates)) if row.report else "—",
                         row.best_magnet_text,
                         self._queried_at(row)]
                for offset, text in enumerate(cells, start=2):
                    cell = self.table.item(index, offset)
                    if cell is not None:
                        cell.setText(text)
                return

    def _on_progress(self, done: int, total: int) -> None:
        self.lbl_summary.setText(f"正在检索 {done}/{total} 个番号…")

    def _on_done(self, count: int) -> None:
        self.btn_stop.setEnabled(False)
        self.btn_lookup_new.setEnabled(True)
        self.btn_lookup_all.setEnabled(True)
        self._fill_table()
        self._update_summary()
        self.lbl_summary.setText(self.lbl_summary.text() + f"（本次检索 {count} 个）")

    # ── 导出 ──
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
    def on_config_changed(self) -> None:
        self._release = ReleaseService(self.cfg)
        self.refresh()

    def shutdown(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(3000)
