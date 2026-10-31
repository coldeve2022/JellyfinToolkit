"""替换版本检索页 —— 阶段二：对已确认「需要替换」的视频，按作品编号查可用替代版本。

为什么单独一页
--------------
"检查画面是否需要处理"和"上网找替代版本"是两件独立的事（见 docs/像素化状态与工作流.md）。
把它们塞在一页里，检查页的表格既要显示结论又要显示检索状态、还要放候选面板，
最后两边都不好用。

这一页只干一件事：**拿着你已经标记为「需要替换」的作品编号，去外部站点
查有没有可用的替代版本。**好处是：

- 检查页的表格可以精简回"文件名 / 结论 / 处理 / 记录来源"；
- 已确认需替换的视频在这里**按作品编号聚合**（一部片多个副本只占一行），
  可以反复重查、可以导出清单；
- 检索失败、站点被拦这些事不会干扰检查流程。

数据来源：``VerdictStore.confirmed_censored()`` —— 只认真人标记，不认算法猜测。
"""

from __future__ import annotations

import csv
import os
from typing import Optional

from PySide6.QtCore import Qt, Slot
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QColor, QDrag, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QSplitter, QMenu, QLineEdit, QComboBox,
)

from censor import CensorService
from censor.releases import ReleaseReport, ReleaseService
from censor.release_filter import FIELDS, FilterRule
from censor.release_query import NumberRow, ReleaseQuery
from ui.release_filters import ReleaseFilterBar, ColumnFilterMenu, FilterHeader
from censor.releases.models import normalize_number
from ui.censor_release import ReleaseLookupWorker, ReleasePanel
from ui.services import events, start_task
from ui.widgets import make_number_item
from ui import theme

__all__ = ["CensorReplacePage"]

_COLUMNS = ["番号", "片数", "检索结果", "候选", "最大种子", "最近检索", "喜欢", "播放状态"]
_COLUMN_FIELDS = ["number", "files", "state", "candidates", "magnet_size", "queried", "favorite", "played"]

_STATE_COLOR = {
    "confirmed": "#1e8449",   # 确有
    "likely": "#b9770e",      # 疑似
    "none": "#7f8c8d",        # 未见
    "unknown": "#7f8c8d",     # 未查
    "todo": "#d35400",        # 还没查
}
_STATE_TOKEN = {"confirmed": "success", "likely": "warning", "none": "text_secondary",
                "unknown": "text_secondary", "todo": "warning"}


class _NumberTable(QTableWidget):
    """替换版本检索的表格：能把选中行背后的**文件**拖出去。

    表格默认拖出的是单元格文本（番号），拖到 Lada / 资源管理器里没用。
    这里改成塞文件 URL，接收方就会当成"拖进来一个视频文件"。
    """

    def __init__(self, rows: int, columns: int, parent=None) -> None:
        super().__init__(rows, columns, parent)
        #: 由页面提供：当前要拖出去的文件路径
        self.path_provider = lambda: []
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    def startDrag(self, supported_actions) -> None:  # noqa: N802 - Qt 命名
        paths = [p for p in (self.path_provider() or []) if os.path.isfile(p)]
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setText("\n".join(paths))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class CensorReplacePage(QWidget):
    """阶段二工作台：已确认需替换 → 查可用替代版本。"""

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
        #: 检查页送来的作品（规范化番号）。非空时只查这些，清单本身仍显示全部。
        self._focus_numbers: set[str] = set()
        self._pending_focus_lookup = False
        #: 右键「只显示这些」用；空集合 = 不限制
        self._only_keys: set[str] = set()
        #: 高级筛选：任意条件组合（见 censor/release_filter.py）
        self._rules: 'list[FilterRule]' = []
        self._logic: str = 'and'
        self._column_values = {}
        self._snapshot_service = None
        #: Jellyfin 快照（规范化路径 → 喜欢/播放状态）；空 = 未同步
        self._review_items: dict = {}
        self._refresh_owner = QWidget(self)
        self._refresh_owner.hide()
        self._current_key: str = ""
        self._setup_ui()
        events.censor_changed.connect(self._on_censor_changed)
        events.media_changed.connect(self._media_changed)
        events.user_state_changed.connect(self._user_state_changed)

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
        self._reload_review()
        self.refresh()

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        toolbar = QHBoxLayout()
        title = QLabel("替换版本检索")
        title.setProperty("cssClass", "page-title")
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.btn_refresh = QPushButton("刷新清单")
        self.btn_refresh.setToolTip("重新读库里「已确认需替换」的记录")
        self.btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(self.btn_refresh)
        self.btn_lookup_new = QPushButton("查询未查")
        self.btn_lookup_new.setToolTip("只查还没有结果或结果已过期的番号")
        self.btn_lookup_new.setProperty("cssClass", "accent")
        self.btn_lookup_new.clicked.connect(lambda: self._lookup(force=False))
        toolbar.addWidget(self.btn_lookup_new)
        self.btn_send_replace = QPushButton("送去替换")
        self.btn_send_replace.setToolTip(
            "把选中作品的原始文件交给「视频版本管理」；只传数据，替换在那边做")
        self.btn_send_replace.clicked.connect(self._send_selected_to_versions)
        toolbar.addWidget(self.btn_send_replace)
        self.btn_focus_clear = QPushButton("显示全部")
        self.btn_focus_clear.setToolTip("清除「只处理检查页送来的这些」限定")
        self.btn_focus_clear.clicked.connect(self._clear_focus)
        self.btn_focus_clear.setVisible(False)
        toolbar.addWidget(self.btn_focus_clear)
        self.btn_lookup_all = QPushButton("全部重查")
        self.btn_lookup_all.setToolTip("忽略缓存，重查当前筛选范围内的番号")
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
        settings = QPushButton("设置…")
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
        selected = QPushButton('查询选中')
        selected.clicked.connect(self._lookup_selected)
        toolbar.insertWidget(2, selected)
        layout.addLayout(toolbar)

        self.lbl_summary = QLabel("正在读取已确认需替换的记录…")
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
                           ('未见', 'none'), ('未查', 'todo'), ('未完成/未知', 'unknown')):
            self.state_filter.addItem(label, key)
        # 显式归位：默认必须是"全部"，否则一进来就只剩"确有"，
        # 用户会以为清单少了一大半（这条以前靠 addItem 的隐式行为，靠不住）
        self.state_filter.setCurrentIndex(0)
        self.state_filter.currentIndexChanged.connect(self._filter)
        filter_row.addWidget(self.state_filter)
        layout.addLayout(filter_row)

        # 第二行筛选：喜欢 / 播放状态（复用检查页的 Jellyfin 快照，不另做一套口径）
        library_row = QHBoxLayout()
        self.combo_favorite = QComboBox()
        self.combo_favorite.setProperty('taskInteractive', True)
        for text, value in (('喜欢：全部', 'all'), ('只看喜欢', 'favorite'),
                            ('只看未喜欢', 'not_favorite'), ('喜欢状态未知', 'unknown')):
            self.combo_favorite.addItem(text, value)
        self.combo_favorite.setCurrentIndex(0)
        self.combo_favorite.currentIndexChanged.connect(self._filter)
        library_row.addWidget(self.combo_favorite)
        self.combo_playback = QComboBox()
        self.combo_playback.setProperty('taskInteractive', True)
        for text, value in (('播放：全部', 'all'), ('未播放', 'unplayed'),
                            ('播放中', 'progress'), ('已播放', 'played'),
                            ('播放状态未知', 'unknown')):
            self.combo_playback.addItem(text, value)
        self.combo_playback.setCurrentIndex(0)
        self.combo_playback.currentIndexChanged.connect(self._filter)
        library_row.addWidget(self.combo_playback)
        self.btn_clear_filters = QPushButton('清除筛选')
        self.btn_clear_filters.clicked.connect(self._clear_filters)
        library_row.addWidget(self.btn_clear_filters)
        library_row.addStretch()
        self.lbl_filter_note = QLabel('')
        self.lbl_filter_note.setProperty('cssClass', 'muted-label')
        library_row.addWidget(self.lbl_filter_note)
        layout.addLayout(library_row)
        self.filter_bar = ReleaseFilterBar(self)
        self.filter_bar.changed.connect(self._conditions_changed)
        layout.addWidget(self.filter_bar)
        self.lbl_active_filters = QLabel()
        self.lbl_active_filters.setWordWrap(True)
        self.lbl_active_filters.setProperty("cssClass", "muted-label")
        layout.addWidget(self.lbl_active_filters)
        body = QSplitter(Qt.Horizontal)
        self.splitter = body
        self.table = _NumberTable(0, len(_COLUMNS))
        self.table.setHorizontalHeader(FilterHeader(self.table))
        self.table.path_provider = self._drag_paths
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
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        header.filter_requested.connect(self._header_menu)
        header.setToolTip("点击表头右侧 ▾ 筛选本列；点击标题排序；其他字段在上方添加条件")
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 100)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
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
        local = QPushButton("导入新版本…")
        local.clicked.connect(self._import_candidate)
        right_lay.addWidget(local)
        body.addWidget(right)
        body.setSizes([580, 420])
        layout.addWidget(body, 1)
        from ui.workspace_preferences import WorkspacePreferences
        self.workspace = WorkspacePreferences(self, 'release', self.table, splitter=body,
            filters={'search': self.search, 'state': self.state_filter,
                     'favorite': self.combo_favorite, 'playback': self.combo_playback})
        state = self.workspace.state
        saved_rules = state.get('filter_rules') or []
        if isinstance(saved_rules, list):
            self._rules = [FilterRule.from_dict(item) for item in saved_rules
                           if isinstance(item, dict)]
        self._logic = str(state.get('filter_logic') or 'and')
        self.filter_bar.set_rules(self._rules, self._logic)
        self._rules = self.filter_bar.rules()
        self._logic = self.filter_bar.logic_key()
        saved_columns = state.get("column_filters", {})
        if isinstance(saved_columns, dict):
            self._column_values = {k: set(str(x) for x in v) for k, v in saved_columns.items()
                                   if k in _COLUMN_FIELDS and isinstance(v, list)}
        self._filter()
        menu.addMenu(self.workspace.menu(more)).setText('工作区布局与偏好')

    # ── 数据 ──
    def receive_requests(self, paths) -> None:
        """接收检查页送来的作品，**只查这些**。

        为什么不在本页重做"喜欢/结论"筛选：检查页已经有这些筛选，而且带
        Jellyfin 账号状态快照。在这里再实现一遍就是两套逻辑、两个口径 ——
        用户要的是"把我挑出来的这些拿去查"，不是"再给我一个筛选器"。
        """
        paths = [p for p in dict.fromkeys(str(x) for x in (paths or [])) if p]
        if not paths:
            return
        numbers: set[str] = set()
        try:
            records = self._censor.store.get_many(paths)
            for item in records.values():
                key = normalize_number(getattr(item, 'number', '') or '')
                if key:
                    numbers.add(key)
        except Exception:  # noqa: BLE001 - 读库失败就退回全量，不挡流程
            numbers = set()
        self._focus_numbers = numbers
        self.btn_focus_clear.setVisible(bool(numbers))
        self._pending_focus_lookup = bool(numbers)
        self.lbl_summary.setText(
            f'已从检查页收到 {len(paths)} 个文件、{len(numbers)} 个可检索番号；'
            '本次只查这些，清单里其它作品不动')
        self.refresh()

    def _clear_focus(self) -> None:
        self._focus_numbers = set()
        self._pending_focus_lookup = False
        self.btn_focus_clear.setVisible(False)
        self.refresh()

    def _in_focus(self, row: 'NumberRow') -> bool:
        if not self._focus_numbers:
            return True
        return normalize_number(row.number) in self._focus_numbers

    def _send_selected_to_versions(self) -> None:
        """把选中作品的**原始文件路径**交给版本管理。

        这里只传数据，配对/快照/替换/回滚全部由版本管理模块负责 ——
        两个页面之间不应该互相知道对方的实现细节。
        """
        rows = sorted({i.row() for i in self.table.selectionModel().selectedIndexes()})
        paths: list[str] = []
        for index in rows:
            if self.table.isRowHidden(index):
                continue
            key = self.table.item(index, 0).data(Qt.UserRole)
            row = self._rows.get(key)
            if row is not None:
                paths.extend(row.paths)
        if not paths:
            self.lbl_summary.setText('先在清单里选中要替换的作品（可多选）')
            return
        from ui.services import events
        events.media_upgrade_requested.emit(list(dict.fromkeys(paths)))
        self.lbl_summary.setText(
            f'已把 {len(paths)} 个原始文件交给「视频版本管理」；'
            '在那边加入新版本、预览配对后再执行替换')

    def refresh(self) -> None:
        """从库里重建清单：已确认需替换 → 按作品编号聚合。"""
        self._reload_review()
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
            key = NumberRow(item.number or "", [item.path]).key
            row = grouped.setdefault(key, NumberRow(number=item.number or ""))
            if not row.number and item.number:
                row.number = item.number
            if item.path not in row.paths:
                row.paths.append(item.path)
                row.records.append(item)
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
        if self._pending_focus_lookup:
            self._pending_focus_lookup = False
            if self._focus_numbers:
                # 用户在检查页挑好后才送过来，意思很明确：现在就查这些
                self._lookup(force=False)
                return
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
                self._review_text(row, "favorite"),
                self._review_text(row, "played"),
            ]
            for col, text in enumerate(values):
                numeric = {1: len(row.paths), 3: len(row.report.candidates) if row.report else 0,
                           4: FIELDS['magnet_size'].getter(row, {}),
                           5: row.report.queried_at if row.report else 0}
                item = make_number_item(text, numeric[col]) if col in numeric else QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0:
                    item.setData(Qt.UserRole, row.key)
                if col == 2:
                    item.setForeground(QColor(theme.color(_STATE_TOKEN.get(row.state_key, "text_primary"))))
                self.table.setItem(index, col, item)
        self.table.setSortingEnabled(True)
        self._filter()
        if selected in self._rows and selected in {r.key for r in self._visible_rows()}:
            for index in range(self.table.rowCount()):
                if (self.table.item(index, 0).data(Qt.UserRole) == selected
                        and not self.table.isRowHidden(index)):
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
        # 只留 5 个数：原来拼了 9 段，读起来比数字本身还费劲
        checked = counts['confirmed'] + counts['likely'] + counts['none']
        text = (
            f"{len(rows)} 部 · 已查 {checked} · 待查 {counts['todo']}"
            f" · 有 {counts['confirmed']} · 疑似 {counts['likely']} · 未见 {counts['none']}"
            + (" · 只查送来的这部份" if self._focus_numbers else ""))
        # 筛选后让人一眼看到"现在还剩几部"，而不是只看到总数
        visible = sum(1 for i in range(self.table.rowCount()) if not self.table.isRowHidden(i))
        if visible != len(rows):
            text += f"（当前筛选可见 {visible}）"
            if visible == 0 and rows:
                text += " —— 没有符合条件的结果，点「清除筛选」恢复"
        self.lbl_summary.setText(text)
        self.lbl_summary.setToolTip(
            f"共 {sum(len(r.paths) for r in rows)} 个文件；可检索 {len(searchable)} 部"
            + ("；其余没有番号，无法检索" if len(rows) > len(searchable) else ""))

    # ── 选择 ──
    def _on_row_selected(self) -> None:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        indexes = [i for i in indexes if not self.table.isRowHidden(i.row())]
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
    def _query(self, rules=None, logic=None):
        return ReleaseQuery(
            words=tuple(self.search.text().casefold().split()),
            quick={'state': self.state_filter.currentData(),
                   'favorite': self.combo_favorite.currentData() if self.combo_favorite.isEnabled() else 'all',
                   'played': self.combo_playback.currentData() if self.combo_playback.isEnabled() else 'all'},
            columns=self._column_values,
            rules=tuple(self._rules if rules is None else rules),
            logic=self._logic if logic is None else logic,
            only_keys=frozenset(self._only_keys))

    def _visible_rows(self):
        query, ctx = self._query(), self._filter_context()
        return [row for row in self._rows.values() if query.matches(row, ctx)]

    def _filter(self):
        if not hasattr(self, 'table'):
            return
        query, ctx = self._query(), self._filter_context()
        hidden_current = False
        for index in range(self.table.rowCount()):
            item = self.table.item(index, 0)
            row = self._rows.get(item.data(Qt.UserRole)) if item else None
            show = row is not None and query.matches(row, ctx)
            self.table.setRowHidden(index, not show)
            if not show:
                for col in range(self.table.columnCount()):
                    cell = self.table.item(index, col)
                    if cell:
                        cell.setSelected(False)
            hidden_current |= not show and row is not None and row.key == self._current_key
        if hidden_current:
            self._current_key = ''
            self.files.setRowCount(0)
            self.panel.clear()
            self.lbl_detail.setText('选择一个番号查看候选与文件')
        self._sync_filter_summary()
        self._update_summary()

    def _review_service(self):
        from utils.jellyfin_review import shared_review_service
        self._snapshot_service = shared_review_service(self.cfg, self._censor.store.db_path.parent)
        return self._snapshot_service

    def _user_state_changed(self, context_key, data_dir):
        service = self._review_service()
        if context_key == service.context_key and data_dir == str(service._db_path.parent.resolve()):
            self._reload_review()
            self._fill_table()

    def _reload_review(self):
        service = self._review_service()
        snapshot = service.read_snapshot()
        self._review_items = snapshot.items
        ready = snapshot.status == 'ready'
        for combo in (self.combo_favorite, self.combo_playback):
            combo.setEnabled(ready)
            combo.setToolTip('' if ready else snapshot.message)
        self.lbl_filter_note.setText('' if ready else 'Jellyfin：' + snapshot.message)

    def _review_of(self, path: str) -> dict:
        if not self._review_items:
            return {}
        try:
            from utils.jellyfin_review import JellyfinReviewService

            return self._review_items.get(JellyfinReviewService.normalize_path(path), {}) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _favorite_matches(self, row, want):
        return want == 'all' or FIELDS['favorite'].getter(row, self._filter_context()) == want

    def _playback_matches(self, row, want):
        return want == 'all' or FIELDS['played'].getter(row, self._filter_context()) == want

    def _review_text(self, row, field):
        spec = FIELDS[field]
        return dict(spec.choices).get(spec.getter(row, self._filter_context()), '未知')

    def _filter_context(self):
        return {'review': self._review_of, 'exists': os.path.isfile}

    def _conditions_changed(self):
        self._rules = self.filter_bar.rules()
        self._logic = self.filter_bar.logic_key()
        self._save_filters()
        self._filter()

    def _save_filters(self):
        if not hasattr(self, 'workspace'):
            return
        self.workspace.state['filter_rules'] = [rule.as_dict() for rule in self._rules]
        self.workspace.state['filter_logic'] = self._logic
        self.workspace.state['column_filters'] = {k: sorted(v) for k, v in self._column_values.items()}
        self.workspace.schedule()

    def _preview_count(self, rules, logic):
        query, ctx = self._query(rules, logic), self._filter_context()
        return sum(query.matches(row, ctx) for row in self._rows.values())

    def _set_column_values(self, field, values):
        if values is None:
            self._column_values.pop(field, None)
        else:
            self._column_values[field] = set(str(value) for value in values)
        self._save_filters()
        self._filter()

    def _column_counts(self, field):
        query, ctx = self._query(), self._filter_context()
        counts = {}
        for row in self._rows.values():
            key = str(FIELDS[field].getter(row, ctx))
            counts.setdefault(key, 0)
            if query.matches(row, ctx, exclude_column=field):
                counts[key] += 1
        return counts

    def _header_menu(self, pos):
        header = self.table.horizontalHeader()
        col = header.logicalIndexAt(pos)
        if col < 0:
            return
        field = _COLUMN_FIELDS[col]
        spec = FIELDS[field]
        # Numeric/date columns offer an in-page comparison editor as well.
        menu = ColumnFilterMenu(spec.label, self._column_counts(field),
            self._column_values.get(field), self._column_labels(field), self)
        menu.applied.connect(lambda values: self._set_column_values(field, values))
        menu.addSeparator()
        menu.addAction('编辑比较条件…', lambda: self.filter_bar.select_field(field))
        menu.addAction('升序排列', lambda: self.table.sortItems(col, Qt.AscendingOrder))
        menu.addAction('降序排列', lambda: self.table.sortItems(col, Qt.DescendingOrder))
        menu.exec(header.mapToGlobal(pos))
        menu.deleteLater()

    def _column_labels(self, field):
        labels = dict(FIELDS[field].choices)
        if field == 'queried':
            from datetime import datetime
            labels.update({key: datetime.fromtimestamp(float(key)).strftime('%Y-%m-%d %H:%M:%S')
                           if float(key) > 0 else '从未检索' for key in self._column_counts(field)})
        elif field == 'magnet_size':
            labels.update({key: f'{float(key) / (1024 ** 3):.2f} GB' if float(key) > 0 else '无已知大小'
                           for key in self._column_counts(field)})
        return labels

    def _sync_filter_summary(self):
        parts = []
        if self.search.text().strip():
            parts.append('搜索：' + self.search.text().strip())
        for control in (self.state_filter, self.combo_favorite, self.combo_playback):
            if control.isEnabled() and control.currentData() not in ('', 'all', None):
                parts.append(control.currentText())
        for field, values in self._column_values.items():
            labels = dict(FIELDS[field].choices)
            text = '、'.join(labels.get(v, v) for v in sorted(values)) if values else '未选择任何取值'
            parts.append(FIELDS[field].label + '：' + text)
        if self._only_keys:
            parts.append(f'仅选中范围 {len(self._only_keys)} 部')
        if self._rules:
            parts.append(f'页内条件 {len(self._rules)} 条（{self._logic.upper()}）')
        self.lbl_active_filters.setText('；'.join(parts) if parts else '未设置筛选 · 点击表头 ▾ 选择取值，上方可添加比较条件')
        for col, field in enumerate(_COLUMN_FIELDS):
            active = field in self._column_values or any(r.field == field for r in self._rules)
            self.table.horizontalHeaderItem(col).setText(_COLUMNS[col] + (' ●' if active else ''))

    def _clear_filters(self):
        controls = (self.search, self.state_filter, self.combo_favorite, self.combo_playback)
        for control in controls:
            control.blockSignals(True)
        self.search.clear()
        for control in controls[1:]:
            control.setCurrentIndex(0)
        for control in controls:
            control.blockSignals(False)
        self._only_keys = set()
        self._column_values = {}
        self._rules, self._logic = [], 'and'
        self.filter_bar.set_rules([])
        self._save_filters()
        self._filter()

    # ── 选中与拖出 ──
    def _selected_rows(self) -> 'list[NumberRow]':
        model = self.table.selectionModel()
        if model is None:
            return []
        out = []
        for index in sorted({i.row() for i in model.selectedIndexes()}):
            if self.table.isRowHidden(index):
                continue
            item = self.table.item(index, 0)
            row = self._rows.get(item.data(Qt.UserRole)) if item else None
            if row is not None:
                out.append(row)
        return out

    def _drag_paths(self) -> 'list[str]':
        """拖出去的是**文件**（接收方当成拖入视频），不是番号文本。"""
        rows = self._selected_rows()
        if not rows:
            index = self.table.currentRow()
            item = self.table.item(index, 0) if index >= 0 else None
            row = self._rows.get(item.data(Qt.UserRole)) if item else None
            rows = [row] if row is not None and not self.table.isRowHidden(index) else []
        paths: 'list[str]' = []
        for row in rows:
            paths.extend(row.paths)
        return list(dict.fromkeys(paths))

    # ── 右键菜单 ──
    def _table_menu(self, pos) -> None:
        index = self.table.rowAt(pos.y())
        if index >= 0 and not self.table.isRowHidden(index):
            if index not in {i.row() for i in self.table.selectionModel().selectedIndexes()}:
                self.table.selectRow(index)
        rows = self._selected_rows()
        if not rows:
            return
        count = len(rows)
        menu = QMenu(self)
        menu.addAction(f'查询选中（{count}）', self._lookup_selected)
        menu.addAction(f'送去替换（{count}）', self._send_selected_to_versions)
        menu.addAction(f'送去 Lada 破码（{count}）', self._send_selected_to_lada)
        menu.addSeparator()
        menu.addAction('复制番号', self._copy_numbers)
        menu.addAction('复制文件路径', self._copy_paths)
        menu.addAction('复制磁链', self._copy_magnets)
        menu.addAction('打开候选页面', self._open_selected_page)
        menu.addSeparator()
        menu.addAction('只显示这些（再点「清除筛选」恢复）', self._focus_selected)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy(self, text: str, what: str) -> None:
        text = text.strip()
        if not text:
            self.lbl_summary.setText(f'没有可复制的{what}')
            return
        QGuiApplication.clipboard().setText(text)
        lines = text.count(chr(10)) + 1
        self.lbl_summary.setText(f'已复制 {lines} 行{what}')

    def _copy_numbers(self) -> None:
        self._copy('\n'.join(row.number for row in self._selected_rows() if row.number), '番号')

    def _copy_paths(self) -> None:
        paths: 'list[str]' = []
        for row in self._selected_rows():
            paths.extend(row.paths)
        self._copy('\n'.join(dict.fromkeys(paths)), '文件路径')

    def _copy_magnets(self) -> None:
        links: 'list[str]' = []
        for row in self._selected_rows():
            best = row.report.best if row.report else None
            magnet = getattr(best, 'best_magnet', None) if best else None
            if magnet is not None and getattr(magnet, 'link', ''):
                links.append(magnet.link)
        self._copy('\n'.join(dict.fromkeys(links)), '磁链')

    def _send_selected_to_lada(self) -> None:
        paths: 'list[str]' = []
        for row in self._selected_rows():
            paths.extend(row.paths)
        paths = [p for p in dict.fromkeys(paths) if os.path.isfile(p)]
        if not paths:
            self.lbl_summary.setText('选中的文件都不在本地，无法送去 Lada')
            return
        events.lada_ready.emit(paths)
        self.lbl_summary.setText(f'已把 {len(paths)} 个文件送去「画面修复」页')

    def _open_selected_page(self) -> None:
        rows = [row for row in self._selected_rows() if row.report and row.report.candidates]
        if not rows:
            self.lbl_summary.setText('选中的番号还没有候选页面')
            return
        from PySide6.QtGui import QDesktopServices

        opened = 0
        for row in rows[:10]:
            best = row.report.best if row.report else None
            url = getattr(best, 'page_url', '') or ''
            if url:
                QDesktopServices.openUrl(QUrl(url))
                opened += 1
        self.lbl_summary.setText(f'已打开 {opened} 个候选页面' + ('（最多 10 个）' if len(rows) > 10 else ''))

    def _focus_selected(self) -> None:
        self._only_keys = {row.key for row in self._selected_rows()}
        self._filter()
        self.lbl_summary.setText(f'只显示选中的 {len(self._only_keys)} 个（点「清除筛选」恢复）')

    def _lookup_selected(self):
        indexes = self.table.selectionModel().selectedRows()
        numbers = [self._rows[self.table.item(index.row(), 0).data(Qt.UserRole)].number
                   for index in indexes if not self.table.isRowHidden(index.row())]
        if numbers:
            self._lookup(force=True, numbers=numbers)
        else:
            self.lbl_summary.setText('先选择一个或多个作品')

    def lookup_targets(self, force=False) -> list[str]:
        return [row.number for row in self._visible_rows()
                if row.searchable and self._in_focus(row)
                and (force or self._release.fresh(row.number) is None)]

    def _ensure_supjav_channel(self) -> str:
        """Supjav 站内必须先注册浏览器 reader，否则只会拿到 403。

        这里**静默**准备会话（不弹窗、不抢焦点）：实测有头 Chromium 能自动过
        Cloudflare 的 JS 挑战；真需要人机验证时窗口已经在那儿，用户点一下即可。
        以前要用户自己跑到设置页点"连接 Supjav 浏览器会话"，忘了点就等于
        整个 Supjav 源永远拿不到东西。
        """
        sources = getattr(self.cfg, "release_sources", None) or []
        if "supjav" not in sources:
            return ""
        if str(getattr(self.cfg, "release_supjav_mode", "auto")) == "index":
            return ""
        try:
            from ui.supjav_browser import prepare_supjav_session

            prepare_supjav_session()
            return "Supjav 站内：浏览器会话就绪"
        except Exception as exc:  # noqa: BLE001 - 会话不可用要如实说，但不挡检索
            return f"Supjav 站内：浏览器会话不可用（{exc}）"

    def _lookup(self, *, force: bool, numbers: Optional[list[str]] = None) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.lbl_summary.setText("检索进行中，先等它结束或点「停止」")
            return
        channel = self._ensure_supjav_channel()
        if channel:
            self.lbl_summary.setText(channel)
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
        if not start_task(self, worker, "替换版本检索", mutating=False):
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
        numbers = [row.number for row in self._visible_rows() if row.searchable and self._in_focus(row)]
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
        worker.finished.connect(self._lookup_prepare_finished)
        worker.error.connect(self._refresh_error)
        self.lbl_summary.setText('后台校验检索缓存与待办…')
        start_task(self, worker, '校验检索缓存与待办')

    @Slot()
    def _lookup_prepare_finished(self):
        if getattr(self, '_lookup_prepare', None) is self.sender():
            self._lookup_prepare = None

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
                        if offset == 4:
                            cell.setData(Qt.UserRole, FIELDS['magnet_size'].getter(row, {}))
                        if offset == 5:
                            cell.setData(Qt.UserRole, row.report.queried_at if row.report else 0)
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
        rows = self._visible_rows()
        if not rows:
            self.lbl_summary.setText("清单是空的，没有可导出的内容")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单", "需替换-版本检索.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["番号", "文件数", "检索结果", "候选数", "最大种子", "磁链", "文件"])
                for row in sorted(rows, key=lambda r: r.key):
                    best = row.report.best if row.report else None
                    magnet = best.best_magnet.uri if (best and best.best_magnet) else ""
                    writer.writerow([row.number, len(row.paths), row.state_text,
                                     len(row.report.candidates) if row.report else 0,
                                     row.best_magnet_text, magnet,
                                     " | ".join(row.paths)])
            self.lbl_summary.setText(f"已导出当前筛选的 {len(rows)} 个番号 → {path}")
        except OSError as exc:
            self.lbl_summary.setText(f"导出失败：{exc}")

    # ── 生命周期 ──
    def _settings(self):
        from ui.release_settings import edit_release_settings
        edit_release_settings(self.cfg, self)

    def on_config_changed(self) -> None:
        self._censor = CensorService(self.cfg)
        self._release = ReleaseService(self.cfg)
        self._snapshot_service = None
        self.refresh()

    def apply_theme(self):
        for row in self._rows.values():
            self._update_row_cells(row)

    def shutdown(self) -> None:
        from shiboken6 import isValid
        self.workspace.save()
        self._refresh_version += 1
        self._refresh_dirty = False
        for pending in (self._refresh_worker, getattr(self, '_lookup_prepare', None)):
            if pending is not None and isValid(pending) and pending.isRunning():
                pending.stop()
                pending.wait(3000)
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.stop()
            worker.wait(3000)
