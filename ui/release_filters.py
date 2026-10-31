"""Embedded condition editor and anchored Excel-style column value menus."""
from PySide6.QtCore import Qt, Signal, QPoint, QRect
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QComboBox, QLineEdit,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QStackedWidget, QMenu, QWidgetAction,
    QHeaderView, QStyle, QStyleOptionHeader)

from censor.release_filter import FIELDS, OPS, FilterRule, describe, validate_rule


class FilterHeader(QHeaderView):
    filter_requested = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._filter_pressed = False

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        col = self.logicalIndexAt(pos)
        edge = self.sectionViewportPosition(col) + self.sectionSize(col) if col >= 0 else -1
        self._filter_pressed = event.button() == Qt.LeftButton and col >= 0 and pos.x() >= edge - 22
        if self._filter_pressed:
            event.accept()
            self.filter_requested.emit(pos)
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._filter_pressed:
            self._filter_pressed = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def paintSection(self, painter, rect, logical_index):
        content = QRect(rect)
        content.setWidth(max(0, rect.width() - 22))
        painter.save()
        super().paintSection(painter, content, logical_index)
        painter.restore()
        option = QStyleOptionHeader()
        self.initStyleOption(option)
        option.rect = QRect(rect.right() - 21, rect.top(), 22, rect.height())
        option.text = '▾'
        option.textAlignment = Qt.AlignCenter
        option.sortIndicator = QStyleOptionHeader.None_
        painter.save()
        painter.setClipRect(rect)
        self.style().drawControl(QStyle.CE_HeaderSection, option, painter, self)
        option.rect = QRect(rect.right() - 16, rect.center().y() - 4, 10, 10)
        self.style().drawPrimitive(QStyle.PE_IndicatorArrowDown, option, painter, self)
        painter.restore()


class ReleaseFilterBar(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []
        self._editing = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel('条件'))
        self.logic = QComboBox()
        self.logic.addItem('全部满足（AND）', 'and')
        self.logic.addItem('任一满足（OR）', 'or')
        self.logic.currentIndexChanged.connect(self.changed)
        row.addWidget(self.logic)
        self.field = QComboBox()
        for key, spec in FIELDS.items():
            self.field.addItem(spec.label, key)
        row.addWidget(self.field)
        self.op = QComboBox()
        row.addWidget(self.op)
        self.value = QLineEdit()
        self.choice = QComboBox()
        self.stack = QStackedWidget()
        self.stack.addWidget(self.value)
        self.stack.addWidget(self.choice)
        self.stack.addWidget(QLabel('无需填写值'))
        row.addWidget(self.stack, 1)
        self.add = QPushButton('添加条件')
        self.add.clicked.connect(self._apply)
        self.value.returnPressed.connect(self._apply)
        row.addWidget(self.add)
        self.cancel = QPushButton('取消编辑')
        self.cancel.clicked.connect(self._cancel)
        self.cancel.hide()
        row.addWidget(self.cancel)
        layout.addLayout(row)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty('cssClass', 'muted-label')
        self.error.hide()
        layout.addWidget(self.error)
        active_row = QHBoxLayout()
        self.active = QListWidget()
        self.active.itemClicked.connect(self._edit)
        active_row.addWidget(self.active, 1)
        self.remove = QPushButton('删除选中条件')
        self.remove.clicked.connect(self._remove)
        active_row.addWidget(self.remove, 0, Qt.AlignTop)
        layout.addLayout(active_row)
        self.field.currentIndexChanged.connect(self._field_changed)
        self.op.currentIndexChanged.connect(self._operator_changed)
        self._field_changed()
        self._render()
        # These stay editable while a lookup task owns the page.
        for control in (self, self.logic, self.field, self.op, self.value, self.choice,
                        self.add, self.cancel, self.remove, self.active):
            control.setProperty('taskInteractive', True)

    def _field_changed(self):
        spec = FIELDS[self.field.currentData()]
        self.op.blockSignals(True)
        self.op.clear()
        for key in spec.ops:
            self.op.addItem(OPS[key], key)
        self.op.blockSignals(False)
        self.choice.clear()
        for value, label in spec.choices:
            self.choice.addItem(label, value)
        self.value.setPlaceholderText(spec.unit or '填写筛选值')
        self._operator_changed()

    def _operator_changed(self):
        spec = FIELDS[self.field.currentData()]
        no_value = self.op.currentData() in ('is_true', 'is_false', 'never')
        self.stack.setCurrentIndex(2 if no_value else 1 if spec.kind == 'enum' else 0)

    def rules(self):
        return list(self._rules)

    def logic_key(self):
        return self.logic.currentData()

    def set_rules(self, rules, logic='and'):
        self._rules = []
        invalid = []
        for rule in rules:
            try:
                validate_rule(rule)
            except ValueError as exc:
                invalid.append(f'{describe(rule)}：{exc}')
            else:
                self._rules.append(rule)
        self.logic.blockSignals(True)
        self.logic.setCurrentIndex(max(0, self.logic.findData(logic)))
        self.logic.blockSignals(False)
        self._cancel()
        self._render()
        self._set_error('旧条件未启用，请重新填写：' + '；'.join(invalid) if invalid else '')

    def _set_error(self, message):
        self.error.setText(message)
        self.error.setVisible(bool(message))

    def select_field(self, key):
        index = self.field.findData(key)
        if index >= 0:
            self.field.setCurrentIndex(index)
        self.value.setFocus()

    def _apply(self):
        rule = FilterRule(self.field.currentData(), self.op.currentData(),
                          str(self.choice.currentData() or '') if self.stack.currentIndex() == 1
                          else self.value.text().strip())
        try:
            validate_rule(rule)
        except ValueError as exc:
            self._set_error(str(exc))
            return
        if self._editing is None:
            self._rules.append(rule)
        else:
            self._rules[self._editing] = rule
        self._cancel()
        self._set_error('')
        self._render()
        self.changed.emit()

    def _edit(self, item):
        index = self.active.row(item)
        rule = self._rules[index]
        self._editing = index
        self.select_field(rule.field)
        self.op.setCurrentIndex(self.op.findData(rule.op))
        self.value.setText(rule.value)
        self.choice.setCurrentIndex(max(0, self.choice.findData(rule.value)))
        self.add.setText('更新条件')
        self.cancel.show()

    def _cancel(self):
        self._editing = None
        self.add.setText('添加条件')
        self.cancel.hide()

    def _remove(self):
        index = self.active.currentRow()
        if index < 0:
            return
        self._rules.pop(index)
        self._cancel()
        self._render()
        self.changed.emit()

    def _render(self):
        self.active.clear()
        self.active.addItems([describe(rule) + '  （点击编辑）' for rule in self._rules])
        self.active.setFixedHeight(min(94, max(32, len(self._rules) * 30 + 4)))
        self.active.setVisible(bool(self._rules))
        self.remove.setVisible(bool(self._rules))


class ColumnFilterMenu(QMenu):
    """Only Apply publishes choices; dismissing the menu leaves filters alone."""
    applied = Signal(object)

    def __init__(self, title, counts, selected=None, labels=None, parent=None):
        super().__init__(parent)
        labels = labels or {}
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel(title + ' · 同列多选取并集'))
        search = QLineEdit()
        search.setPlaceholderText('搜索本列取值…')
        layout.addWidget(search)
        self.values = QListWidget()
        self.values.setMinimumSize(260, 190)
        for key in sorted(set(counts) | set(selected or ())):
            item = QListWidgetItem(f'{labels.get(key, key) or "（空白）"}  ({counts.get(key, 0)})')
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if selected is None or key in selected else Qt.Unchecked)
            self.values.addItem(item)
        search.textChanged.connect(lambda text: [self.values.item(i).setHidden(
            text.casefold() not in self.values.item(i).text().casefold()) for i in range(self.values.count())])
        layout.addWidget(self.values)
        row = QHBoxLayout()
        for label, checked in (('全选', True), ('全不选', False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, yes=checked: self._check_all(yes))
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        apply = QPushButton('应用')
        apply.clicked.connect(self._apply)
        row.addWidget(apply)
        clear = QPushButton('清除此列筛选')
        clear.clicked.connect(self._clear)
        row.addWidget(clear)
        layout.addLayout(row)
        action = QWidgetAction(self)
        action.setDefaultWidget(box)
        self.addAction(action)

    def _check_all(self, checked):
        for i in range(self.values.count()):
            item = self.values.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _apply(self):
        selected = {self.values.item(i).data(Qt.UserRole) for i in range(self.values.count())
                    if self.values.item(i).checkState() == Qt.Checked}
        self.applied.emit(selected)
        self.close()

    def _clear(self):
        self.applied.emit(None)
        self.close()
