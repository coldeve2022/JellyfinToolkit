"""One visible library scope for collection, feedback, profiles and plans."""
from __future__ import annotations

import copy
import hashlib
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QDialog,
    QListWidget, QListWidgetItem, QPlainTextEdit, QRadioButton,
    QDialogButtonBox,
)

from ui.services import CallWorker, TaskManager, events, start_task, busy


def selection_source(cfg):
    """Connection binding contains no credentials and needs no server access."""
    values = [getattr(cfg, key, "") for key in (
        "library_source", "jellyfin_server", "jellyfin_db_path", "server_type")]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class ScopeBar(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._dialog = None
        self._worker = None
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        self.choose_button = QPushButton("选择媒体库 / 目录…")
        self.choose_button.clicked.connect(self.open_selector)
        row.addWidget(self.choose_button)
        events.config_changed.connect(self.refresh)
        TaskManager.instance().changed.connect(self._tasks_changed)
        self.refresh()

    def selection_ready(self):
        cfg = self.cfg
        bound = getattr(cfg, "library_scope_source", "")
        return bool(
            (getattr(cfg, "library_scope_confirmed", False)
             or getattr(cfg, "library_scope_ids", []) or getattr(cfg, "library_scope_paths", []))
            and (not bound or bound == selection_source(cfg))
        )

    def _tasks_running(self):
        if TaskManager.instance().running():
            return True
        return any(busy(page) for page in getattr(self.window(), "_pages", []))

    def _tasks_changed(self):
        self.choose_button.setEnabled(not self._tasks_running())

    def refresh(self):
        if not self.selection_ready():
            text = "库分析范围：未选择。请先选择电影、动漫等媒体库或目录，再开始分析。"
        else:
            selected = set(getattr(self.cfg, "library_scope_ids", []))
            catalog = {row["id"]: row["name"] for row in getattr(self.cfg, "library_scope_catalog", [])
                       if isinstance(row, dict) and "id" in row and "name" in row}
            names = [catalog.get(key, "未解析的媒体库") for key in sorted(selected)]
            names += list(getattr(self.cfg, "library_scope_paths", []))
            text = "库分析范围：" + ("；".join(names) if names else "全部媒体库（已明确选择，会合并分析）")
        self.summary.setText(text)
        self.summary.setToolTip(text + "\n影响概览、画像、打标、删除建议、从库挑选和自动化；手动添加文件按其输入处理。")
        self._tasks_changed()

    def apply_selection(self, catalog, ids, paths, *, all_libraries=False):
        if self._tasks_running():
            self.summary.setText("任务正在进行；请等待结束或停止后再切换分析范围。")
            return False
        from utils.library_scope import normalize_scope_path

        try:
            raw_paths = [str(path).strip() for path in paths if str(path).strip()]
            paths = sorted({normalize_scope_path(path) for path in raw_paths})
            if raw_paths and "" in paths:
                raise ValueError("目录必须是完整源路径，且不能包含上级目录跳转。")
            ids = sorted(set(ids))
            if not ids and not paths and not all_libraries:
                raise ValueError("请至少选择一个媒体库或填写一个目录；分析全部库需明确勾选。")
            fields = ("library_scope_ids", "library_scope_paths", "library_scope_confirmed",
                      "library_scope_catalog", "library_scope_source")
            old = {key: copy.deepcopy(getattr(self.cfg, key, None)) for key in fields}
            try:
                self.cfg.library_scope_ids = [] if all_libraries else ids
                self.cfg.library_scope_paths = [] if all_libraries else paths
                self.cfg.library_scope_confirmed = True
                self.cfg.library_scope_catalog = copy.deepcopy(catalog)
                self.cfg.library_scope_source = selection_source(self.cfg)
                self.cfg.save()
            except Exception:
                for key, value in old.items():
                    setattr(self.cfg, key, value)
                raise
        except (OSError, ValueError, TypeError) as exc:
            self.summary.setText("范围未保存：" + str(exc))
            return False
        events.config_changed.emit()
        self.refresh()
        return True

    def open_selector(self):
        if self._tasks_running():
            self.summary.setText("任务正在进行；结束或停止后再选择分析范围。")
            return
        if self._dialog is not None:
            self._dialog.show()
            self._dialog.raise_()
            return
        dialog = self._dialog = QDialog(self)
        dialog.setWindowTitle("选择库分析范围")
        dialog.resize(680, 560)
        layout = QVBoxLayout(dialog)
        hint = QLabel("选择电影、动漫等媒体库，或填写更细的目录。画像、打标和 AI 缓存会按范围保存。\n库和额外目录取并集；不会根据片名或有无番号自动判断所属库。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.selected_mode = QRadioButton("只分析所选媒体库和额外目录")
        self.all_mode = QRadioButton("明确分析全部媒体库（会合并不同分类的喜好）")
        all_selected = self.selection_ready() and not self.cfg.library_scope_ids and not self.cfg.library_scope_paths
        self.all_mode.setChecked(all_selected)
        self.selected_mode.setChecked(not all_selected)
        layout.addWidget(self.selected_mode)
        layout.addWidget(self.all_mode)
        self.library_list = QListWidget()
        layout.addWidget(self.library_list, 1)
        self.catalog_status = QLabel("读取服务器配置的媒体库；不会修改服务器或媒体文件。")
        self.catalog_status.setWordWrap(True)
        layout.addWidget(self.catalog_status)
        reload_button = QPushButton("刷新媒体库列表")
        reload_button.clicked.connect(self._load_catalog)
        layout.addWidget(reload_button)
        layout.addWidget(QLabel("额外目录（源服务器路径，每行一项；包含全部子目录）："))
        self.path_edit = QPlainTextEdit("\n".join(self.cfg.library_scope_paths))
        self.path_edit.setPlaceholderText("例如 /media/movies 或 D:\\电影\\某分类；按完整目录边界匹配")
        self.path_edit.setMaximumHeight(100)
        layout.addWidget(self.path_edit)
        self.all_mode.toggled.connect(lambda checked: self.library_list.setEnabled(not checked))
        self.all_mode.toggled.connect(lambda checked: self.path_edit.setEnabled(not checked))
        self.library_list.setEnabled(not all_selected)
        self.path_edit.setEnabled(not all_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存范围")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save_dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self._catalog = copy.deepcopy(self.cfg.library_scope_catalog)
        self._fill_catalog(self._catalog)
        dialog.finished.connect(self._dialog_closed)
        dialog.show()
        self._load_catalog()

    def _fill_catalog(self, catalog):
        selected = set(self.cfg.library_scope_ids)
        if self.library_list.count():
            selected = {self.library_list.item(i).data(Qt.UserRole) for i in range(self.library_list.count())
                        if self.library_list.item(i).checkState() == Qt.Checked}
        self.library_list.clear()
        known = set()
        for entry in catalog:
            key = entry["id"]
            known.add(key)
            item = QListWidgetItem(entry["name"] + " — " + "; ".join(entry.get("paths", [])))
            item.setData(Qt.UserRole, key)
            item.setCheckState(Qt.Checked if key in selected else Qt.Unchecked)
            self.library_list.addItem(item)
        for key in sorted(selected - known):
            item = QListWidgetItem("原选媒体库暂不可用，请刷新或取消选择")
            item.setData(Qt.UserRole, key)
            item.setCheckState(Qt.Checked)
            self.library_list.addItem(item)

    def _load_catalog(self):
        if self._tasks_running():
            return
        from utils.library_source import discover_scopes

        source = copy.deepcopy(self.cfg)
        request = selection_source(self.cfg)
        self._worker = CallWorker(lambda stop: discover_scopes(source, should_stop=stop))
        self._worker.result.connect(lambda rows: self._catalog_loaded(rows, request))
        self._worker.error.connect(self.catalog_status.setText)
        self.catalog_status.setText("正在后台读取媒体库列表…")
        start_task(self, self._worker, "读取可选媒体库")

    def _catalog_loaded(self, rows, request):
        if self._dialog is None:
            return
        if request != selection_source(self.cfg):
            self.catalog_status.setText("连接已变化，请刷新媒体库列表。")
            return
        self._catalog = rows
        self._fill_catalog(rows)
        self.catalog_status.setText(f"已发现 {len(rows)} 个媒体库。未发现时可手动填写额外目录。")

    def _save_dialog(self):
        ids = [self.library_list.item(i).data(Qt.UserRole) for i in range(self.library_list.count())
               if self.library_list.item(i).checkState() == Qt.Checked]
        if self.apply_selection(self._catalog, ids, self.path_edit.toPlainText().splitlines(),
                                all_libraries=self.all_mode.isChecked()):
            self._dialog.accept()

    def _dialog_closed(self):
        dialog = self._dialog
        if self._worker and self._worker.isRunning():
            self._worker.stop()
        self._dialog = None
        if dialog is not None:
            dialog.deleteLater()
