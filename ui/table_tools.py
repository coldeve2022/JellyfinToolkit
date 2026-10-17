"""Shared text filtering and numeric sorting without losing action identities."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel, QTableWidgetItem


class PathNumberItem(QTableWidgetItem):
    """UserRole remains the action path; numeric comparison has its own role."""
    def __init__(self, text, number, path):
        super().__init__(str(text))
        self.setData(Qt.UserRole, path)
        self.setData(Qt.UserRole + 3, float(number or 0))

    def __lt__(self, other):
        return float(self.data(Qt.UserRole + 3) or 0) < float(other.data(Qt.UserRole + 3) or 0)


class TableSearch(QWidget):
    def __init__(self, tables, parent=None):
        super().__init__(parent)
        self.tables = list(tables)
        self._queued = False
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.input = QLineEdit()
        self.input.setPlaceholderText("搜索当前表格：名称、标签、理由或路径…")
        self.input.setClearButtonEnabled(True)
        self.count = QLabel()
        row.addWidget(self.input, 1)
        row.addWidget(self.count)
        self.input.textChanged.connect(self.apply)
        for table in self.tables:
            table.model().rowsInserted.connect(self.schedule)
            table.model().rowsRemoved.connect(self.schedule)
            table.model().modelReset.connect(self.schedule)
            table.model().dataChanged.connect(self.schedule)

    def schedule(self, *args):
        if not self._queued:
            self._queued = True
            QTimer.singleShot(0, self, self.apply)

    def apply(self, *args):
        self._queued = False
        words = self.input.text().casefold().split()
        visible = total = 0
        for table in self.tables:
            for row in range(table.rowCount()):
                values = []
                for column in range(table.columnCount()):
                    item = table.item(row, column)
                    if item:
                        values += [item.text(), item.toolTip()]
                text = " ".join(values).casefold()
                show = all(word in text for word in words)
                table.setRowHidden(row, not show)
                if not show:
                    for column in range(table.columnCount()):
                        item = table.item(row, column)
                        if item:
                            item.setSelected(False)
                total += 1
                visible += int(show)
        self.count.setText(f"显示 {visible} / {total} 行")
