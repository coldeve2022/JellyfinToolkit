"""Stable table identities and a single insert/update projection for review."""
from math import isfinite
import os

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QTableWidgetItem
from shiboken6 import isValid

from censor.fusion import explain
from censor.review_route import conclusion_label, bucket_label, bucket


class ScoreItem(QTableWidgetItem):
    def __lt__(self, other):
        return (self.data(Qt.UserRole + 2) or 0.) < (other.data(Qt.UserRole + 2) or 0.)


class ReviewTablePresenter:
    def __init__(self, page, state_colors, bucket_colors, evidence_grade):
        self.page = page
        self.table = page.table
        self.identities = {}
        self.state_colors = state_colors
        self.bucket_colors = bucket_colors
        self.evidence_grade = evidence_grade

    def row_for(self, path):
        item = self.identities.get(path)
        if item is not None and isValid(item) and item.tableWidget() is self.table:
            return item.row()
        self.identities.pop(path, None)
        return None

    def selected_paths(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedIndexes()})
        return [self.table.item(row, 0).data(Qt.UserRole) for row in rows
                if not self.table.isRowHidden(row) and self.table.item(row, 0)]

    def active_path(self):
        paths = self.selected_paths()
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        current = item.data(Qt.UserRole) if item else None
        return current if current in paths else (paths[0] if paths else None)

    def ordered_paths(self):
        return [self.table.item(row, 0).data(Qt.UserRole) for row in range(self.table.rowCount())
                if self.table.item(row, 0)]

    def present(self, result):
        table, page = self.table, self.page
        sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        try:
            with QSignalBlocker(table):
                row = self.row_for(result.path)
                if row is None:
                    row = table.rowCount()
                    table.insertRow(row)
                score = float(result.visual_score or 0.)
                if not isfinite(score):
                    score = 0.
                values = [os.path.basename(result.path), result.number or '—',
                    conclusion_label(result), bucket_label(result), f'{score:.1f}' if score else '—',
                    (result.error or '；'.join(text for _, text in explain(result.verdict))
                     or result.verdict.reason)[:120], page._record_source(result)]
                for column, text in enumerate(values):
                    item = table.item(row, column)
                    if item is None or (column == 4 and not isinstance(item, ScoreItem)):
                        item = ScoreItem() if column == 4 else QTableWidgetItem()
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        table.setItem(row, column, item)
                    item.setText(text)
                    if column == 0:
                        item.setData(Qt.UserRole, result.path)
                        item.setToolTip(result.path)
                        self.identities[result.path] = item
                    elif column == 2:
                        item.setForeground(QColor(self.state_colors.get(result.state, '#333')))
                        item.setToolTip('像素化结论与最后分析状态分别保存；人工确认不被失败重查抹去')
                    elif column == 3:
                        item.setForeground(QColor(self.bucket_colors.get(bucket(result), '#333')))
                        item.setToolTip(self.evidence_grade(result))
                    elif column == 4:
                        item.setData(Qt.UserRole + 2, score)
                page._set_library_cells(row, result.path)
                if page._batching:
                    table.setRowHidden(row, not page._matches_filter(result))
        finally:
            table.setSortingEnabled(sorting)
        if not page._batching:
            page._apply_filter()

    def apply_layout(self, name):
        visible = {'review': {0, 2, 3, 7, 8}, 'diagnostic': {0, 1, 2, 3, 4, 5, 6},
                   'all': set(range(self.table.columnCount()))}.get(name)
        if visible is None:
            raise ValueError('未知复核布局')
        for column in range(self.table.columnCount()):
            self.table.setColumnHidden(column, column not in visible)
        if name == 'diagnostic':
            self.table.setColumnWidth(5, 280)
        self.page.workspace.state['layout_preset'] = name
        self.page.workspace.save()
        self.page.workspace._sync_menus()

    def clear(self):
        self.identities.clear()
