"""Generic video pixelation review page using the existing toolkit style.

The UI displays service decisions, processing states, structural scores and exact
analyzed-frame thumbnails. It never judges file pixels from metadata itself.
"""

from __future__ import annotations

import os
from collections import Counter, deque
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QItemSelectionModel, QSignalBlocker
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QDialog, QDialogButtonBox, QSpinBox,
    QMenu, QProgressBar, QSizePolicy, QMessageBox,
    QLineEdit, QCheckBox, QScrollArea,
)

from censor import CensorOptions, CensorService, CensorState, VideoVerdict
from censor.fusion import explain, fuse
from censor.models import EvidenceSource, ProcessingStatus
from censor.jobs import ScanJobs, scan_priority as scan_priority
from censor.releases import ReleaseService
from censor.releases.models import normalize_number
from censor.review_route import (
    ReviewBucket, WorkflowView, awaits_replacement, bucket as review_bucket,
    bucket_label, conclusion_label, in_view, category,
    next_review_target,
)
from ui.censor_gallery import EvidenceGallery
from config import ToolkitConfig
from ui.services import busy, start_task, stop_page
from ui.widgets import FileInputActions
from ui.censor_scan import CensorScanWorker
from ui.censor_history import HistoryLoadWorker
from ui.censor_release import ConnectionCheck
from utils.media_review_query import ReviewQuery, favorite_state, playback_state

__all__ = ["CensorshipPage"]

#: 表格列：**结论**与**处理**是两个独立维度，不再挤在一格里
#: （以前一列里混着"疑似·待复核""人工确认未见""低风险·未确证"，无法单独筛）
_COLUMNS = ["文件名", "标识", "结论", "处理", "结构分", "依据", "记录来源", "喜欢", "播放"]


class _SavedHistoryWorker(HistoryLoadWorker):
    """Keep persisted counts and history reads off the GUI thread."""

    saved_stats = Signal(object)

    def __init__(self, store, *, records=True, parent=None):
        super().__init__(store, parent=parent)
        self.records = records

    def run(self):
        try:
            self.saved_stats.emit(self.store.stats())
        except Exception as exc:
            self.error.emit(f'读取已保存记录计数失败：{exc}')
        if self.records:
            super().run()


class _FavoriteItem(QTableWidgetItem):
    def __lt__(self, other):
        return ((self.data(Qt.UserRole + 1) or 0, self.data(Qt.UserRole + 2) or '')
                < (other.data(Qt.UserRole + 1) or 0, other.data(Qt.UserRole + 2) or ''))

#: 结论（有没有码）用状态色
_STATE_COLOR = {
    CensorState.CENSORED: "#c0392b",
    CensorState.UNCENSORED: "#1e8449",
    CensorState.UNKNOWN: "#7f8c8d",
}

#: 处理（还需不需要人）用另一套色，避免和结论混淆
_BUCKET_COLOR = {
    ReviewBucket.PENDING: "#7f8c8d",
    ReviewBucket.FAILED: "#b9770e",
    ReviewBucket.UNREVIEWED: "#d35400",
    ReviewBucket.CONFIRMED: "#1e8449",
    ReviewBucket.LOW_RISK: "#566573",
    ReviewBucket.DEFERRED: "#566573",
}


def _evidence_grade(result):
    """给"处理"列做悬停说明：这条结论是靠什么来的。"""
    if result.verdict.of(EvidenceSource.MANUAL) is not None:
        return '人工确认'
    return '多时点画面' if result.is_decided else '证据不足'
class _DropTable(QTableWidget):
    """结果表格 —— 直接往表格上拖文件也能开始检测（与其它页面一致的手感）。"""

    files_dropped = Signal(list)

    def __init__(self, rows: int, cols: int, parent=None):
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit(paths)


class CensorshipPage(QWidget):
    """有码检测 + 人工复核。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setAcceptDrops(True)
        self._worker: Optional[CensorScanWorker] = None
        self._service = CensorService(cfg)
        self._jobs = ScanJobs(self._service.store.db_path)
        self._history_offset = 0
        self._history_worker = None
        self._history_loading = False
        self._pending_results = deque()
        self._category_counts = Counter()
        self._review_overrides = {}
        self._media_epochs = {}
        self._pending_verifications = set()
        self._last_status = {}
        self._batching = False
        self._finishing = False
        self._batch_review_navigation = None
        self._evidence_requests = []
        self._evidence_queue_epoch = 0
        self._evidence_queue_timer = QTimer(self)
        self._evidence_queue_timer.setSingleShot(True)
        self._evidence_queue_timer.setInterval(100)
        self._evidence_queue_timer.timeout.connect(self._pump_evidence_requests)
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._drain_results)
        # 操作反馈的恢复定时器：挂在 self 上，页面销毁时自动失效
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(8000)
        self._flash_timer.timeout.connect(self._restore_progress_title)
        self._results: dict[str, VideoVerdict] = {}
        self._submitted: list[str] = []
        self._current: Optional[VideoVerdict] = None
        # ── 无码版本检索：结果按番号缓存，同一部片的多份副本共用 ──
        self._release_service = ReleaseService(cfg)
        self._restored = False
        self._startup_cancelled = False
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.setInterval(40)
        self._startup_timer.timeout.connect(self._restore_history_once)
        self._saved_stats_worker = None
        self._conn_check: Optional[ConnectionCheck] = None
        self._review_shortcuts = {}
        self._normal_sort = None
        self._setting_library_sort = False
        from ui.jellyfin_review_controls import JellyfinReviewController
        self.library_review = JellyfinReviewController(self)
        self._setup_ui()
        from ui.censor_results import ReviewTablePresenter
        self._table_presenter = ReviewTablePresenter(self, _STATE_COLOR, _BUCKET_COLOR, _evidence_grade)
        self.library_review.changed.connect(self.library_status.setText)
        self.library_status.setText(self.library_review.last_status)
        self.library_review.start_auto_refresh()
        from ui.review_controls import ReviewMetadataController
        self.metadata_controller = ReviewMetadataController(self)
        self.metadata_controller.changed.connect(self.sync_status.setText)
        self.metadata_controller.timer.start(1200)
        from ui.services import events
        events.media_changed.connect(self._media_changed)
        events.config_changed.connect(self.library_review.reload)
        self._refresh_stats()
        if self._jobs.pending() or self._jobs.unfinished_sources():
            self._stats_label.setText('发现未完成任务，点击“继续未完成”恢复')

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)
        toolbar = QHBoxLayout()
        title = QLabel('像素化检查')
        title.setProperty('cssClass', 'page-title')
        toolbar.addWidget(title)
        toolbar.addStretch()
        toolbar.addWidget(FileInputActions(self, self._start_scan))
        library = QPushButton('从当前媒体库导入')
        library.clicked.connect(self._import_library)
        toolbar.addWidget(library)
        self.more_button = QPushButton('更多')
        menu = QMenu(self.more_button)
        library_menu = menu.addMenu('Jellyfin 喜欢与播放状态')
        library_menu.addAction('选择观看账号…', self.library_review.choose_user)
        library_menu.addAction('同步喜欢和播放状态', self.library_review.refresh)
        library_menu.addAction('导入喜欢的视频进行检查', self.library_review.import_favorites)
        library_menu.addAction('只看喜欢且有码的视频', self._show_favorite_censored)
        sync_menu = menu.addMenu('标签同步')
        sync_menu.addAction('查看状态 / 重试…', lambda: self.metadata_controller.show_status())
        sync_menu.addAction('补同步当前筛选中的人工确认…', lambda: self.metadata_controller.backfill())
        batch_menu = menu.addMenu('批量人工确认（先选中多行）')
        batch_menu.addAction('选中项确认有马赛克…', lambda: self._batch_label(CensorState.CENSORED))
        batch_menu.addAction('选中项确认未见马赛克…', lambda: self._batch_label(CensorState.UNCENSORED))
        for text,callback in [('继续未完成任务',self._resume),('重试失败项',self._retry_failed),
                              ('选择延后视频…',self._choose_deferred)]:
            menu.addAction(text,callback)
        history = menu.addMenu('历史与重新分析')
        history.addAction('加载全部历史',self._load_history)
        history.addSeparator()
        history.addAction('忽略缓存，重新分析可见的未复核项',self._reanalyze_visible)
        menu.addSeparator()
        menu.addAction('检查设置…',self._show_settings)
        menu.addAction('使用说明',self._show_help)
        menu.addAction('清理过期无引用证据图', self._cleanup_evidence)
        menu.addAction('补抓当前筛选中缺图的视频', self._refresh_missing_evidence)
        menu.addAction('清空当前列表（保留记录）',self._clear)
        self.more_button.setMenu(menu)
        toolbar.addWidget(self.more_button)
        self.btn_stop = QPushButton('停止')
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        toolbar.addWidget(self.btn_stop)
        layout.addLayout(toolbar)

        self.progress_title = QLabel('尚未开始 · 添加文件或文件夹')
        self.progress_title.setWordWrap(True)
        layout.addWidget(self.progress_title)
        self.scan_progress = QProgressBar()
        self.scan_progress.setRange(0,100)
        self.scan_progress.setValue(0)
        self.scan_progress.setFixedHeight(16)
        layout.addWidget(self.scan_progress)
        self.progress_detail = QLabel('扫描时可滚动表格、筛选和查看已完成证据；分析完成不等于人工确认。')
        self.progress_detail.setWordWrap(True)
        self.progress_detail.setMaximumHeight(46)
        self.progress_detail.setProperty('cssClass','muted-label')
        layout.addWidget(self.progress_detail)
        self.data_status = QLabel(
            f'当前资料库：{self._service.store.db_path.resolve()} · 已保存扫描 / 人工确认：等待读取')
        self.data_status.setWordWrap(True)
        self.data_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.data_status.setProperty('cssClass', 'muted-label')
        layout.addWidget(self.data_status)

        splitter = QSplitter(Qt.Horizontal)
        self.splitter = splitter
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0,0,3,0)
        filter_row = QHBoxLayout()
        # 视图 = "我现在想干什么"；结论 = "有没有码"。两个维度各管一件事，
        # 不再用一个菜单同时表达结论、处理进度和业务动作（那是上一版的病根）。
        filter_row.addWidget(QLabel('视图：'))
        self.combo_filter = QComboBox()
        self.combo_filter.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo_filter.setToolTip('数量为当前其他筛选命中 / 此视图已加载总数；两者相同时只显示一个数')
        self.combo_filter.setProperty('taskInteractive',True)
        for view in WorkflowView:
            self.combo_filter.addItem(view.label, view.value)
        self.combo_filter.setCurrentIndex(self.combo_filter.findData(WorkflowView.ALL.value))
        self.combo_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.combo_filter)
        filter_row.addWidget(QLabel('结论：'))
        self.combo_conclusion = QComboBox()
        self.combo_conclusion.setProperty('taskInteractive',True)
        self.combo_conclusion.addItem('全部', '')
        for state, text in ((CensorState.CENSORED, '有码'),
                            (CensorState.UNCENSORED, '无码'),
                            (CensorState.UNKNOWN, '未定')):
            self.combo_conclusion.addItem(text, state.value)
        self.combo_conclusion.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.combo_conclusion)
        filter_row.addStretch()
        left_lay.addLayout(filter_row)
        library_filters = QHBoxLayout()
        self.combo_favorite = QComboBox()
        self.combo_favorite.setProperty('taskInteractive', True)
        for text, value in [('喜欢：全部', 'all'), ('只看喜欢', 'favorite'),
                            ('只看未喜欢', 'not_favorite'), ('喜欢未同步', 'unknown')]:
            self.combo_favorite.addItem(text, value)
        self.combo_playback = QComboBox()
        self.combo_playback.setProperty('taskInteractive', True)
        for text, value in [('播放：全部', 'all'), ('未播放', 'unplayed'),
                            ('播放中', 'progress'), ('已播放', 'played'), ('播放未同步', 'unknown')]:
            self.combo_playback.addItem(text, value)
        self.combo_library_sort = QComboBox()
        self.combo_library_sort.setProperty('taskInteractive', True)
        self.combo_library_sort.addItem('沿用表格排序', 'normal')
        self.combo_library_sort.addItem('喜欢优先', 'favorite_first')
        self.combo_library_sort.addItem('播放中优先', 'progress_first')
        for control in (self.combo_favorite, self.combo_playback):
            control.currentIndexChanged.connect(self._apply_filter)
            library_filters.addWidget(control)
        self.combo_library_sort.currentIndexChanged.connect(self._apply_library_sort)
        library_filters.addWidget(self.combo_library_sort)
        left_lay.addLayout(library_filters)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索文件名、路径、标识、Jellyfin 标签或类型…')
        self.search.setClearButtonEnabled(True)
        self.search.setProperty('taskInteractive', True)
        self.search.textChanged.connect(self._apply_filter)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.btn_clear_filters = QPushButton('清除筛选')
        self.btn_clear_filters.setProperty('taskInteractive', True)
        self.btn_clear_filters.setToolTip('清除搜索、结论、喜欢和播放条件；保留当前视图与排序，不删除记录')
        self.btn_clear_filters.clicked.connect(self._clear_scope_filters)
        search_row.addWidget(self.btn_clear_filters)
        left_lay.addLayout(search_row)
        self.table = _DropTable(0,len(_COLUMNS))
        self.table.setProperty('taskInteractive',True)
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setMinimumSectionSize(50)
        # 全部列都可手动拖动；用"最后一列拉伸"占满剩余，而不是把第 0 列设成
        # Stretch —— 那样文件名列宽度就被锁死、拖不动（这是之前拖不动的原因）。
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.setSectionsMovable(True)
        self.table.setColumnWidth(0,260)
        self.table.setColumnWidth(2,70)
        self.table.setColumnWidth(3,84)
        self.table.setColumnWidth(4,62)
        self.table.setColumnWidth(6,96)
        # 标识/结构分/依据 默认收起：结论与处理才是主线，其余是排查用的细节
        for column in (1,4,5):
            self.table.setColumnHidden(column,True)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.currentCellChanged.connect(lambda *_: self._on_row_selected())
        # 右键菜单兜底：按钮被布局挤走时也能标记（这是最符合直觉的第二个入口）
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_row_menu)
        self.table.files_dropped.connect(self._start_scan)
        self._table_shortcuts = []
        for key, callback in [('Ctrl+A', self.table.selectAll), ('Ctrl+C', self._copy_selection),
                              ('Ctrl+Shift+C', lambda: self._copy_selection(paths=True)),
                              ('Ctrl+F', lambda: self.search.setFocus()), ('Escape', self.table.clearSelection)]:
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self._table_shortcuts.append(shortcut)
        evidence_actions = QHBoxLayout()
        self.btn_refresh_evidence = QPushButton('补抓选中证据图')
        self.btn_refresh_evidence.setToolTip('支持 Ctrl / Shift 多选；只补充参考画面，保留检测结论和人工确认。忙碌时加入队列')
        self.btn_refresh_evidence.clicked.connect(self._refresh_selected_evidence)
        self.btn_deep_selected = QPushButton('深入检查选中项')
        self.btn_deep_selected.setToolTip('批量重新分析，增加取样和展示图片；后续轮次改变取样时刻，人工确认保留')
        self.btn_deep_selected.clicked.connect(self._deepen_current)
        for button in (self.btn_refresh_evidence, self.btn_deep_selected):
            button.setProperty('taskInteractive', True)
            button.setMinimumHeight(28)
            evidence_actions.addWidget(button)
        left_lay.addLayout(evidence_actions)
        left_lay.addWidget(self.table)
        splitter.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(3,0,0,0)
        review_header = QHBoxLayout()
        self.lbl_review_title = QLabel('选择一条结果查看证据')
        self.lbl_review_title.setWordWrap(True)
        self.lbl_review_title.setMinimumWidth(0)
        self.lbl_review_title.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        review_header.addWidget(self.lbl_review_title,1)
        current_menu = QPushButton('此视频')
        actions = QMenu(current_menu)
        actions.addAction('补抓选中证据图', self._refresh_selected_evidence)
        actions.addAction('深入检查选中项（更多画面）',self._deepen_current)
        actions.addAction('撤回当前人工标记', self._withdraw_current)
        actions.addAction('交给 Lada 处理', self._send_lada)
        current_menu.setMenu(actions)
        review_header.addWidget(current_menu)
        right_lay.addLayout(review_header)
        self.gallery = EvidenceGallery(self)
        self.gallery.setProperty('taskInteractive',True)
        self.gallery.setMinimumHeight(150)
        self._thumb_labels = self.gallery.labels
        right_lay.addWidget(self.gallery,4)
        self.txt_evidence = QPlainTextEdit()
        self.txt_evidence.setReadOnly(True)
        self.txt_evidence.setPlaceholderText('分析依据、覆盖数量和历史记录状态')
        self.txt_evidence.setMaximumHeight(100)
        right_lay.addWidget(self.txt_evidence)
        row = QHBoxLayout()
        # 这两个按钮是阶段一的**唯一产出**：给视频打上明确标记
        self.btn_censored = QPushButton('标记「有码」')
        self.btn_censored.setToolTip('确定这部片有马赛克 → 标记后自动进入「② 找替换」')
        self.btn_uncensored = QPushButton('标记「无码」')
        self.btn_uncensored.setToolTip('确定这部片没有马赛克 → 不需要找替换')
        for button,state,key,style,hint in [
                (self.btn_censored, CensorState.CENSORED, 'Alt+1', 'danger',
                 '确定有马赛克 → 标记后进入「② 找替换」'),
                (self.btn_uncensored, CensorState.UNCENSORED, 'Alt+2', 'accent',
                 '确定没有马赛克 → 不需要找替换')]:
            button.setShortcut(key)
            button.setToolTip(f'{key}；{hint}；保存后自动进入下一条')
            button.setProperty('cssClass',style)
            button.setProperty('taskReviewInteractive', True)
            button.clicked.connect(lambda _,value=state:self._confirm_selected(value))
            # 不禁用：没选中行时点击会给明确提示，比灰着让人猜更好
            button.setMinimumHeight(28)
            button.setMinimumWidth(104)
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            row.addWidget(button)
        next_button = QPushButton('下一条')
        self.btn_next = next_button
        next_button.setProperty('taskInteractive',True)
        next_button.setShortcut('Alt+3')
        next_button.clicked.connect(self._next_review)
        row.addWidget(next_button)
        right_lay.addLayout(row)
        # 顺序是"先确认有没有无码版，再替换"：所以这里只负责把选中项送去核查，
        # 替换入口在「替换版本检索」页。之前直接跳到版本管理，等于跳过了核查这一步。
        self.btn_send_versions = QPushButton('送去查无码版')
        self.btn_send_versions.setToolTip('把选中的作品送到「替换版本检索」页核查，只查这些')
        self.btn_send_versions.clicked.connect(self._send_to_lookup)
        right_lay.addWidget(self.btn_send_versions)
        splitter.addWidget(right)
        splitter.setSizes([580,460])
        layout.addWidget(splitter,1)
        self._stats_label = QLabel('就绪')
        self._stats_label.setWordWrap(True)
        self._stats_label.setProperty('cssClass','muted-label')
        layout.addWidget(self._stats_label)
        self.sync_status = QLabel('标签同步：人工确认后自动加入；“更多 → 标签同步”查看状态或补同步')
        self.sync_status.setWordWrap(True)
        self.sync_status.setProperty('cssClass', 'muted-label')
        layout.addWidget(self.sync_status)
        self.library_status = QLabel()
        self.library_status.setWordWrap(True)
        self.library_status.setProperty('cssClass', 'muted-label')
        layout.addWidget(self.library_status)

        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle('检查设置')
        outer_settings = QVBoxLayout(self.settings_dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        settings = QVBoxLayout(body)
        scroll.setWidget(body)
        outer_settings.addWidget(scroll)
        self.settings_dialog.resize(630, 640)
        settings.addWidget(QLabel('每个视频最多保留的证据图片数：'))
        self.evidence_count = QSpinBox()
        self.evidence_count.setRange(1,20)
        self.evidence_count.setValue(self.cfg.censor_candidates)
        settings.addWidget(self.evidence_count)
        settings.addWidget(QLabel('并行视频上限（实际并发受 CPU 和内存预算约束）：'))
        self.scan_workers = QSpinBox()
        self.scan_workers.setRange(1, 16)
        self.scan_workers.setValue(self.cfg.censor_workers)
        settings.addWidget(self.scan_workers)
        settings.addWidget(QLabel('检测内存预算（MiB）：'))
        self.memory_budget = QSpinBox()
        self.memory_budget.setRange(64, 8192)
        self.memory_budget.setValue(self.cfg.censor_memory_mb)
        settings.addWidget(self.memory_budget)
        self.verify_replacements = QCheckBox('媒体替换完成后自动检查新版本')
        self.verify_replacements.setChecked(self.cfg.censor_verify_replacements)
        settings.addWidget(self.verify_replacements)
        from ui.review_controls import ReviewFeatureSettings
        self.review_settings = ReviewFeatureSettings(self.cfg)
        settings.addWidget(self.review_settings)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close,
                                   parent=self.settings_dialog)
        buttons.button(QDialogButtonBox.Save).setText('保存并关闭')
        buttons.button(QDialogButtonBox.Close).setText('关闭')
        buttons.accepted.connect(self._save_settings)
        buttons.rejected.connect(self.settings_dialog.reject)
        outer_settings.addWidget(buttons)
        from ui.workspace_preferences import WorkspacePreferences
        self.workspace = WorkspacePreferences(self, 'pixelation', self.table, splitter=splitter,
            gallery=self.gallery, filters={'view': self.combo_filter, 'conclusion': self.combo_conclusion,
                                          'search': self.search, 'favorite': self.combo_favorite,
                                          'playback': self.combo_playback, 'library_sort': self.combo_library_sort})
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._header_sort_changed)
        if not self.workspace.state.get('jellyfin_columns_added'):
            self.table.setColumnHidden(7, False)
            self.table.horizontalHeader().moveSection(self.table.horizontalHeader().visualIndex(7), 1)
            self.table.setColumnWidth(7, 82)
            self.workspace.state['jellyfin_columns_added'] = True
        menu.addSeparator()
        layout_menu = self.workspace.menu(self.more_button)
        presets = layout_menu.addMenu('布局预设')
        for title, value in [('快速复核', 'review'), ('诊断与依据', 'diagnostic'), ('显示全部列', 'all')]:
            presets.addAction(title, lambda checked=False, name=value: self._apply_review_layout(name))
        remember = layout_menu.addAction('下次启动记住当前视图')
        remember.setCheckable(True)
        remember.setChecked(self.workspace.state.get('remember_view') is True)
        remember.toggled.connect(self._set_remember_view)
        layout_menu.aboutToShow.connect(lambda: remember.setChecked(self.workspace.state.get('remember_view') is True))
        menu.addMenu(layout_menu).setText('工作区布局与偏好')

    def _apply_review_layout(self, name):
        self._table_presenter.apply_layout(name)

    def _set_remember_view(self, enabled):
        self.workspace.state['remember_view'] = bool(enabled)
        self.workspace.save()

    def _show_settings(self):
        if not busy(self):
            self.evidence_count.setValue(self.cfg.censor_candidates)
            self.scan_workers.setValue(self.cfg.censor_workers)
            self.memory_budget.setValue(self.cfg.censor_memory_mb)
            self.verify_replacements.setChecked(self.cfg.censor_verify_replacements)
            self.review_settings.reload(self.cfg)
            self.settings_dialog.exec()

    def _save_settings(self):
        """设置项改的是内存里的 cfg，这里显式落盘 —— 不必等关程序。"""
        try:
            from copy import deepcopy
            updated = deepcopy(self.cfg)
            updated.censor_candidates = self.evidence_count.value()
            updated.censor_workers = self.scan_workers.value()
            updated.censor_memory_mb = self.memory_budget.value()
            updated.censor_verify_replacements = self.verify_replacements.isChecked()
            self.review_settings.apply(updated)
            updated.save()
            vars(self.cfg).update(vars(updated))
            from ui.services import events
            events.config_changed.emit()
            self._stats_label.setText('设置已保存')
        except (OSError, ValueError) as exc:
            self._stats_label.setText(f'设置保存失败：{exc}')
            return
        self.settings_dialog.accept()

    def _show_help(self):
        QMessageBox.information(self,'像素化检查',
            '默认复用通过文件身份、参数和算法版本校验的分析。\n'
            '复用不会自动确认结果：疑似项仍留待复核。历史快照须重新添加文件才校验当前版本。\n'
            '抽样低风险不证明全片没有像素化。Alt+1 标有码、Alt+2 标无码、Alt+3 下一条。\n'
            '更多菜单可恢复任务、选择延后视频、查看历史或忽略缓存重新分析。\n\n'
            '这件事分两步，刻意分开算：\n'
            '① 定标记 —— 你的任务只有一个：确认哪些片需要替换（红「标记有码」）。\n'
            '② 替换版本检索 —— 在左侧「替换版本检索」页里，对已标记的作品编号去外部站点查\n'
            '（JavDB / JavBus 直连；Supjav 站内优先、索引补充），可反复重查、可导出清单。\n'
            '这一页只管定标记；检索结果不在本页显示。')

    def _reanalyze_visible(self):
        paths = [self.table.item(row,0).data(Qt.UserRole) for row in range(self.table.rowCount())
                 if not self.table.isRowHidden(row)]
        paths = [path for path in paths if category(self._results[path]) not in ('manual','deferred')]
        if paths:
            self._start_scan(paths,force=True)
        else:
            self._stats_label.setText('当前没有可重新分析的未复核项；延后视频请从选择菜单手动勾选')

    # ── 首次打开：异步读取完整持久历史，保留已保存的筛选 ──
    def showEvent(self, event):  # noqa: N802 - Qt 约定
        super().showEvent(event)
        if self._restored:
            return
        self._restored = True
        self._startup_timer.start()

    def _restore_history_once(self):
        if self._startup_cancelled or getattr(self.window(), '_closing', False):
            return
        if busy(self):
            # An import or scan may already own this page. Never interrupt it,
            # and recheck results when it has finished before deciding to load.
            self._startup_timer.start()
            return
        if self._results:
            worker = _SavedHistoryWorker(self._service.store, records=False, parent=self)
            self._saved_stats_worker = worker
            worker.saved_stats.connect(self._show_saved_stats)
            worker.error.connect(self._on_log)
            start_task(self, worker, '读取像素化资料库计数')
            return
        self._load_history()

    def _show_saved_stats(self, stats):
        # 这一行以前只报 total，于是"界面 226 行 / 已保存 0 条"会同时出现，
        # 用户只能理解为数据丢了。现在把"完整 / 未完成"分开报，
        # 未完成的行也入库（见 censor/store.py::_write_result）。
        incomplete = getattr(stats, "incomplete", 0)
        detail = f"（其中 {incomplete} 条未跑完，重新扫描会重算）" if incomplete else ""
        self.data_status.setText(
            f'当前资料库：{self._service.store.db_path.resolve()} · '
            f'已入库 {stats.total} 条{detail} · 人工确认 {stats.labeled} 条')

    def _cancel_startup(self):
        self._restored = True
        self._startup_cancelled = True
        self._startup_timer.stop()

    def closeEvent(self, event):  # noqa: N802 - Qt 约定
        self._cancel_startup()
        super().closeEvent(event)

    def _restore_pending(self) -> None:
        """自动载入数据库里的待处理项。

        以前要手动「更多 → 加载历史记录」，关一次程序待办就等于消失了 ——
        这是"遗漏"最实际的来源。现在打开页面就自动接上，并直接把视图切到
        「待我处理」，你不需要记得上次停在哪。
        """
        if self._results:
            return
        try:
            pending = self._service.review_queue(limit=300)
        except Exception as exc:  # noqa: BLE001 - 读历史失败不该拦住页面
            self._stats_label.setText(f"读取待处理记录失败：{exc}")
            return
        if not pending:
            self._refresh_stats()
            return
        self._batching = True
        try:
            for result in pending:
                result.from_history = True
                self._on_item(result)
        finally:
            self._batching = False
        self.combo_filter.setCurrentIndex(
            self.combo_filter.findData(WorkflowView.TODO.value))
        self._apply_filter()
        self._refresh_stats()
        self._stats_label.setText(
            f"已自动载入 {len(pending)} 个待处理项（含历史记录）；"
            "视图切到「待我处理」，其它项在「全部」里")
        self._advance_review('')
        if self._current is None and self.table.rowCount():
            self.table.selectRow(0)
            self._on_row_selected()

    # ── 拖拽 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [os.path.normpath(u.toLocalFile()) for u in event.mimeData().urls()]
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._start_scan(paths)

    # ── 扫描 ──
    def _media_changed(self, paths):
        from censor.models import ProcessingStatus
        changed = set(paths)
        self._pending_results = deque(result for result in self._pending_results if result.path not in changed)
        for path in paths:
            self._media_epochs[path] = self._media_epochs.get(path, 0) + 1
            self._review_overrides.pop(path, None)
            if path in self._results:
                self._on_item(VideoVerdict(path=path, status=ProcessingStatus.PARTIAL,
                                          error='文件已变更，请重新检查新版本'))
        if self.cfg.censor_verify_replacements:
            self._pending_verifications.update(path for path in paths if os.path.isfile(path))
            from ui.services import after_stopped
            after_stopped(self, self._verify_changed)

    def _verify_changed(self):
        paths = sorted(self._pending_verifications)
        self._pending_verifications.clear()
        if paths:
            self._start_scan(paths, force=True)

    def _cleanup_evidence(self):
        if busy(self):
            return
        from ui.services import CallWorker
        self._cleanup_worker = CallWorker(lambda stopped: self._service.cleanup_thumbnails())
        self._cleanup_worker.result.connect(lambda result: self._flash(
            f"已清理 {result['removed_bytes'] / 1048576:.1f} MiB；有效证据保留"))
        self._cleanup_worker.error.connect(self._on_log)
        start_task(self, self._cleanup_worker, '清理过期证据图')

    def _import_library(self):
        if busy(self):
            return
        from copy import deepcopy
        from ui.services import CallWorker
        cfg = deepcopy(self.cfg)
        def collect(stopped):
            from utils import library_source
            items = library_source.collect(cfg, should_stop=stopped,
                exclude_keywords=cfg.exclude_path_keywords)
            paths = [item.path for item in items if item.path and os.path.isfile(item.path)]
            return dict(paths=list(dict.fromkeys(paths)), unavailable=len(items) - len(paths))
        worker = CallWorker(collect)
        self._library_worker = worker
        worker.result.connect(self._library_imported)
        worker.error.connect(self._on_log)
        start_task(self, worker, '从当前媒体库收集检测范围')

    def _library_imported(self, data):
        self._stats_label.setText(f"可访问 {len(data['paths'])} 个视频；无本地映射/不可访问 {data['unavailable']} 个")
        if data['paths']:
            from ui.services import after_stopped
            after_stopped(self, lambda: self._start_scan(data['paths']))

    def queued_paths(self) -> list[str]:
        """已提交去检测的文件（按提交顺序）。供自动化测试与外部脚本查询。"""
        return list(self._submitted)

    def _collect_files(self, paths: list[str]) -> list[str]:
        """把拖进来的文件/文件夹展开成"确实是视频"的文件列表。"""
        from utils.file_scan import iter_media_files

        return list(iter_media_files(
            paths,
            video_exts={e.lower() for e in self.cfg.video_extensions},
            exclude_keywords=self.cfg.exclude_path_keywords,
        ))

    def _start_scan(self, paths: list[str], *, manual_deferred=False, force=False, deep=False,
                    evidence_only=False, missing_only=False) -> None:
        # 运行中再拖进来：不许替换正在跑的线程（历史 bug：新线程顶掉旧线程 → 漏扫）
        if self._worker is not None and self._worker.isRunning():
            self._stats_label.setText("扫描进行中，请等待完成后再添加")
            return
        if busy(self):
            self._stats_label.setText("已有任务在跑，先等它结束")
            return

        files = [str(p) for p in paths]
        if not files:
            self._stats_label.setText("没有新增视频可检测")
            return

        options = CensorOptions(
            detect_visual=(not evidence_only and (deep or bool(self.cfg.censor_visual_enabled))), force=force,
            recheck_manual=deep,
            frame_samples=min(600, self.cfg.censor_frame_samples * 3) if deep else None,
            evidence_frames=(24 if evidence_only else
                             min(60, max(24, self.cfg.censor_candidates * 2)) if deep else None),
            evidence_only=evidence_only,
        )
        from copy import copy, deepcopy
        from ui.services import CallWorker
        service = copy(self._service)
        service.cfg = deepcopy(self.cfg)
        if deep:
            service.cfg.censor_visual_enabled = True
            if 'visual.mosaic_cv' not in service.cfg.censor_detectors:
                service.cfg.censor_detectors = [*service.cfg.censor_detectors, 'visual.mosaic_cv']
        bases = ({path: deepcopy(self._results[path]) for path in files if path in self._results}
                 if evidence_only else {})
        self._finishing = False
        self._set_running(True)
        self.progress_title.setText('后台校验文件身份与缓存…')
        def prepare(stopped):
            if evidence_only:
                from censor.evidence_refresh import has_evidence
                return ([path for path in files if not stopped() and
                    (not missing_only or path not in bases or not has_evidence(bases[path]))], [])
            return self._partition_reusable(files, options, should_stop=stopped, service=service)
        worker = CallWorker(prepare)
        worker.evidence_bases = bases
        worker.delivery_context = self._scan_context()
        worker.media_epochs = dict(self._media_epochs)
        worker.analysis_service = service
        worker.scan_options = options
        worker.manual_deferred = manual_deferred
        self._prepare_worker = worker
        worker.delivered = False
        worker.result.connect(self._prepared_result)
        worker.finished.connect(self._preparation_finished)
        worker.error.connect(self._on_log)
        start_task(self, worker, '校验检测缓存与输入')

    def _scan_context(self):
        import json
        from ui.services import state_context
        return json.dumps((self._service._analysis_config(), self.cfg.merge_ffmpeg_path,
                           state_context(self.cfg)), sort_keys=True, default=str)

    @Slot(object)
    def _prepared_result(self, partition):
        worker = self.sender()
        worker.delivered = True
        if worker.delivery_context != self._scan_context() or worker._stopped:
            self._set_running(False)
            self._stats_label.setText('配置或范围已变更，旧准备结果已丢弃，请重新开始')
            return
        todo, reused = partition
        for result in reused:
            result._delivery_context = worker.delivery_context
            result._media_epoch = worker.media_epochs.get(result.path, 0)
        def valid(path):
            return worker.media_epochs.get(path, 0) == self._media_epochs.get(path, 0)
        self._prepared_scan(([p for p in todo if valid(p)], [r for r in reused if valid(r.path)]),
                            worker.scan_options, worker.manual_deferred, service=worker.analysis_service,
                            evidence_bases=getattr(worker, 'evidence_bases', None))

    @Slot()
    def _preparation_finished(self):
        if not self.sender().delivered:
            self._set_running(False)

    def _prepared_scan(self, partition, options, manual_deferred, *, service=None, evidence_bases=None):
        todo, reused = partition
        if reused:
            self._on_discovered([result.path for result in reused])
            for result in reused:
                self._queue_item(result)
            self._refresh_stats()
        if not todo:
            self._set_running(False)
            if options.evidence_only:
                self._flash('当前筛选范围内没有缺图视频，无需补抓')
                return
            self._stats_label.setText(
                f"{len(reused)} 个视频沿用上次结果，本次无需重查"
                "（要强制重算请用「更多 → 忽略缓存重新分析」）")
            return

        files = todo
        self._set_running(True)
        self._finishing = False
        self._worker = CensorScanWorker(
            service or self._service, files, options,
            workers=int(getattr(self.cfg, "censor_workers", 4) or 4),
            manual_deferred=manual_deferred,
            evidence_bases=evidence_bases,
        )
        self._worker.delivery_context = self._scan_context()
        self._worker.media_epochs = dict(self._media_epochs)
        self._worker.item_done.connect(self._queue_scan_item)
        self._worker.discovered.connect(self._on_discovered)
        self._worker.task_status.connect(self._on_task_status)
        self._worker.error.connect(self._on_log)
        self._worker.all_done.connect(self._on_all_done)
        start_task(self, self._worker, '批量补抓证据图' if options.evidence_only else "视频像素化检测")

    def _partition_reusable(self, files: list[str], options: CensorOptions, *, should_stop=None, service=None):
        """把"已有有效结果"的文件挑出来，其余才是要真算的。

        这是"不重复检查"的落点：拖同一批片子进来不会从头再扫一遍，
        但文件被换过（大小/修改时间变）、参数或插件版本变过，签名就不匹配，
        照样会重算 —— 不会因为"有缓存"而漏掉该更新的结果。
        """
        from censor.scan_plan import partition_reusable
        return partition_reusable(service or self._service, files, options,
                                  jobs=self._jobs, should_stop=should_stop)

    def _on_discovered(self, paths):
        worker = self.sender()
        context = getattr(worker, 'delivery_context', None)
        if context is not None and context != self._scan_context():
            return
        seen = set(self._submitted)
        for path in paths:
            if path not in seen:
                self._submitted.append(path)
                seen.add(path)

    def _deepen_current(self):
        paths = self._selected_visible_paths()
        if paths:
            self._queue_evidence_request(paths, deep=True)
        else:
            self._flash('先选择要深入检查的视频；支持 Ctrl / Shift 多选后一次提交')

    def _refresh_selected_evidence(self):
        self._queue_evidence_request(self._selected_visible_paths())

    def _refresh_missing_evidence(self):
        paths = [self.table.item(row, 0).data(Qt.UserRole)
            for row in range(self.table.rowCount()) if not self.table.isRowHidden(row)]
        self._queue_evidence_request(paths, missing_only=True)

    def _queue_evidence_request(self, paths, *, deep=False, missing_only=False):
        paths = list(dict.fromkeys(paths))
        if not paths:
            self._flash('先选择视频；支持 Ctrl / Shift 多选，或从“更多”补抓当前筛选范围中的缺图项')
            return
        from ui.services import TaskManager
        TaskManager.instance().track_pending_owner(self)
        epoch = getattr(self, '_task_epoch', 0)
        if epoch != self._evidence_queue_epoch:
            self._evidence_requests.clear()
        self._evidence_queue_epoch = epoch
        pending = {p for request in self._evidence_requests
                   if request[0:2] == (deep, missing_only) for p in request[2]}
        paths = [path for path in paths if path not in pending]
        if paths:
            self._evidence_requests.append((deep, missing_only, paths))
        self._flash(f'已提交{len(paths)}个视频到本地{"深检" if deep else "补图"}队列；可继续选择其他视频，停止会取消未开始项')
        self._pump_evidence_requests()

    def _pump_evidence_requests(self):
        if self._evidence_queue_epoch != getattr(self, '_task_epoch', 0):
            self._evidence_requests.clear()
            self._evidence_queue_timer.stop()
            return
        if not self._evidence_requests or getattr(self.window(), '_closing', False):
            return
        dispatch = getattr(self, '_task_dispatch', None)
        if busy(self) or self._render_timer.isActive() or (dispatch and dispatch.pending):
            self._evidence_queue_timer.start()
            return
        deep, missing_only, paths = self._evidence_requests.pop(0)
        kwargs = dict(manual_deferred=True, force=True, deep=True) if deep else dict(
            force=True, evidence_only=True, missing_only=missing_only)
        self._start_scan(paths, **kwargs)
        if self._evidence_requests:
            self._evidence_queue_timer.start()

    def _advance_review(self, after_path, *, positions=None):
        """跳到**表格里实际可见的**下一个待复核项。

        以前这里还会再套一层 `_matches_filter`（按视图/喜欢/播放状态二次过滤）。
        表格本身已经按这些条件筛过了，再筛一遍会得出"当前视图没有待复核视频"——
        而用户眼前的列表里明明还有几百条。视图被记成"暂不处理"时尤其明显：
        标记完就不动了，只能回左边列表重新点。
        """
        if positions is None:
            positions = self._table_presenter.ordered_paths()
        visible = {self.table.item(r, 0).data(Qt.UserRole)
                   for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)}

        def is_candidate(path):
            result = self._results.get(path)
            # The current visible scope is the user's review choice. Parked
            # low-risk/deferred rows still need a human conclusion when opened.
            return result is not None and review_bucket(result) is not ReviewBucket.CONFIRMED

        target = next_review_target(positions, visible, after_path, is_candidate)
        if target:
            row = self._table_presenter.row_for(target)
            self.table.selectRow(row)
            self._on_row_selected()
            self.table.scrollToItem(self.table.item(row, 0))
            return
        self._clear_current_review('当前筛选中没有其他未人工确认的视频；可切换筛选或加载全部历史')

    def _next_review(self):
        path = self._current.path if self._current else ''
        if not path:
            row = self.table.currentRow()
            item = self.table.item(row, 0) if row >= 0 else None
            path = item.data(Qt.UserRole) if item else ''
        self._advance_review(path)

    def _clear_current_review(self, message='请选择当前筛选范围内的视频'):
        self._current = None
        self.table.clearSelection()
        self.txt_evidence.clear()
        self.lbl_review_title.setText(message)
        self.gallery.show_candidates([])

    def _choose_deferred(self):
        if busy(self):
            return
        rows = list(dict(self._jobs.deferred() + [
            (result.path, '历史文件名有无码/破解线索；可手动重新检查')
            for result in self._results.values() if category(result) == 'deferred'
        ]).items())
        if not rows:
            self._stats_label.setText('没有延后视频；普通任务完成后也不会自动检查这类视频')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f'延后视频（{len(rows)} 个）—仅检查你勾选的项目')
        dialog.resize(900, 480)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('延后任务及当前加载的名称延后历史。线索不是画面结论。默认全部不选。'))
        table = QTableWidget(len(rows), 2, dialog)
        table.setHorizontalHeaderLabels(['视频（勾选后检查）', '延后依据'])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for index, (path, note) in enumerate(rows):
            item = QTableWidgetItem(path)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            table.setItem(index, 0, item)
            table.setItem(index, 1, QTableWidgetItem(note))
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog)
        buttons.button(QDialogButtonBox.Ok).setText('检查勾选项目')
        buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        table.itemChanged.connect(lambda _: buttons.button(QDialogButtonBox.Ok).setEnabled(
            any(table.item(r, 0).checkState() == Qt.Checked for r in range(table.rowCount()))))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            selected = [rows[r][0] for r in range(len(rows))
                        if table.item(r, 0).checkState() == Qt.Checked]
            if selected:
                self._start_scan(selected, manual_deferred=True)

    def _resume(self):
        if busy(self):
            return
        pending = list(dict.fromkeys(self._jobs.unfinished_sources() + self._jobs.pending()))
        if not pending:
            self._stats_label.setText('没有未完成任务')
            return
        self._start_scan(pending)

    def _retry_failed(self):
        if not busy(self):
            self._jobs.retry_failed()
            self._resume()

    def _load_history(self):
        if busy(self):
            return
        self._cancel_startup()
        self._clear()
        self._finishing = False
        self._history_offset = 0
        self._history_loading = True
        self._history_worker = _SavedHistoryWorker(self._service.store, parent=self)
        self._history_worker.saved_stats.connect(self._show_saved_stats)
        self._history_worker.page_ready.connect(self._on_history_loaded)
        self._history_worker.result.connect(self._on_history_finished)
        self._history_worker.error.connect(self._on_log)
        self.progress_title.setText('正在加载全部历史…')
        self.scan_progress.setRange(0, 0)
        self._set_running(True)
        if not start_task(self,self._history_worker,'加载全部像素化历史'):
            self._history_loading = False
            self._set_running(False)

    def _on_history_loaded(self, rows):
        sender = self.sender()
        if isinstance(sender, HistoryLoadWorker) and (
                sender is not self._history_worker or not self._history_loading):
            return
        total = rows.get('total', rows.get('next_offset', 0)) if isinstance(rows, dict) else len(rows)
        if isinstance(rows, dict):
            self._history_offset = rows['next_offset']
            rows = rows['records']
        else:  # Compatibility for callers delivering the original list API.
            self._history_offset += len(rows)
        for result in rows:
            result.from_history = True
            self._queue_item(result)
        self.progress_title.setText(f'正在加载历史 · 已读取 {self._history_offset}/{total} 条，尚未校验当前文件')
        self.scan_progress.setRange(0,max(1, total))
        self.scan_progress.setValue(self._history_offset)
        self.scan_progress.setFormat('%p%')
        self.progress_detail.setText('历史成功只表示当时分析完成；疑似项仍需复核。重新添加文件可校验版本并复用或重算。')
        if self._history_loading and not self._pending_results:
            self._history_worker.acknowledge()

    def _on_history_finished(self, summary):
        if self.sender() is not self._history_worker:
            return
        self._history_loading = False
        self._set_running(False)
        loaded = len(self._results)
        if summary.get('error'):
            title = f'历史读取失败 · 已保留 {loaded} 条'
        elif summary.get('cancelled'):
            title = f'历史加载已停止 · 已加载 {loaded} 条，记录仍保存在数据库'
        else:
            skipped = summary['read'] - loaded
            title = f'历史已全部加载 · {loaded} 条，尚未校验当前文件'
            if skipped:
                title += f' · 跳过 {skipped} 条损坏记录'
        self.progress_title.setText(title)
        self.scan_progress.setRange(0, max(1, summary['total']))
        value = loaded if summary.get('cancelled') else summary['read']
        if not summary.get('cancelled') and not summary.get('error'):
            value = max(1, value)
        self.scan_progress.setValue(value)
        self._refresh_stats()

    def _stop(self) -> None:
        self._cancel_startup()
        self._evidence_requests.clear()
        self._evidence_queue_timer.stop()
        self.metadata_controller.pause()
        if self._history_loading:
            self._history_loading = False
            self._pending_results.clear()
            self._render_timer.stop()
        stop_page(self)
        self.progress_title.setText('已请求停止，正在收尾…')

    def _set_running(self, running: bool) -> None:
        self.btn_stop.setEnabled(running)

    def shutdown(self) -> None:
        self._cancel_startup()
        self._evidence_requests.clear()
        self._evidence_queue_timer.stop()
        self.library_review.shutdown()
        self.metadata_controller.pause()
        if self._history_loading:
            self._history_loading = False
            self._pending_results.clear()
            self._render_timer.stop()
        self.workspace.save()
        if self._conn_check is not None and self._conn_check.isRunning():
            self._conn_check.stop()
            self._conn_check.wait(3000)
        stop_page(self)

    # ── Batched delivery keeps cache-heavy imports responsive. ──
    @Slot(object)
    def _queue_scan_item(self, result):
        worker = self.sender()
        if (worker.delivery_context != self._scan_context()
                or worker.media_epochs.get(result.path, 0) != self._media_epochs.get(result.path, 0)):
            return
        # Keep the context with queued render items as well as at signal receipt.
        result._delivery_context = worker.delivery_context
        result._media_epoch = worker.media_epochs.get(result.path, 0)
        self._queue_item(result)

    def _queue_item(self, result):
        self._pending_results.append(result)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _drain_results(self):
        self._batching = True
        self.table.setUpdatesEnabled(False)
        self.table.setSortingEnabled(False)
        try:
            for _ in range(min(40,len(self._pending_results))):
                self._on_item(self._pending_results.popleft())
        finally:
            self._batching = False
            self.table.setSortingEnabled(True)
            self.table.setUpdatesEnabled(True)
        self._apply_filter()
        if not self._pending_results:
            self._render_timer.stop()
            self._finish_batch_review_navigation()
            if self._history_loading:
                self._history_worker.acknowledge()
            if self._finishing:
                self._finish_view()

    def _on_item(self, result: VideoVerdict) -> None:
        if (getattr(result, '_delivery_context', self._scan_context()) != self._scan_context()
                or getattr(result, '_media_epoch', self._media_epochs.get(result.path, 0))
                != self._media_epochs.get(result.path, 0)):
            return
        reviewed = self._review_overrides.get(result.path)
        if reviewed is not None:
            if result.file_identity and reviewed.file_identity == result.file_identity:
                manual = reviewed.verdict.of(EvidenceSource.MANUAL)
                others = [ev for ev in result.verdict.evidence if ev.source is not EvidenceSource.MANUAL]
                result.verdict = fuse([manual] + others)
            elif result.file_identity:
                # A replacement file never inherits the older file's review.
                self._review_overrides.pop(result.path,None)
        previous = self._results.get(result.path)
        if previous is not None:
            self._category_counts[category(previous)] -= 1
        self._results[result.path] = result
        self._category_counts[category(result)] += 1
        if previous is not None:
            self._replace_row(result)
        else:
            self._insert_row(result)
        if self._current is not None and self._current.path == result.path:
            self._current = result
            self._on_row_selected()

    @staticmethod
    def _record_source(result):
        if result.from_history:
            return '历史快照'
        return '复用分析' if result.cache_hit else '本次结果'

    def _insert_row(self, result: VideoVerdict) -> None:
        self._table_presenter.present(result)

    def _on_progress(self, done: int, total: int) -> None:
        self.scan_progress.setRange(0,max(1,total))
        self.scan_progress.setValue(done)
        self.progress_title.setText(f'分析视频 · {done}/{total}')

    def _on_task_status(self, status):
        self._last_status = status
        self.scan_progress.setFormat('%p%')
        phase = status.get('phase','分析视频')
        fraction = (f' · {status.get("completed",0)}/{status.get("total",0)}'
                    if status.get('progress') is not None else '')
        self.progress_title.setText(f'{phase}{fraction}'
            f' · 已发现 {status.get("discovered",0)} · 复用 {status.get("cached",0)}'
            f' · 失败 {status.get("failed",0)} · 延后 {status.get("deferred",0)}')
        if status.get('progress') is None:
            self.scan_progress.setRange(0,0)
        else:
            self.scan_progress.setRange(0,100)
            self.scan_progress.setValue(status['progress'])
        active = status.get('active',[])
        detail = '\n'.join(f'{os.path.basename(a["path"])} · {a["stage"]}' +
                          (f' {a["done"]}/{a["total"]}' if a['total'] else '') for a in active)
        self.progress_detail.setText(detail or status.get('detail',''))
        self.progress_detail.setToolTip(status.get('detail',''))

    def _on_log(self, message: str) -> None:
        self.progress_detail.setText(message)
        self.progress_detail.setToolTip(message)

    def _on_all_done(self, count: int) -> None:
        self._finishing = True
        if self._pending_results:
            self._drain_results()
        else:
            self._finish_view()

    def _finish_view(self):
        self._finishing = False
        self._set_running(False)
        self._refresh_stats()

    def _current_view(self) -> WorkflowView:
        data = self.combo_filter.currentData()
        return WorkflowView(data) if data else WorkflowView.TODO

    def _matches_filter(self, result) -> bool:
        """视图（我现在想干什么）× 结论（有没有码）—— 两个维度独立的 AND。"""
        if not self._matches_scope(result):
            return False
        view = self._current_view()
        return view is WorkflowView.ALL or in_view(view, review_bucket(result))

    def _scope_query(self):
        return ReviewQuery.from_values(self.search.text(), self.combo_conclusion.currentData(),
                                       self.combo_favorite.currentData(), self.combo_playback.currentData())

    def _matches_scope(self, result, query=None) -> bool:
        """The shared favorite/playback/conclusion/search scope, without view."""
        item = self.library_review.item(result.path)
        return (query or self._scope_query()).matches(result.path, result.number, result.state.value,
                                                      result.verdict.reason, item)

    def _clear_scope_filters(self):
        controls = (self.search, self.combo_conclusion, self.combo_favorite, self.combo_playback)
        blockers = [QSignalBlocker(control) for control in controls]
        self.search.clear()
        for control in controls[1:]:
            control.setCurrentIndex(0)
        for blocker in blockers:
            blocker.unblock()
        self._apply_filter()
        self.workspace.schedule()

    def _set_library_cells(self, row, path):
        item = self.library_review.item(path)
        favorite = favorite_state(item)
        cell = _FavoriteItem({'favorite': '♥ 喜欢', 'not_favorite': '未喜欢', 'unknown': '未同步'}[favorite])
        cell.setData(Qt.UserRole + 1, {'favorite': 2, 'not_favorite': 1, 'unknown': 0}[favorite])
        cell.setData(Qt.UserRole + 2, str(path).casefold())
        cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
        cell.setToolTip('所选 Jellyfin 账号的状态；未匹配或未同步保持未知')
        self.table.setItem(row, 7, cell)
        played = playback_state(item)
        cell = _FavoriteItem({'unknown': '未同步', 'played': '已播放', 'progress': '播放中', 'unplayed': '未播放'}[played])
        cell.setData(Qt.UserRole + 1, {'unknown': 0, 'played': 1, 'unplayed': 2, 'progress': 3}[played])
        cell.setData(Qt.UserRole + 2, str(path).casefold())
        cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, 8, cell)

    def _apply_library_sort(self):
        if not hasattr(self, 'table'):
            return
        header = self.table.horizontalHeader()
        mode = self.combo_library_sort.currentData()
        if mode in ('favorite_first', 'progress_first'):
            if self._normal_sort is None:
                self._normal_sort = (header.sortIndicatorSection(), header.sortIndicatorOrder())
            self._setting_library_sort = True
            try:
                self.table.sortItems(7 if mode == 'favorite_first' else 8, Qt.DescendingOrder)
            finally:
                self._setting_library_sort = False
        elif self._normal_sort is not None:
            column, order = self._normal_sort
            self._normal_sort = None
            self.table.sortItems(column, order)

    def _header_sort_changed(self, *_):
        if self._setting_library_sort or self.combo_library_sort.currentData() == 'normal':
            return
        self._normal_sort = None
        with QSignalBlocker(self.combo_library_sort):
            self.combo_library_sort.setCurrentIndex(self.combo_library_sort.findData('normal'))
        self.workspace.schedule()

    def _show_favorite_censored(self):
        self.combo_filter.setCurrentIndex(self.combo_filter.findData(WorkflowView.ALL.value))
        self.combo_conclusion.setCurrentIndex(self.combo_conclusion.findData(CensorState.CENSORED.value))
        self.combo_favorite.setCurrentIndex(self.combo_favorite.findData('favorite'))
        self._load_history()

    def _selected_visible_paths(self):
        return self._table_presenter.selected_paths()

    def _copy_selection(self, *, paths=False):
        from PySide6.QtWidgets import QApplication
        selected = self._selected_visible_paths()
        QApplication.clipboard().setText('\n'.join(selected if paths else [os.path.basename(p) for p in selected]))

    def _send_to_lookup(self):
        paths = self._selected_visible_paths()
        if not paths and self._current and self._matches_filter(self._current):
            paths = [self._current.path]
        if not paths:
            self._flash('先选中要核查的视频（可以先按「喜欢 / 结论」筛一遍，再全选）')
            return
        from ui.services import events
        events.release_lookup_requested.emit(list(dict.fromkeys(paths)))
        self._flash(f'已把 {len(paths)} 个作品送去「替换版本检索」核查；还没查、也没动文件')

    def set_review_shortcut(self, name, sequence):
        button = getattr(self, {'censored': 'btn_censored', 'uncensored': 'btn_uncensored', 'next': 'btn_next'}[name])
        button.setShortcut(QKeySequence())
        old = self._review_shortcuts.pop(name, None)
        if old is not None:
            old.setEnabled(False)
            old.deleteLater()
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(lambda: self._run_review_shortcut(name))
        self._review_shortcuts[name] = shortcut
        button.setToolTip(f'{QKeySequence(sequence).toString()} · 焦点在复核工作区时可用；输入文字时不确认')

    def _run_review_shortcut(self, name):
        from PySide6.QtWidgets import QApplication
        if isinstance(QApplication.focusWidget(), (QLineEdit, QPlainTextEdit)):
            return
        if name == 'next':
            self._next_review()
        else:
            button = self.btn_censored if name == 'censored' else self.btn_uncensored
            if button.isEnabled():
                state = CensorState.CENSORED if name == 'censored' else CensorState.UNCENSORED
                self._confirm_selected(state)

    def _confirm_selected(self, state):
        if len(self._selected_visible_paths()) > 1:
            self._batch_label(state)
        else:
            self._label(state)

    def on_config_changed(self):
        from ui.services import refresh_configuration
        refresh_configuration(self)
        self.library_review.reload()

    def _apply_filter(self) -> None:
        projection = self._filter_projection()
        view = self._current_view()
        for row in range(self.table.rowCount()):
            item = self.table.item(row,0)
            membership = projection['membership'].get(item.data(Qt.UserRole)) if item else None
            if membership is not None:
                value, matches = membership
                self.table.setRowHidden(row, not matches or not in_view(view, value))
        if self._current is not None and not self._matches_filter(self._current):
            if self._selected_visible_paths():
                self._on_row_selected()
            else:
                self._clear_current_review()
        self._render_stats(projection)

    # ── 复核 ──
    def _on_row_selected(self) -> None:
        presenter = getattr(self, '_table_presenter', None)
        if presenter is None:
            return
        path = presenter.active_path()
        if path is None:
            if self._current is not None:
                self._clear_current_review()
            return
        result = self._results.get(path)
        if result is None:
            return
        self._current = result
        self.lbl_review_title.setText(
            f"{os.path.basename(result.path)}\n"
            f"结论：{conclusion_label(result)} · 处理：{bucket_label(result)} · "
            f"结构分 {result.visual_score:.1f} · "
            f"耗时 {result.elapsed:.0f}s")
        self._show_thumbs(result)
        lines = [f"{src}: {text}" for src, text in explain(result.verdict)]
        lines.insert(0, f'处理：{result.status.label}；已分析/已抽样 '
                     f'{result.analyzed_frames}/{result.sampled_frames} 帧；抽样不等于全片证明')
        if result.path:
            lines.insert(1, '调度线索：' + (result.scheduling_note or '历史记录暂无调度线索'))
        lines.insert(0,'记录来源：'+self._record_source(result)+'；分析完成不等于人工确认')
        for run in result.detector_runs.values():
            if run.get('status') == 'retained_manual':
                lines.append('已有当前版本人工确认，保留原始视觉证据；需要新分析可用深入检查。')
            if run.get('evidence_target'):
                lines.append(f'深入检查第{run.get("evidence_round", 1)}轮：计划保留最多'
                    f'{run["evidence_target"]}张画面；本轮改变分层取样时刻，重复内容仍会去重。')
            plan = run.get('triage') or {}
            if plan.get('enabled'):
                lines.append(f"分组快速检查：{plan.get('group', '')} · {plan.get('sampled_samples', result.sampled_frames)}/{plan.get('planned_samples', '')} 个计划点 · "
                             + ('已补检' if plan.get('refined') else '首轮低风险，非人工确认'))
        capture = result.detector_runs.get('review.frames')
        if isinstance(capture, dict):
            lines.append(f'参考补图第{capture.get("round", 1)}轮：取样{capture.get("sampled", 0)}帧，'
                f'新增{capture.get("added", 0)}张，重复{capture.get("duplicates", 0)}帧；'
                f'用时{capture.get("elapsed", 0):.1f}秒；仅补充画面，原检测和人工结论保留。')
            if capture.get('error'):
                lines.append('补图提示：' + capture['error'])
        lines.append(f'当前保留{len(result.candidates)}张证据/参考画面。')
        if result.from_history:
            lines.insert(1,'历史快照尚未校验当前文件；重新添加后将校验文件身份、参数及算法版本。')
        if result.error:
            lines.append(f"错误: {result.error}")
        self.txt_evidence.setPlainText("\n".join(lines) or "（没有可用依据）")

    def _show_row_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        if item.data(Qt.UserRole) in self._selected_visible_paths():
            self.table.selectionModel().setCurrentIndex(self.table.model().index(row, 0),
                QItemSelectionModel.NoUpdate)
        else:
            self.table.selectRow(row)
        self._on_row_selected()
        menu = QMenu(self)
        action = menu.addAction("标记「有码」", lambda: self._confirm_selected(CensorState.CENSORED))
        action.setEnabled(self.btn_censored.isEnabled())
        action = menu.addAction("标记「无码」", lambda: self._confirm_selected(CensorState.UNCENSORED))
        action.setEnabled(self.btn_uncensored.isEnabled())
        menu.addSeparator()
        menu.addAction('补抓选中证据图', self._refresh_selected_evidence)
        menu.addAction('深入检查选中项（更多画面）', self._deepen_current)
        menu.addAction('补抓当前筛选中缺图的视频', self._refresh_missing_evidence)
        menu.addAction("下一条", self._next_review)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _show_thumbs(self, result: VideoVerdict) -> None:
        self.gallery.show_candidates(result.candidates)

    def _flash(self, text: str) -> None:
        """把操作反馈显示在进度标题上。

        **不能写 stats_label**：``_label`` 结尾会调 ``_refresh_stats()``，
        它会把 stats_label 整段覆盖掉 —— 那样点了按钮就跟没点一样。
        """
        self.progress_title.setText(text)
        self._flash_timer.start()

    def _restore_progress_title(self) -> None:
        """8 秒后把进度标题恢复成常态。

        用成员 QTimer 而不是 ``QTimer.singleShot``：后者没有生命周期绑定，
        页面销毁后仍会回调，会访问已删除的控件（测试里表现为跨用例报错）。
        """
        if self._worker is not None and self._worker.isRunning():
            return
        self.progress_title.setText(f'列表 {len(self._results)} 条 · 选中一项可标记')

    def _resolve_current(self):
        """Only an explicitly selected visible row may receive a human label."""
        self._on_row_selected()
        return self._current

    def _label(self, state: CensorState) -> None:
        current = self._resolve_current()
        if current is None:
            total = len(self._results)
            if any(not self.table.isRowHidden(row) for row in range(self.table.rowCount())):
                self._flash('请先在表格中选中要确认的视频；当前未保存任何标记。可点“下一条”定位未确认项')
                return
            self._flash(
                '没有可标记的行：当前视图'
                f'（{self.combo_filter.currentText()}）里一行都没有'
                + (f'，共 {total} 条被筛掉了 —— 切到「全部记录」看看' if total else
                   '。先拖入视频检查，或点「更多 → 历史与重新分析 → 加载全部历史」'))
            return
        self._current = current
        path = current.path
        # A confirmation may move this row under conclusion/status sorting.
        # Continue from the order the user was reviewing before that mutation.
        review_order = self._table_presenter.ordered_paths()
        try:
            refreshed = self._service.label(path, state, "界面人工确认", base=current)
        except ValueError as exc:
            # 文件被替换过：旧的人工结论不能套到新文件上
            self._flash(f'标记失败：{exc} —— 请重新检查这部片再标记')
            self.txt_evidence.appendPlainText(f'无法确认：{exc}')
            return
        except OSError as exc:
            self._flash(
                f'标记失败：读不到文件（{exc}）\n'
                '文件可能被移动或删除，请把它重新拖进来检查')
            self.txt_evidence.appendPlainText(f'无法确认：{exc}')
            return
        if refreshed is not None:
            self._review_overrides[path] = refreshed
            self._on_item(refreshed)
            if state is CensorState.CENSORED:
                # 标记完成 = 进入阶段二。这里只**入队**，不联网：
                # 真正去查是在「替换版本检索」页点检索（可以反复重查）。
                number = normalize_number(refreshed.number)
                tail = " —— 已进入「替换版本检索」清单"
                if number:
                    try:
                        self._release_service.queue([number], note="界面标记有码")
                    except Exception as exc:  # noqa: BLE001 - 排队失败不影响标记
                        tail = f" —— 入队失败（{exc}）"
                else:
                    tail = " —— 没有番号，无法按番号检索"
                self._flash(f"已标记「有码」：{os.path.basename(path)}{tail}")
            else:
                self._flash(
                    f"已标记「无码」：{os.path.basename(path)} —— 不需要找替换")
            from ui.services import events
            events.censor_changed.emit([path])
            message = getattr(self._service, 'last_metadata_sync_error', '')
            if message:
                self.sync_status.setText(message)
            else:
                self.metadata_controller.schedule()
        self._refresh_stats()
        if self.workspace.state.get('auto_next', True):
            self._advance_review(path, positions=review_order)

    def _withdraw_current(self):
        current = self._resolve_current()
        if current is None:
            self._flash('先选择一条已人工标记的记录')
            return
        try:
            result = self._service.remove_label(current.path)
        except (OSError, ValueError) as exc:
            self._flash(f'撤回失败：{exc}')
            return
        self._review_overrides.pop(current.path, None)
        if result is None:
            result = VideoVerdict(path=current.path, status=ProcessingStatus.PARTIAL,
                error='当前文件无匹配的分析记录，请重新检查；旧版本历史保留')
            result.verdict.needs_review = True
        self._on_item(result)
        from ui.services import events
        events.censor_changed.emit([current.path])
        self.metadata_controller.schedule()
        self._flash('已撤回人工确认，历史保留；可重新检查')

    def _batch_label(self, state):
        if busy(self):
            self._flash('后台任务仍在处理；请稍后批量确认，或只选一条进行单项确认')
            return
        paths = self._selected_visible_paths()
        if not paths:
            self._flash('先用 Ctrl / Shift 选中已审核的视频；只处理当前筛选中可见的选中项')
            return
        conclusion = '有马赛克' if state is CensorState.CENSORED else '未见马赛克'
        dialog = QMessageBox(self)
        dialog.setWindowTitle('批量人工确认')
        dialog.setText(f'将 {len(paths)} 个选中视频确认为“{conclusion}”。\n这是你的人工结论；低风险抽样本身不证明全片不存在。确认后按设置同步 NFO / 服务器标签。')
        dialog.setDetailedText('\n'.join(paths))
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        dialog.button(QMessageBox.Yes).setText(f'确认这 {len(paths)} 个视频')
        dialog.setDefaultButton(QMessageBox.Cancel)
        if dialog.exec() == QMessageBox.Yes:
            self._start_batch_confirmation(paths, state)

    def _start_batch_confirmation(self, paths, state):
        if busy(self):
            return
        items = [self._results[path] for path in dict.fromkeys(paths)
                 if path in self._results and self._matches_filter(self._results[path])]
        if not items:
            return
        from ui.review_controls import BatchConfirmationWorker
        worker = BatchConfirmationWorker(self._service, items, state)
        self._batch_review_worker = worker
        positions = self._table_presenter.ordered_paths()
        selected = {item.path for item in items}
        self._batch_review_navigation = dict(positions=positions, selected=selected,
            after=next((path for path in reversed(positions) if path in selected), ''),
            context=self._scan_context(), epoch=getattr(self, '_task_epoch', 0), report=None)
        worker.item_done.connect(self._on_batch_confirmed)
        worker.result.connect(self._on_batch_confirmation_done)
        start_task(self, worker, '批量保存人工确认')

    def _on_batch_confirmed(self, result):
        self._review_overrides[result.path] = result
        self._queue_item(result)

    def _on_batch_confirmation_done(self, report):
        from ui.services import events
        if report['paths']:
            events.censor_changed.emit(report['paths'])
        if report['cancelled']:
            self.metadata_controller.pause()
        else:
            self.metadata_controller.schedule()
        self._flash(f"批量确认已保存 {report['completed']} 个；失败 {report['fail']} 个"
                    + ('；已停止，已保存的确认保留' if report['cancelled'] else ''))
        navigation = self._batch_review_navigation
        if navigation is not None:
            navigation['report'] = report
            if not self._pending_results:
                self._finish_batch_review_navigation()

    def _finish_batch_review_navigation(self):
        navigation = self._batch_review_navigation
        if navigation is None or navigation['report'] is None:
            return
        self._batch_review_navigation = None
        report = navigation['report']
        if (report['fail'] or report['cancelled'] or not report['completed']
                or not self.workspace.state.get('auto_next', True)
                or navigation['epoch'] != getattr(self, '_task_epoch', 0)
                or navigation['context'] != self._scan_context()):
            return
        # Do not steal focus if the user has already chosen another video
        # while the batch was saving or waiting for its render queue.
        if self._current is not None and self._current.path not in navigation['selected']:
            return
        self._advance_review(navigation['after'], positions=navigation['positions'])

    def _send_lada(self):
        current = self._resolve_current()
        if current is None or not awaits_replacement(current):
            self._flash('先人工确认当前视频有码，再交给 Lada')
            return
        from ui.services import events
        events.lada_ready.emit([current.path])

    def apply_theme(self):
        from ui import theme
        _STATE_COLOR.update({CensorState.CENSORED: theme.color('danger'),
                             CensorState.UNCENSORED: theme.color('success'),
                             CensorState.UNKNOWN: theme.color('text_secondary')})
        _BUCKET_COLOR.update({key: theme.color('success' if key is ReviewBucket.CONFIRMED
            else 'warning' if key in (ReviewBucket.FAILED, ReviewBucket.UNREVIEWED)
            else 'text_secondary') for key in ReviewBucket})
        for result in self._results.values():
            self._replace_row(result)

    def _replace_row(self, result: VideoVerdict) -> None:
        self._table_presenter.present(result)

    # ── 无码版本检索（网络）──────────────────────────────────────
    # 说明：检索属于"辅助动作"，刻意**不进 TaskManager** ——
    # 否则一个慢站点会长时间占住 busy()，用户连拖新文件进来都会被拦。
    # 它自己可取消，页面关闭时一起收尾。
    def _clear(self) -> None:
        if busy(self):
            return
        self.table.setRowCount(0)
        self._results.clear()
        self._evidence_requests.clear()
        self._evidence_queue_timer.stop()
        self._batch_review_navigation = None
        self._category_counts.clear()
        self._review_overrides.clear()
        self._pending_results.clear()
        self._render_timer.stop()
        self._submitted.clear()
        self._table_presenter.clear()
        self._current = None
        self.gallery.show_candidates([])
        self.txt_evidence.clear()
        self._refresh_stats()

    def _filter_projection(self):
        """Count each row once; the selected view cannot filter its own counts."""
        counts, scoped, membership = Counter(), Counter(), {}
        confirmed_censored = 0
        visible = 0
        view = self._current_view()
        query = self._scope_query()
        for path, result in self._results.items():
            value = review_bucket(result)
            matches = self._matches_scope(result, query)
            counts[value] += 1
            membership[path] = (value, matches)
            if value is ReviewBucket.CONFIRMED and result.state is CensorState.CENSORED:
                confirmed_censored += 1
            if matches:
                scoped[value] += 1
                visible += int(in_view(view, value))
        return dict(counts=counts, scoped=scoped, membership=membership,
                    confirmed_censored=confirmed_censored, visible=visible)

    @staticmethod
    def _view_totals(counts):
        return {
            WorkflowView.TODO: sum(n for value, n in counts.items() if value.is_todo),
            WorkflowView.ALL: sum(counts.values()),
            WorkflowView.DONE: counts[ReviewBucket.CONFIRMED],
            WorkflowView.PARKED: counts[ReviewBucket.LOW_RISK] + counts[ReviewBucket.DEFERRED],
        }

    def _refresh_stats(self) -> None:
        self._render_stats(self._filter_projection())

    def _render_stats(self, projection) -> None:
        """两段统计，**刻意不混着算**：检查进度和检索进度是两件事。

        混在一起（上一版把"可换无码版"塞进同一个计数）会让人分不清
        "还有多少要我看"和"还有多少要去网上查"。
        """
        counts = projection['counts']
        todo = sum(n for b, n in counts.items() if b.is_todo)
        confirmed_censored = projection['confirmed_censored']
        confirmed_clean = counts[ReviewBucket.CONFIRMED] - confirmed_censored
        self._update_view_labels(self._view_totals(projection['scoped']), self._view_totals(counts))
        self._stats_label.setText(
            f'列表 {len(self._results)} · 筛选匹配 {sum(projection["scoped"].values())}'
            f' · 当前显示 {projection["visible"]} · 全列表统计：待处理 {todo}'
            f' · 已确认有码 {confirmed_censored}（在「替换版本检索」页提交）'
            f' · 已确认无码 {confirmed_clean}'
            f' · 低风险 {counts[ReviewBucket.LOW_RISK]}'
            f' · 已延后 {counts[ReviewBucket.DEFERRED]}'
            + (f' · 正在呈现 {len(self._pending_results)} 条结果' if self._pending_results else ''))

    def _update_view_labels(self, counts: dict, totals=None) -> None:
        """把计数写进下拉项：``待我处理 (37)``。

        菜单里带数字是为了让"还有多少活"一眼可见 —— 上一版要逐个点开才知道。
        """
        for index in range(self.combo_filter.count()):
            value = self.combo_filter.itemData(index)
            try:
                view = WorkflowView(value)
            except ValueError:
                continue
            label = view.label
            if value in counts:
                total = totals[value] if totals is not None else counts[value]
                count = counts[value]
                label = f'{label} ({count})' if count == total else f'{label} ({count} / {total})'
                self.combo_filter.setItemData(index,
                    f'当前其他筛选命中 {count}；此视图已加载总数 {total}。切换视图不改变其他视图的计数范围。',
                    Qt.ToolTipRole)
            if self.combo_filter.itemText(index) != label:
                self.combo_filter.setItemText(index, label)
