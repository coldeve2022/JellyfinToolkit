"""Shared release settings editor. Edits have no side effects until apply."""
from copy import deepcopy

from PySide6.QtWidgets import (QWidget, QFormLayout, QVBoxLayout, QHBoxLayout, QCheckBox,
                              QLineEdit, QSpinBox, QDoubleSpinBox, QPushButton, QPlainTextEdit,
                              QDialog, QDialogButtonBox, QScrollArea, QMessageBox, QComboBox)

from ui.services import events, start_task


class ReleaseSettings(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.enabled = QCheckBox("启用版本检索")
        self.enabled.setChecked(cfg.release_enabled)
        self.auto = QCheckBox("人工确认有码后自动检索")
        self.auto.setChecked(cfg.release_auto_lookup)
        form.addRow(self.enabled)
        form.addRow(self.auto)
        self.sources = {}
        row = QHBoxLayout()
        for name in ("javdb", "javbus", "supjav"):
            control = QCheckBox(name)
            control.setChecked(name in cfg.release_sources)
            self.sources[name] = control
            row.addWidget(control)
        form.addRow("检索来源", row)
        self.engines = {}
        row = QHBoxLayout()
        for name in ("bing", "duckduckgo"):
            control = QCheckBox(name)
            control.setChecked(name in cfg.release_supjav_engines)
            self.engines[name] = control
            row.addWidget(control)
        form.addRow("Supjav 索引引擎", row)
        self.supjav_mode = QComboBox()
        for label, value in (('站内优先，索引补充', 'auto'), ('只查站内', 'direct'), ('只查索引（兼容）', 'index')):
            self.supjav_mode.addItem(label, value)
        self.supjav_mode.setCurrentIndex(max(0, self.supjav_mode.findData(cfg.release_supjav_mode)))
        form.addRow('Supjav 检索方式', self.supjav_mode)
        self.browser_button = QPushButton('连接 / 验证 Supjav 浏览器会话…')
        self.browser_button.clicked.connect(self._open_supjav)
        form.addRow(self.browser_button)
        self.proxy = QLineEdit(cfg.release_proxy)
        self.proxy.setPlaceholderText("留空自动检测；例如 http://127.0.0.1:7897")
        self.bypass = QLineEdit(cfg.release_cf_bypass_url)
        self.bypass.setPlaceholderText("可选的 FlareSolverr 兼容地址")
        form.addRow("代理", self.proxy)
        form.addRow("Cloudflare 旁路", self.bypass)
        self.numbers = {}
        for key, label, low, high, floating in (
            ("release_timeout", "单请求超时（秒）", 3, 300, True),
            ("release_bypass_timeout", "旁路超时（秒）", 5, 300, True),
            ("release_retries", "失败重试次数", 0, 5, False),
            ("release_min_interval", "同站最小间隔（秒）", 0, 60, True),
            ("release_max_requests", "每作品请求预算", 1, 400, False),
            ("release_source_workers", "同时查询来源数（同站仍限速）", 1, 3, False),
            ("release_detail_limit", "每来源详情上限", 1, 20, False),
            ("release_cache_days", "缓存天数（0 永不过期）", 0, 3650, False),
        ):
            control = QDoubleSpinBox() if floating else QSpinBox()
            control.setRange(low, high)
            control.setValue(getattr(cfg, key))
            self.numbers[key] = control
            form.addRow(label, control)
        layout.addLayout(form)
        self.btn_test = QPushButton("测试当前填写的连接（不保存）")
        self.btn_test.clicked.connect(self._test)
        layout.addWidget(self.btn_test)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumHeight(130)
        layout.addWidget(self.output)
        self._check = None
        self._loaded_values = self.values()
        events.config_changed.connect(self.reload)

    def reload(self):
        """Refresh untouched fields; a deliberate local draft remains editable."""
        current = self.values()
        for name, old in self._loaded_values.items():
            new = getattr(self._cfg, name)
            if current[name] != old:
                continue
            if name == 'release_enabled':
                self.enabled.setChecked(new)
            elif name == 'release_auto_lookup':
                self.auto.setChecked(new)
            elif name in ('release_sources', 'release_supjav_engines'):
                controls = self.sources if name == 'release_sources' else self.engines
                for key, control in controls.items():
                    control.setChecked(key in new)
            elif name == 'release_proxy':
                self.proxy.setText(new)
            elif name == 'release_cf_bypass_url':
                self.bypass.setText(new)
            elif name == 'release_supjav_mode':
                self.supjav_mode.setCurrentIndex(max(0, self.supjav_mode.findData(new)))
            else:
                self.numbers[name].setValue(new)
        self._loaded_values = {name: deepcopy(getattr(self._cfg, name)) for name in current}

    def validated_values(self):
        values = self.values()
        if values['release_enabled'] and not values['release_sources']:
            raise ValueError('启用检索时至少选择一个来源')
        if (values['release_enabled'] and 'supjav' in values['release_sources']
                and values['release_supjav_mode'] != 'direct' and not values['release_supjav_engines']):
            raise ValueError('启用 Supjav 时至少选择一个索引引擎')
        return values

    def values(self):
        return dict(release_enabled=self.enabled.isChecked(), release_auto_lookup=self.auto.isChecked(),
                    release_sources=[n for n, c in self.sources.items() if c.isChecked()],
                    release_supjav_engines=[n for n, c in self.engines.items() if c.isChecked()],
                    release_supjav_mode=self.supjav_mode.currentData(),
                    release_proxy=self.proxy.text().strip(), release_cf_bypass_url=self.bypass.text().strip(),
                    **{key: control.value() for key, control in self.numbers.items()})

    def apply(self, cfg):
        draft = deepcopy(cfg)
        values = self.validated_values()
        for name, value in values.items():
            setattr(draft, name, value)
        if draft.release_enabled and not draft.release_sources:
            raise ValueError("启用检索时至少选择一个来源")
        draft.save()
        for name, value in values.items():
            setattr(cfg, name, value)
        self._loaded_values = deepcopy(values)
        events.config_changed.emit()

    def _open_supjav(self):
        from ui.supjav_browser import open_supjav_session
        open_supjav_session(self)

    def _test(self):
        from ui.censor_release import ConnectionCheck
        if self._check and self._check.isRunning():
            return
        draft = deepcopy(self._cfg)
        for name, value in self.values().items():
            setattr(draft, name, value)
        self.output.clear()
        self._check = ConnectionCheck(draft, self)
        self._check.line.connect(self.output.appendPlainText)
        self._check.passed.connect(lambda ok, total: self.output.appendPlainText(f"完成：{ok}/{total} 项通过"))
        start_task(self, self._check, "测试版本检索连接")


def edit_release_settings(cfg, parent=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle("版本检索设置")
    dialog.resize(580, 720)
    layout = QVBoxLayout(dialog)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    editor = ReleaseSettings(cfg, dialog)
    scroll.setWidget(editor)
    layout.addWidget(scroll)
    buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    layout.addWidget(buttons)
    def save():
        try:
            editor.apply(cfg)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(dialog, "设置未保存", str(exc))
            return
        dialog.accept()
    buttons.accepted.connect(save)
    buttons.rejected.connect(dialog.reject)
    result = dialog.exec()
    if editor._check and editor._check.isRunning():
        editor._check.stop()
        # Keep its owner alive until the bounded active network request returns.
        editor._check.finished.connect(dialog.deleteLater)
    else:
        dialog.deleteLater()
    return result
