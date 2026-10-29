"""Persist presentation choices separately from analysis configuration."""
from copy import deepcopy
import base64
import binascii
import json

from PySide6.QtCore import QObject, QTimer, QEvent, Qt, QByteArray, QSignalBlocker
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QMenu, QDialog, QFormLayout, QSpinBox, QCheckBox,
                              QDialogButtonBox, QVBoxLayout, QKeySequenceEdit)


class WorkspacePreferences(QObject):
    def __init__(self, owner, key, table, *, splitter=None, gallery=None, filters=None):
        super().__init__(owner)
        import config
        self.path = config.DATA_DIR / "workspace_preferences.json"
        self.owner, self.key, self.table = owner, key, table
        self.splitter, self.gallery, self.filters = splitter, gallery, filters or {}
        self._applying = False
        self._menus = []
        self._default_header = bytes(table.horizontalHeader().saveState())
        self._defaults = dict(font_size=owner.font().pointSize() or 10, row_height=28,
            auto_next=True, remember_view=False, tile_width=186, columns=0,
            vertical=bool(splitter and splitter.orientation() == Qt.Vertical),
            filters={name: control.currentData() if hasattr(control, 'currentData') else control.text()
                     for name, control in self.filters.items()},
            shortcuts={"censored": "Alt+1", "uncensored": "Alt+2", "next": "Alt+3"})
        if splitter:
            self._defaults['splitter'] = base64.b64encode(bytes(splitter.saveState())).decode()
        self.state = deepcopy(self._defaults)
        saved = self._read().get(key, {})
        if isinstance(saved, dict):
            self.state.update(saved)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.save)
        self.restore()
        header = table.horizontalHeader()
        header.sectionMoved.connect(self.schedule)
        header.sectionResized.connect(self.schedule)
        header.sortIndicatorChanged.connect(self.schedule)
        table.model().rowsInserted.connect(self._rows_added)
        if splitter:
            splitter.splitterMoved.connect(self.schedule)
        for control in self.filters.values():
            signal = getattr(control, 'currentIndexChanged', None)
            if signal is None:
                signal = getattr(control, 'textChanged', None)
            if signal is not None:
                signal.connect(self.schedule)
        owner.installEventFilter(self)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def schedule(self, *args):
        if not self._applying:
            self._timer.start()

    def _rows_added(self, *args):
        self.table.verticalHeader().setDefaultSectionSize(int(self.state['row_height']))

    def save(self):
        if self._applying:
            return
        try:
            self.state['header'] = base64.b64encode(bytes(self.table.horizontalHeader().saveState())).decode()
            if self.splitter:
                self.state['splitter'] = base64.b64encode(bytes(self.splitter.saveState())).decode()
                self.state['vertical'] = self.splitter.orientation() == Qt.Vertical
            self.state['filters'] = {name: control.currentData() if hasattr(control, 'currentData')
                                    else control.text() for name, control in self.filters.items()}
            data = self._read()
            data[self.key] = self.state
            from automation.plans import write_json_atomic
            write_json_atomic(self.path, data)
        except (OSError, RuntimeError, ValueError, TypeError):
            return  # Preferences cannot interrupt a media operation or shutdown.

    def restore(self):
        self._applying = True
        try:
            for key, low, high in (("font_size", 8, 24), ("row_height", 20, 64),
                                   ("tile_width", 128, 400), ("columns", 0, 6)):
                try:
                    self.state[key] = max(low, min(high, int(self.state[key])))
                except (ValueError, TypeError):
                    self.state[key] = self._defaults[key]
            if self.state.get('header'):
                self._restore_layout('header', self.table.horizontalHeader().restoreState)
            # The identity column cannot be enabled through its disabled menu action.
            self.table.setColumnHidden(0, False)
            if self.splitter:
                self.splitter.setOrientation(Qt.Vertical if self.state.get('vertical') else Qt.Horizontal)
                if self.state.get('splitter'):
                    self._restore_layout('splitter', self.splitter.restoreState)
            font = QFont(self.owner.font())
            font.setPointSize(self.state['font_size'])
            self.owner.setFont(font)
            self.table.setFont(font)
            self._rows_added()
            if self.gallery:
                self.gallery.set_preferences(self.state['tile_width'], self.state['columns'])
            filters = self.state.get('filters')
            if isinstance(filters, dict):
                for name, control in self.filters.items():
                    if name not in filters or (name == 'view' and self.state.get('remember_view') is not True):
                        # View memory is explicit. Without it, retain this
                        # page's default view; other saved filters still apply.
                        continue
                    if hasattr(control, 'findData'):
                        control.setCurrentIndex(max(0, control.findData(filters[name])))
                    else:
                        control.setText(str(filters[name]))
            shortcuts = self.state.get('shortcuts')
            if not isinstance(shortcuts, dict):
                shortcuts = {}
            self.state['shortcuts'] = {
                name: shortcuts[name] if isinstance(shortcuts.get(name), str) else default
                for name, default in self._defaults['shortcuts'].items()}
            for name, attr in (("censored", "btn_censored"), ("uncensored", "btn_uncensored"),
                               ("next", "btn_next")):
                button = getattr(self.owner, attr, None)
                if button is not None:
                    setter = getattr(self.owner, 'set_review_shortcut', None)
                    if callable(setter):
                        setter(name, self.state['shortcuts'][name])
                    else:
                        button.setShortcut(QKeySequence(self.state['shortcuts'][name]))
        except (ValueError, TypeError):
            pass
        finally:
            self._applying = False
        self._sync_menus()

    def _restore_layout(self, key, restore):
        """A damaged layout must not prevent independent preferences restoring."""
        try:
            value = self.state[key]
            if not isinstance(value, str):
                raise TypeError('Layout state must be base64 text')
            if restore(QByteArray(base64.b64decode(value, validate=True))):
                return
        except (ValueError, TypeError, binascii.Error):
            pass
        self.state.pop(key, None)

    def reset(self):
        self.state = deepcopy(self._defaults)
        self.table.horizontalHeader().restoreState(QByteArray(self._default_header))
        self.restore()
        self.save()

    def menu(self, parent=None):
        menu = QMenu(parent)
        columns = menu.addMenu('显示列')
        column_actions = []
        for column in range(self.table.columnCount()):
            item = self.table.horizontalHeaderItem(column)
            action = columns.addAction(item.text() if item else str(column))
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(column))
            action.setEnabled(column != 0)
            action.toggled.connect(lambda checked, col=column: self._column(col, checked))
            column_actions.append(action)
        orientation = None
        if self.splitter:
            action = menu.addAction('上下分栏')
            action.setCheckable(True)
            action.setChecked(self.splitter.orientation() == Qt.Vertical)
            action.toggled.connect(self._orientation)
            orientation = action
        menu.addAction('字体、密度与复核偏好…', self.customize)
        menu.addAction('恢复默认布局与偏好', self.reset)
        self._menus.append((menu, columns, column_actions, orientation))
        return menu

    def _sync_menus(self):
        from shiboken6 import isValid
        self._menus = [entry for entry in self._menus if isValid(entry[0])]
        for _, _, actions, orientation in self._menus:
            for column, action in enumerate(actions):
                with QSignalBlocker(action):
                    action.setChecked(not self.table.isColumnHidden(column))
            if orientation is not None:
                with QSignalBlocker(orientation):
                    orientation.setChecked(self.splitter.orientation() == Qt.Vertical)

    def _column(self, column, visible):
        # Keep at least the identity column visible and reachable.
        if column == 0 and not visible:
            return
        self.table.setColumnHidden(column, not visible)
        self.save()
        self._sync_menus()

    def _orientation(self, vertical):
        self.splitter.setOrientation(Qt.Vertical if vertical else Qt.Horizontal)
        self.save()
        self._sync_menus()

    def customize(self):
        dialog = QDialog(self.owner)
        dialog.setWindowTitle('工作区偏好')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        controls = {}
        for key, label, low, high in (("font_size", "字体大小", 8, 24),
                ("row_height", "表格行高", 20, 64), ("tile_width", "证据图宽度", 128, 400),
                ("columns", "画廊列数（0 自适应）", 0, 6)):
            control = QSpinBox()
            control.setRange(low, high)
            control.setValue(self.state[key])
            controls[key] = control
            form.addRow(label, control)
        auto = QCheckBox('标记后自动进入下一条')
        auto.setChecked(bool(self.state.get('auto_next', True)))
        form.addRow(auto)
        keys = {}
        for name, label in (("censored", "标记有码"), ("uncensored", "标记无码"), ("next", "下一条")):
            control = QKeySequenceEdit(QKeySequence(self.state.get('shortcuts', {}).get(name, '')))
            form.addRow(label + '快捷键', control)
            keys[name] = control
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            self.state.update({key: control.value() for key, control in controls.items()})
            self.state['auto_next'] = auto.isChecked()
            self.state['shortcuts'] = {key: control.keySequence().toString() for key, control in keys.items()}
            self.restore()
            self.save()
        dialog.deleteLater()

    def eventFilter(self, watched, event):
        # Qt can deliver a late owner event while cyclic Python wrappers are
        # being cleared. There is no preference state to save at that point.
        owner = getattr(self, 'owner', None)
        if owner is None:
            return False
        if watched is owner and event.type() in (QEvent.Close, QEvent.Hide):
            self.save()
        return super().eventFilter(watched, event)
