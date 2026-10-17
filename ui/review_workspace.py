"""Review workspace: bounded result area and a virtualized, wrapping facet cloud."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QLineEdit, QComboBox, QListWidget, QListView, QStyledItemDelegate,
    QAbstractItemView, QStyle, QTabWidget, QFrame, QScrollArea,
    QStackedWidget, QSplitter, QSizePolicy, QMenu, QDialog, QMessageBox, QTableWidget,
)
from ui import theme
from ui.widgets import LogPanel

COUNT_ROLE = Qt.UserRole + 10


class ResultTable(QTableWidget):
    """Keep the reason for an empty result visible inside its bounded viewport."""
    def __init__(self, rows, columns):
        super().__init__(rows, columns)
        self.empty_hint = QLabel("选择媒体库后，点击「开始分析」生成结果。", self.viewport())
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_hint.setGeometry(self.viewport().rect().adjusted(16, 40, -16, -16))

    def show_empty_state(self, visible, filtered=False):
        self.empty_hint.setText("没有符合筛选条件的作品，可调整标签或清除筛选。" if filtered else "当前分类暂无作品。")
        self.empty_hint.setGeometry(self.viewport().rect().adjusted(16, 40, -16, -16))
        self.empty_hint.setVisible(visible)


class PillDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        facet = index.data(Qt.UserRole)
        label = (("类型 · " if facet[0] == "genre" else "") + (index.data(Qt.UserRole + 11) or facet[1])) if facet else index.data()
        count = index.data(COUNT_ROLE)
        width = option.fontMetrics.horizontalAdvance(label + "  " + (str(count) if count is not None else "")) + 26
        return QSize(min(250, max(64, width)), max(28, option.fontMetrics.height() + 12))

    def paint(self, painter, option, index):
        painter.save()
        checked = index.data(Qt.CheckStateRole) == Qt.Checked.value
        rect = QRectF(option.rect.adjusted(2, 2, -2, -2))
        hovered = bool(option.state & QStyle.State_MouseOver)
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setBrush(QColor(theme.color("accent") if checked else theme.color("bg_hover" if hovered else "bg_card")))
        painter.setPen(QPen(QColor(theme.color("accent" if checked else "border"))))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        facet = index.data(Qt.UserRole)
        label = (("类型 · " if facet[0] == "genre" else "") + (index.data(Qt.UserRole + 11) or facet[1])) if facet else index.data()
        count = str(index.data(COUNT_ROLE)) if index.data(COUNT_ROLE) is not None else ""
        count_width = option.fontMetrics.horizontalAdvance(count) + 8
        text_rect = option.rect.adjusted(12, 0, -count_width - 10, 0)
        painter.setPen(QColor(theme.color("selection_text" if checked else "text_primary")))
        painter.drawText(text_rect, Qt.AlignVCenter, option.fontMetrics.elidedText(label, Qt.ElideRight, text_rect.width()))
        painter.setPen(QColor(theme.color("selection_text" if checked else "text_secondary")))
        painter.drawText(option.rect.adjusted(0, 0, -12, 0), Qt.AlignRight | Qt.AlignVCenter, count)
        if option.state & QStyle.State_HasFocus:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(theme.color("accent")), 1, Qt.DotLine))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)
        painter.restore()


class FacetCloud(QListWidget):
    """Native item model keeps thousands of labels cheap without child buttons."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.single_choice = False
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setSpacing(1)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setItemDelegate(PillDelegate(self))
        self.setMinimumWidth(0)

    def _toggle(self, item):
        if item is None:
            return
        checked = item.checkState() == Qt.Checked
        self.blockSignals(True)
        if self.single_choice:
            for i in range(self.count()):
                self.item(i).setCheckState(Qt.Unchecked)
        item.setCheckState(Qt.Unchecked if checked else Qt.Checked)
        self.blockSignals(False)
        self.itemChanged.emit(item)
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item:
                self.setCurrentItem(item)
                self._toggle(item)
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._toggle(self.currentItem())
            event.accept()
        else:
            super().keyPressEvent(event)


def _row(layout, *widgets):
    row = QHBoxLayout()
    row.setSpacing(6)
    for widget in widgets:
        row.addWidget(widget)
    layout.addLayout(row)
    return row


def build_review_workspace(p):
    p.setObjectName("reviewWorkspace")
    p.setStyleSheet("#reviewWorkspace QPushButton { padding: 4px 10px; min-height: 24px; }"
                    "#reviewWorkspace QLineEdit, #reviewWorkspace QComboBox { padding: 4px 6px; min-height: 24px; }"
                    "#reviewWorkspace QTabBar::tab { padding: 8px 10px; min-width: 56px; }")
    root = QVBoxLayout(p)
    root.setContentsMargins(12, 10, 12, 8)
    root.setSpacing(6)
    title = QLabel("快速打标")
    title.setProperty("cssClass", "page-title")
    p.btn_analyze = QPushButton("开始分析")
    p.btn_analyze.setProperty("cssClass", "accent")
    p.btn_analyze.clicked.connect(p._start)
    p.btn_undo = QPushButton("撤销")
    p.btn_undo.setToolTip("撤销上次人工打标 · Ctrl+Z")
    p.btn_undo.clicked.connect(p._undo)
    p.tools_button = QPushButton("更多工具 ▾")
    menu = QMenu(p.tools_button)
    p.tools_button.setMenu(menu)

    def action(attr, text, callback):
        button = QPushButton(text, p)
        button.clicked.connect(callback)
        button.hide()
        setattr(p, attr, button)
        entry = menu.addAction(text)
        entry.triggered.connect(button.click)
        menu.aboutToShow.connect(lambda: entry.setEnabled(button.isEnabled()))
        return button

    action("_order_btn", "队列顺序", p._toggle_order)
    action("_restore_skipped_btn", "恢复跳过的作品", p._restore_skipped)
    menu.addSeparator()
    action("btn_ai_like", "AI 复核 · 可能喜欢", lambda: p._ai_recheck("like"))
    action("btn_ai_del", "AI 复核 · 建议删除", lambda: p._ai_recheck("delete"))
    action("btn_ai_review", "AI 批量预判待复核", p._ai_review_and_mark)
    action("btn_ai_score", "AI 语义打分", p._ai_score_all)
    p.ai_tools_toggle = p.tools_button
    p._order_label = QLabel(p)
    p._order_label.hide()
    p._sync_order_ui()
    menu.aboutToShow.connect(lambda: menu.actions()[0].setText(p._order_btn.text()))
    p.log_dialog = QDialog(p)
    p.log_dialog.setWindowTitle("快速打标 · 任务日志")
    p.log_dialog.resize(740, 400)
    log_layout = QVBoxLayout(p.log_dialog)
    p.log = LogPanel()
    log_layout.addWidget(p.log)
    menu.addSeparator()
    menu.addAction("任务日志…", p.log_dialog.show)
    menu.addAction("快捷键说明…", lambda: QMessageBox.information(
        p, "卡片快判快捷键", "→ 喜欢/保留　← 不喜欢　↓ 暂时跳过\n空格打开文件夹　Enter 播放　Ctrl+Z 撤销\n表格：点击表头排序；拖动列边界调整宽度；拖动表头调整列顺序。"))
    header = _row(root, title)
    header.addStretch()
    for w in (p.btn_analyze, p.btn_undo, p.tools_button):
        header.addWidget(w)

    p.filter_text = QLineEdit()
    p.filter_text.setPlaceholderText("搜索名称 / 番号 / 演员 / 标签 / 理由…")
    p.filter_text.setClearButtonEnabled(True)
    p.filter_toggle = QPushButton("标签筛选")
    p.filter_toggle.setCheckable(True)
    p.btn_view = QPushButton("☷ 卡片视图")
    p.btn_view.clicked.connect(p._toggle_view)
    search_row = _row(root, p.filter_text, p.filter_toggle, p.btn_view)
    search_row.setStretch(0, 1)
    p.counts_label = QLabel()
    p.counts_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    root.addWidget(p.counts_label)

    p.workspace_splitter = QSplitter(Qt.Horizontal)
    root.addWidget(p.workspace_splitter, 1)
    p.main_tabs = QTabWidget()
    p.main_tabs.setMinimumSize(0, 0)
    p.main_tabs.setUsesScrollButtons(True)
    p.workspace_splitter.addWidget(p.main_tabs)
    p.filter_panel = QWidget()
    p.filter_panel.setMinimumWidth(290)
    side = QVBoxLayout(p.filter_panel)
    side.setContentsMargins(8, 4, 0, 0)
    side.setSpacing(6)
    p.facet_context = QLabel("当前结果页 · 尚未分析")
    side.addWidget(p.facet_context)
    p.facet_search = QLineEdit()
    p.facet_search.setPlaceholderText("搜索全部分组 · 空格分词…")
    p.facet_search.setClearButtonEnabled(True)
    side.addWidget(p.facet_search)
    p.facet_group = QComboBox()
    for name, value in (("内容标签", 'content'), ("类型", 'genre'), ("技术/版本", 'technical'), ("全部", 'all')):
        p.facet_group.addItem(name, value)
    p.facet_sort = QComboBox()
    for name, value in (("常用优先", 'count'), ("名称排序", 'name'), ("已选优先", 'selected')):
        p.facet_sort.addItem(name, value)
    _row(side, p.facet_group, p.facet_sort)
    p.facet_selection = QComboBox()
    p.facet_selection.addItems(["单选标签", "多选标签"])
    p.facet_selection.setCurrentIndex(1)
    p.filter_mode = QComboBox()
    p.filter_mode.addItems(["全部满足 AND", "任一满足 OR"])
    _row(side, p.facet_selection, p.filter_mode)
    p.filter_bucket = QComboBox()
    p.filter_bucket.addItem("全部分类", "")
    clear = QPushButton("清除筛选")
    clear.clicked.connect(p._clear_filters)
    _row(side, p.filter_bucket, clear)
    p.facet_status = QLabel("点击标签选中，再次点击取消")
    p.facet_status.setWordWrap(True)
    side.addWidget(p.facet_status)
    p.selected_facets = FacetCloud()
    p.selected_facets.setMaximumHeight(70)
    p.selected_facets.setMinimumHeight(34)
    p.selected_facets.hide()
    p.selected_facets.itemChanged.connect(lambda cell: p._set_metadata_facets(
        p._metadata_facets - {tuple(cell.data(Qt.UserRole))}))
    side.addWidget(p.selected_facets)
    p.filter_facets = FacetCloud()
    p.filter_facets.setToolTip("仅来自当前结果页。数字为点击后的命中数；常用排序保持稳定。已选条件可在上方直接取消。")
    p.facet_empty = QLabel()
    p.facet_empty.setWordWrap(True)
    p.facet_empty.hide()
    side.addWidget(p.facet_empty)
    side.addWidget(p.filter_facets, 1)
    p.workspace_splitter.addWidget(p.filter_panel)
    p.workspace_splitter.setCollapsible(0, False)
    p.workspace_splitter.setCollapsible(1, False)
    p.workspace_splitter.setStretchFactor(0, 1)
    p.workspace_splitter.setStretchFactor(1, 0)
    p.workspace_splitter.setSizes([760, 340])
    p.filter_panel.hide()
    p.filter_toggle.toggled.connect(p.filter_panel.setVisible)

    def selection_mode(index):
        p.filter_facets.single_choice = index == 0
        if index == 0 and len(p._metadata_facets) > 1:
            p._set_metadata_facets({sorted(p._metadata_facets)[0]})
        else:
            p._apply_filters()
    p.facet_selection.currentIndexChanged.connect(selection_mode)

    pending = QScrollArea()
    pending.setWidgetResizable(True)
    pending.setFrameShape(QFrame.NoFrame)
    p.card = QFrame()
    p.card.setProperty("cssClass", "panel-card")
    p.card_lay = QVBoxLayout(p.card)
    p.card_lay.setContentsMargins(12, 12, 12, 12)
    pending.setWidget(p.card)
    p.main_tabs.addTab(pending, "待复核 · 快判")
    p.tab_like = p._make_table(["番号", "名称", "命中度", "理由", "标签 / 类型 / 演员"])
    p.tab_del = p._make_table(["番号", "名称", "命中度", "理由", "标签 / 类型 / 演员"])
    p.tab_done = p._make_table(["番号", "名称", "打标", "大小"])
    p._list_stack, p._list_cardlay = {}, {}
    for key, table, name in (("like", p.tab_like, "可能喜欢"), ("del", p.tab_del, "建议删除"), ("done", p.tab_done, "已打标")):
        stack = QStackedWidget()
        stack.setMinimumSize(0, 0)
        stack.addWidget(table)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        cards = QWidget()
        layout = QVBoxLayout(cards)
        layout.setSpacing(8)
        scroll.setWidget(cards)
        stack.addWidget(scroll)
        p._list_stack[key], p._list_cardlay[key] = stack, layout
        p.main_tabs.addTab(stack, name)
    ai_tab = QWidget()
    ai_layout = QVBoxLayout(ai_tab)
    hint = QLabel("AI 建议须人工勾选采纳；删除仍须确认计划。")
    hint.setWordWrap(True)
    ai_layout.addWidget(hint)
    p.ai_suggestions = p._make_table(["采纳", "作品", "AI建议", "依据", "标签/类型/演员"])
    ai_layout.addWidget(p.ai_suggestions, 1)
    adopt, undo_ai = QPushButton("采纳勾选建议…"), QPushButton("撤销上次采纳")
    adopt.clicked.connect(p._adopt_ai_suggestions)
    undo_ai.clicked.connect(p._undo_ai_adoption)
    _row(ai_layout, adopt, undo_ai)
    p.main_tabs.addTab(ai_tab, "AI 建议")
    p.filter_counts = QLabel("尚未分析")
    p.filter_counts.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    root.addWidget(p.filter_counts)
    p.progress = QProgressBar()
    p.progress.setFixedHeight(5)
    p.progress.setTextVisible(False)
    p.progress.setValue(0)
    root.addWidget(p.progress)
    p.filter_text.textChanged.connect(p._apply_filters)
    p.filter_bucket.currentIndexChanged.connect(p._apply_filters)
    p.filter_mode.currentIndexChanged.connect(p._apply_filters)
    p.filter_facets.itemChanged.connect(p._facets_changed)
    p.facet_search.textChanged.connect(p._search_facets)
    p.facet_group.currentIndexChanged.connect(p._search_facets)
    p.facet_sort.currentIndexChanged.connect(p._apply_filters)
    p.main_tabs.currentChanged.connect(p._facet_tab_changed)
    p._clear_card()
    p._render_counts(None)
