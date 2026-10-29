"""筛选条件编辑器：自由组合任意条件，实时预览命中数。

设计取向
--------
不做"再多几个下拉"——下拉越多越互相打架（而且每次加维度都要改主页面）。
这里给一个条件表：字段 + 运算符 + 值，可以加任意条、切"满足全部/任一"，
还能存成预设。主页面只保留搜索框 + 「筛选（N）」按钮 + 「清除」。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from censor.release_filter import (FIELDS, OPS, FilterRule, fields_for_ui)

__all__ = ["ReleaseFilterDialog"]

_BOOL_OPS = ("is_true", "is_false")


class ReleaseFilterDialog(QDialog):
    """编辑 ``rules``。

    Args:
        rules: 初始条件
        logic: 'and' / 'or'
        presets: {名称: {"logic": str, "rules": [dict...]}}
        preview: ``preview(rules, logic) -> int``，返回命中条数
    """

    def __init__(self, parent=None, *, rules=(), logic="and", presets=None,
                 preview: Optional[Callable[[list, str], int]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("筛选条件")
        self.resize(860, 520)
        self._preview = preview
        self._presets = dict(presets or {})
        self._rows: list[tuple[QComboBox, QComboBox, QWidget]] = []

        outer = QVBoxLayout(self)
        head = QHBoxLayout()
        head.addWidget(QLabel("满足"))
        self.combo_logic = QComboBox()
        self.combo_logic.addItem("全部条件（而且）", "and")
        self.combo_logic.addItem("任一条件（或者）", "or")
        self.combo_logic.setCurrentIndex(0 if str(logic) != "or" else 1)
        self.combo_logic.currentIndexChanged.connect(self._refresh_preview)
        head.addWidget(self.combo_logic)
        head.addStretch()
        self.lbl_preview = QLabel("")
        self.lbl_preview.setProperty("cssClass", "muted-label")
        head.addWidget(self.lbl_preview)
        outer.addLayout(head)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["字段", "运算符", "值"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(1, 150)
        outer.addWidget(self.table, 1)

        tools = QHBoxLayout()
        add = QPushButton("添加条件")
        add.clicked.connect(lambda: self._add_row(FilterRule("number", "contains", "")))
        tools.addWidget(add)
        remove = QPushButton("删除选中")
        remove.clicked.connect(self._remove_selected)
        tools.addWidget(remove)
        clear = QPushButton("清空")
        clear.clicked.connect(self._clear)
        tools.addWidget(clear)
        tools.addStretch()

        tools.addWidget(QLabel("预设"))
        self.combo_preset = QComboBox()
        self.combo_preset.addItem("（不加载）", "")
        for name in sorted(self._presets):
            self.combo_preset.addItem(name, name)
        tools.addWidget(self.combo_preset)
        load = QPushButton("加载")
        load.clicked.connect(self._load_preset)
        tools.addWidget(load)
        save = QPushButton("保存为预设…")
        save.clicked.connect(self._save_preset)
        tools.addWidget(save)
        drop = QPushButton("删除预设")
        drop.clicked.connect(self._drop_preset)
        tools.addWidget(drop)
        outer.addLayout(tools)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        for rule in rules or ():
            self._add_row(rule)
        self._refresh_preview()

    # ── 行编辑 ─────────────────────────────────────────────
    def _add_row(self, rule: FilterRule) -> None:
        index = self.table.rowCount()
        self.table.insertRow(index)

        field = QComboBox()
        for key, label, _ops, _choices, _unit in fields_for_ui():
            field.addItem(label, key)
        field.setCurrentIndex(max(0, field.findData(rule.field or "number")))
        field.currentIndexChanged.connect(lambda _i, row=index: self._rebuild_row(row))
        self.table.setCellWidget(index, 0, field)

        op = QComboBox()
        self.table.setCellWidget(index, 1, op)

        holder = QWidget()
        QHBoxLayout(holder).setContentsMargins(0, 0, 0, 0)
        self.table.setCellWidget(index, 2, holder)

        self._rows.append((field, op, holder))
        self._rebuild_row(index, op_value=rule.op, value=rule.value)

    def _rebuild_row(self, index: int, *, op_value: str = "", value: str = "") -> None:
        if not (0 <= index < len(self._rows)):
            return
        field, op, holder = self._rows[index]
        spec = FIELDS.get(field.currentData() or "number")
        if spec is None:
            return

        op.blockSignals(True)
        op.clear()
        for key in spec.ops:
            op.addItem(OPS.get(key, key), key)
        op.setCurrentIndex(max(0, op.findData(op_value or spec.ops[0])))
        op.blockSignals(False)
        op.currentIndexChanged.connect(lambda _i, row=index: self._rebuild_value(row))

        self._rebuild_value(index, value=value)
        self._refresh_preview()

    def _rebuild_value(self, index: int, *, value: str = "") -> None:
        if not (0 <= index < len(self._rows)):
            return
        field, op, holder = self._rows[index]
        spec = FIELDS.get(field.currentData() or "number")
        if spec is None:
            return
        layout = holder.layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        op_key = op.currentData() or (spec.ops[0] if spec.ops else "")
        if op_key in _BOOL_OPS:
            hint = QLabel("（不需要填值）")
            hint.setProperty("cssClass", "muted-label")
            layout.addWidget(hint)
        elif spec.kind == "enum":
            combo = QComboBox()
            for key, label in spec.choices:
                combo.addItem(label, key)
            index_of = combo.findData(value) if value else 0
            combo.setCurrentIndex(max(0, index_of))
            combo.currentIndexChanged.connect(self._refresh_preview)
            layout.addWidget(combo)
        else:
            edit = QLineEdit()
            edit.setPlaceholderText(spec.unit or "值")
            edit.setText(value)
            edit.textChanged.connect(self._refresh_preview)
            layout.addWidget(edit)
        layout.addStretch()
        self._refresh_preview()

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedIndexes()},
                      reverse=True)
        for row in rows:
            self.table.removeRow(row)
            if 0 <= row < len(self._rows):
                self._rows.pop(row)
        self._refresh_preview()

    def _clear(self) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        self._refresh_preview()

    # ── 取值 ───────────────────────────────────────────────
    def _value_of(self, index: int) -> str:
        _field, _op, holder = self._rows[index]
        layout = holder.layout()
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if isinstance(widget, QComboBox):
                return str(widget.currentData() or "")
            if isinstance(widget, QLineEdit):
                return widget.text().strip()
        return ""

    def rules(self) -> list[FilterRule]:
        out = []
        for index, (field, op, _holder) in enumerate(self._rows):
            key = field.currentData()
            if not key:
                continue
            out.append(FilterRule(str(key), str(op.currentData() or ""), self._value_of(index)))
        return out

    def logic(self) -> str:
        return str(self.combo_logic.currentData() or "and")

    # ── 预览与预设 ─────────────────────────────────────────
    def _refresh_preview(self) -> None:
        if not callable(self._preview):
            return
        try:
            count = int(self._preview(self.rules(), self.logic()))
        except Exception:  # noqa: BLE001 - 预览失败不该挡住编辑
            return
        self.lbl_preview.setText(f"当前条件匹配 {count} 条")

    def _load_preset(self) -> None:
        name = str(self.combo_preset.currentData() or "")
        data = self._presets.get(name)
        if not data:
            return
        self.table.setRowCount(0)
        self._rows.clear()
        index = self.combo_logic.findData(str(data.get("logic", "and")))
        self.combo_logic.setCurrentIndex(max(0, index))
        for item in data.get("rules") or ():
            self._add_row(FilterRule.from_dict(item))
        self._refresh_preview()

    def _save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "保存筛选预设", "预设名称：")
        name = (name or "").strip()
        if not ok or not name:
            return
        self._presets[name] = {"logic": self.logic(),
                               "rules": [rule.as_dict() for rule in self.rules()]}
        if self.combo_preset.findData(name) < 0:
            self.combo_preset.addItem(name, name)
        self.combo_preset.setCurrentIndex(self.combo_preset.findData(name))

    def _drop_preset(self) -> None:
        name = str(self.combo_preset.currentData() or "")
        if not name or name not in self._presets:
            return
        self._presets.pop(name, None)
        index = self.combo_preset.findData(name)
        if index >= 0:
            self.combo_preset.removeItem(index)

    def presets(self) -> dict:
        """保存后交回页面（页面负责落盘到工作区偏好）。"""
        return dict(self._presets)
