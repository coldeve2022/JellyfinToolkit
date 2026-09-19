"""分集合并页面 — 分层树形列表：番号为父节点，CD1/CD2 等分集为子节点，直接勾选合并或删除。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QHeaderView,
    QFrame, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox,
    QProgressBar, QPushButton, QDialog, QDialogButtonBox, QFileDialog,
    QPlainTextEdit, QScrollArea, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import DragDropListWidget, InfoCard, LogPanel
from utils.merge import (
    check_merge_compat,
    display_number_for_group,
    discover_legacy_merged_files,
    discover_pending_merged_outputs,
)
from utils.merge_archive import (
    add_journal_entries, archive_group_for_merge, group_backup_dir,
    load_journal, rollbackable_entries,
)
from utils.merge_repair import archived_merge_items
from utils.trash import DeleteJournal
from utils.tools import resolve_tool
from workers.ffmpeg import find_binary  # noqa: F401 — 保留兼容入口
from workers.merge import MergeWorker, ScanMergeWorker
from workers.merge_archive import (
    JellyfinRefreshWorker,
    LegacyMergeRenameWorker,
    RollbackWorker,
)
from workers.merge_repair import RepairMergeWorker, RepairScanWorker
from workers.trash import TrashWorker


_STATUS_COLOR = {
    "ready": "success",
    "needs_review": "warning",
    "skip_variant": "text_muted",
    "skip_duplicate": "text_muted",
    "conflict": "danger",
    "skip": "text_muted",
}
_STATUS_CN = {
    "ready": "可合并",
    "needs_review": "待确认",
    "skip_variant": "多版本",
    "skip_duplicate": "重复",
    "conflict": "冲突",
    "skip": "跳过",
}
_MERGEABLE = ("ready", "needs_review", "conflict")
_DUPLICATE_STATUS = "skip_duplicate"
_VARIANT_STATUS = "skip_variant"

_FILTERS = [
    ("全部", None),
    ("可合并", "ready"),
    ("待确认", "needs_review"),
    ("冲突", "conflict"),
    ("重复", "skip_duplicate"),
    ("多版本", "skip_variant"),
]


def _resolve_ffprobe(ffmpeg: str | None = None) -> str:
    """定位 ffprobe：用户配置/程序目录/PATH/常见位置，找不到再从 ffmpeg 同目录推导。"""
    found = resolve_tool("ffprobe")
    if found:
        return found
    if ffmpeg:
        candidate = Path(ffmpeg).with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if candidate.exists():
            return str(candidate)
    return ""


class MergeTreeItem(QTreeWidgetItem):
    """支持按数值列（时长/大小）排序的树节点。"""

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        a = self.data(column, Qt.UserRole + 1)
        b = other.data(column, Qt.UserRole + 1)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except (TypeError, ValueError):
                return str(a).lower() < str(b).lower()
        return self.text(column).lower() < other.text(column).lower()


class MergeTree(QTreeWidget):
    """分集合并树：父节点=番号，子节点=分集文件。"""

    def __init__(self, on_shortcut=None, parent=None):
        super().__init__(parent)
        self._on_shortcut = on_shortcut
        self._menu_builder = None
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def set_menu_builder(self, fn):
        self._menu_builder = fn

    def _show_context_menu(self, pos):
        item = self.itemAt(pos)
        selected = self.selectedItems()
        if item is not None and item not in selected:
            selected = [item]
        menu = QMenu(self)
        if self._menu_builder is not None:
            self._menu_builder(menu, item, selected)
        if not menu.isEmpty():
            menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event):
        ctrl = bool(event.modifiers() & Qt.ControlModifier)
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if ctrl and event.key() == Qt.Key_A:
            self.selectAll()
            return
        if ctrl and event.key() == Qt.Key_U:
            self._emit("none")
            return
        if ctrl and event.key() == Qt.Key_I:
            self._emit("invert")
            return
        if event.key() == Qt.Key_Space:
            if ctrl or shift:
                self._emit("toggle_selected")
            else:
                self._emit("toggle")
            return
        super().keyPressEvent(event)

    def _emit(self, action):
        if self._on_shortcut:
            self._on_shortcut(action)


class MergePage(QWidget):
    """分集自动合并页。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._scan_worker: ScanMergeWorker | None = None
        self._merge_worker: MergeWorker | None = None
        self._trash_worker: TrashWorker | None = None
        self._rollback_worker: RollbackWorker | None = None
        self._legacy_worker: LegacyMergeRenameWorker | None = None
        self._jellyfin_worker: JellyfinRefreshWorker | None = None
        self._repair_scan_worker: RepairScanWorker | None = None
        self._repair_worker: RepairMergeWorker | None = None
        self._plan = None
        self._group_map: dict[str, object] = {}
        self._group_items: dict[str, QTreeWidgetItem] = {}
        self._current_filter: tuple | None = None
        self._merged: dict[str, dict] = {}
        self._trash_groups: dict[str, object] = {}
        self._trash_paths: list = []
        self._repair_items: dict[str, dict] = {}
        self._syncing_checks: bool = False
        self._saved_checks: dict[str, int] = {}
        self.journal = DeleteJournal(DATA_DIR / "trash_journal.db")
        self._archive_entries: list = load_journal()
        self._setup_ui()
        self._refresh_rollback_list()

    def _backup_root_text(self) -> str:
        """真正生效的备份目录（**解析后**的绝对路径）。

        用户最关心的是"原分集到底会备份到哪儿"，所以要显示解析结果而不是配置原文：
        留空、盘符不存在、目录不可写都会回退到数据目录下的 backups/
        （见 ``utils.merge_archive.resolve_backup_root``）。
        """
        from utils.merge_archive import resolve_backup_root

        subfolder = getattr(self.cfg, "merge_original_subfolder", "_merged_originals")
        try:
            return str(resolve_backup_root(self.cfg) / subfolder)
        except Exception:  # noqa: BLE001  提示文案不该因为路径解析失败而让整个流程崩掉
            from config import DATA_DIR
            return str(DATA_DIR / "backups" / subfolder)

    # ── UI ──
    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        scroll.setWidget(content)

        title = QLabel("分集合并")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "1. 拖入目录/文件，点“扫描分集”；系统自动识别同番号分集并排除重复/多版本。\n"
            "2. 树形列表按番号分组：顶层勾选框可整组全选；Ctrl+A 选中所有行，Space 勾选/取消，Ctrl+U 全不选，Ctrl+I 反选。\n"
            "3. “合并勾选分集”只处理可合并/待确认/冲突组；“删除勾选文件”会删除任意状态下的已勾选文件。\n"
            "4. 合并生成「番号 合并.mp4」，原文件不删除；合并后点“替换原文件”，确认后才备份到备份目录并改名为「番号.mp4」。"
        )
        layout.addWidget(hint)

        layout.addWidget(QLabel("待扫描目录/文件 (支持拖入):"))
        self.file_list = DragDropListWidget()
        self.file_list.setMaximumHeight(90)
        self.file_list.files_dropped.connect(self._on_files_dropped)
        layout.addWidget(self.file_list)

        scan_row = QHBoxLayout()
        self.btn_scan = QPushButton("🔍 扫描分集")
        self.btn_scan.clicked.connect(self._scan)
        scan_row.addWidget(self.btn_scan)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        scan_row.addWidget(self.btn_stop)
        scan_row.addStretch()
        self.status_label = QLabel("就绪")
        self.status_label.setProperty("cssClass", "muted-label")
        scan_row.addWidget(self.status_label)
        layout.addLayout(scan_row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.summary_label = QLabel("尚未扫描")
        self.summary_label.setWordWrap(True)
        self.summary_label.setProperty("cssClass", "muted-label")
        layout.addWidget(self.summary_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示:"))
        self.combo_filter = QComboBox()
        for label, statuses in _FILTERS:
            self.combo_filter.addItem(label, statuses)
        self.combo_filter.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.combo_filter)
        filter_row.addStretch()
        self.btn_select_all = QPushButton("全选可合并")
        self.btn_select_none = QPushButton("全不选")
        self.btn_check_selected = QPushButton("☑ 勾选选中项")
        self.btn_uncheck_selected = QPushButton("☐ 取消勾选选中项")
        self.btn_select_all.clicked.connect(lambda: self._on_shortcut("all"))
        self.btn_select_none.clicked.connect(lambda: self._on_shortcut("none"))
        self.btn_check_selected.clicked.connect(lambda: self._check_selected(True))
        self.btn_uncheck_selected.clicked.connect(lambda: self._check_selected(False))
        filter_row.addWidget(self.btn_select_all)
        filter_row.addWidget(self.btn_select_none)
        filter_row.addWidget(self.btn_check_selected)
        filter_row.addWidget(self.btn_uncheck_selected)
        self.count_label = QLabel("可合并 0 组｜已勾选 0 个文件")
        filter_row.addWidget(self.count_label)
        layout.addLayout(filter_row)

        self.tree = MergeTree(self._on_shortcut)
        self.tree.set_menu_builder(self._build_tree_menu)
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["番号 / 分集", "状态", "时长", "大小", "说明", "处理状态"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        header.setSectionResizeMode(5, QHeaderView.Interactive)
        header.setStretchLastSection(False)
        self.tree.setColumnWidth(0, 360)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 100)
        self.tree.setColumnWidth(4, 220)
        self.tree.setColumnWidth(5, 100)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.header().setSortIndicator(0, Qt.AscendingOrder)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        layout.addWidget(self.tree, 1)

        action_row = QHBoxLayout()
        self.btn_merge_checked = QPushButton("▶ 合并勾选分集")
        self.btn_merge_checked.setEnabled(False)
        self.btn_merge_checked.clicked.connect(self._merge_checked)
        action_row.addWidget(self.btn_merge_checked)

        self.btn_delete_checked = QPushButton("🗑️ 删除勾选文件")
        self.btn_delete_checked.setEnabled(False)
        self.btn_delete_checked.clicked.connect(self._delete_checked)
        action_row.addWidget(self.btn_delete_checked)

        self.btn_rediscover = QPushButton("🔎 检测未替换合并")
        self.btn_rediscover.setToolTip("重新扫描当前目录中已生成、还没替换原文件的「番号 合并.mp4」，无需重新合并。")
        self.btn_rediscover.clicked.connect(self._rediscover_pending)
        action_row.addWidget(self.btn_rediscover)

        self.btn_legacy = QPushButton("🧹 整理旧合并")
        self.btn_legacy.setToolTip("扫描旧「番号 合并.mp4」，改名为「番号.mp4」并同步 NFO，随后刷新 Jellyfin。")
        self.btn_legacy.clicked.connect(self._legacy_rename_clicked)
        action_row.addWidget(self.btn_legacy)

        self.btn_replace = QPushButton("🗂️ 替换原文件…")
        self.btn_replace.setEnabled(False)
        self.btn_replace.clicked.connect(self._replace_merged)
        action_row.addWidget(self.btn_replace)
        action_row.addStretch()
        layout.addLayout(action_row)

        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(130)
        layout.addWidget(self.log_panel)

        rollback_box = QGroupBox("回滚备份（可勾选单个文件恢复）")
        rb_layout = QVBoxLayout(rollback_box)
        self.rollback_list = QListWidget()
        self.rollback_list.setMaximumHeight(110)
        self.rollback_list.setToolTip("勾选要恢复的原文件/同名 sidecar，点击下方按钮恢复到原始路径。")
        rb_layout.addWidget(self.rollback_list)
        self.btn_rollback = QPushButton("↩ 回滚选中文件")
        self.btn_rollback.setEnabled(False)
        self.btn_rollback.clicked.connect(self._rollback_selected)
        rb_layout.addWidget(self.btn_rollback)
        layout.addWidget(rollback_box)

        self.rollback_list.itemChanged.connect(self._update_rollback_button)

        repair_box = QGroupBox("修复已损坏的旧合并（自动回滚 → 删除坏文件 → 重新合并 → 重新替换）")
        repair_layout = QVBoxLayout(repair_box)
        repair_btn_row = QHBoxLayout()
        self.btn_repair_scan = QPushButton("📋 列出已处理合并（不校验）")
        self.btn_repair_scan.clicked.connect(self._list_repairs_quick)
        repair_btn_row.addWidget(self.btn_repair_scan)
        self.btn_repair_check = QPushButton("🔎 轻量检测损坏（不解码）")
        self.btn_repair_check.clicked.connect(self._scan_repairs)
        repair_btn_row.addWidget(self.btn_repair_check)
        self.btn_repair_select_all = QPushButton("全选")
        self.btn_repair_select_all.clicked.connect(lambda: self._repair_set_checks(True))
        repair_btn_row.addWidget(self.btn_repair_select_all)
        self.btn_repair_select_none = QPushButton("全不选")
        self.btn_repair_select_none.clicked.connect(lambda: self._repair_set_checks(False))
        repair_btn_row.addWidget(self.btn_repair_select_none)
        repair_btn_row.addStretch()
        self.btn_repair_all = QPushButton("▶ 一键修复全部")
        self.btn_repair_all.clicked.connect(self._repair_all_quick)
        repair_btn_row.addWidget(self.btn_repair_all)
        self.btn_repair_start = QPushButton("▶ 开始修复选中项")
        self.btn_repair_start.setEnabled(False)
        self.btn_repair_start.clicked.connect(self._start_repairs)
        repair_btn_row.addWidget(self.btn_repair_start)
        repair_layout.addLayout(repair_btn_row)

        self.repair_list = QListWidget()
        self.repair_list.setMaximumHeight(160)
        self.repair_list.setToolTip("扫描后勾选要修复的旧合并；每个番号会恢复原分集、删除坏文件并重新合并替换。")
        repair_layout.addWidget(self.repair_list)
        self.repair_status_label = QLabel("尚未扫描")
        self.repair_status_label.setProperty("cssClass", "muted-label")
        repair_layout.addWidget(self.repair_status_label)
        layout.addWidget(repair_box)

        self.repair_list.itemChanged.connect(self._update_repair_buttons)

    def _rediscover_pending(self) -> None:
        """手动重新发现未替换合并，用于合并文件是在本软件之外生成/移动的情况。"""
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        if not self._group_map:
            QMessageBox.information(self, "提示", "请先扫描分集，再进行检测。")
            return
        self._discover_pending_merged_outputs()
        self._populate_tree()
        found = sum(1 for info in self._merged.values() if not info.get("archived"))
        self.status_label.setText(f"已重新检测未替换合并：{found} 个")
        self.log_panel.log_success(f"重新检测到 {found} 个未替换的合并文件")
        self._update_counts()

    # ── 树菜单 ──
    def _build_tree_menu(self, menu: QMenu, item, selected_items) -> None:
        group = self._group_for_item(item)
        if item is not None and group is not None and self._is_top_item(item):
            act_check = QAction("勾选该组全部", self.tree)
            act_uncheck = QAction("取消勾选该组", self.tree)
            act_check.triggered.connect(lambda: self._set_group_checked(group, True))
            act_uncheck.triggered.connect(lambda: self._set_group_checked(group, False))
            menu.addAction(act_check)
            menu.addAction(act_uncheck)
        elif item is not None and self._is_part_item(item):
            act_open = QAction("打开所在文件夹", self.tree)
            act_open.triggered.connect(lambda: self._open_path_in_explorer(self._path_for_item(item)))
            menu.addAction(act_open)
        act_expand = QAction("全部展开", self.tree)
        act_collapse = QAction("全部折叠", self.tree)
        act_expand.triggered.connect(lambda: self.tree.expandAll())
        act_collapse.triggered.connect(lambda: self.tree.collapseAll())
        menu.addSeparator()
        menu.addAction(act_expand)
        menu.addAction(act_collapse)

    def _is_top_item(self, item) -> bool:
        return item.data(0, Qt.UserRole) is not None and item.data(0, Qt.UserRole)[0] == "group"

    def _is_part_item(self, item) -> bool:
        return item.data(0, Qt.UserRole) is not None and item.data(0, Qt.UserRole)[0] == "part"

    def _path_for_item(self, item) -> str:
        data = item.data(0, Qt.UserRole)
        return data[1] if data and data[0] == "part" else ""

    def _number_for_item(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return None
        if data[0] == "group":
            return data[1]
        parent = item.parent()
        return parent.data(0, Qt.UserRole)[1] if parent else None

    def _group_for_item(self, item):
        number = self._number_for_item(item)
        return self._group_map.get(number) if number else None

    # ── 事件 ──
    def _on_files_dropped(self, paths) -> None:
        for p in paths:
            self.file_list.add_item(os.path.basename(p), p)

    def _collect_options(self) -> dict:
        return {
            "merge_output_container": self.cfg.merge_output_container,
            "merge_max_parts": self.cfg.merge_max_parts,
            "merge_dup_sim_threshold": self.cfg.merge_dup_sim_threshold,
            "merge_dup_hash_distance": self.cfg.merge_dup_hash_distance,
            "merge_dup_frame_count": self.cfg.merge_dup_frame_count,
            "merge_min_part_seconds": self.cfg.merge_min_part_seconds,
            "merge_timeout_per_file": self.cfg.merge_timeout_per_file,
            "exclude_path_keywords": self.cfg.exclude_path_keywords,
        }

    def _scan(self) -> None:
        roots = self.file_list.get_all_paths()
        if not roots:
            QMessageBox.warning(self, "提示", "请先拖入要扫描的目录或文件！")
            return
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        ffmpeg = resolve_tool("ffmpeg") or ""
        ffprobe = _resolve_ffprobe(ffmpeg)
        if not ffprobe:
            QMessageBox.warning(self, "缺少 ffprobe",
                                "未找到 ffprobe，无法扫描分集参数。请安装 FFmpeg 并加入 PATH。")
            return
        if ffmpeg:
            self.log_panel.log("画面指纹去重已启用")
        else:
            self.log_panel.log_warning("未找到 ffmpeg，画面指纹去重关闭；仅按文件大小/哈希去重。")
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.status_label.setText("正在扫描…")
        self._scan_worker = ScanMergeWorker(
            roots, ffmpeg or "", ffprobe or "", self._collect_options())
        self._scan_worker.log.connect(self.log_panel.log)
        self._scan_worker.progress.connect(self.progress.setValue)
        self._scan_worker.plan_ready.connect(self._on_plan_ready)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_scan_error(self, msg: str) -> None:
        self.log_panel.log_error(msg)
        QMessageBox.warning(self, "扫描失败", msg)

    def _on_scan_finished(self) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._scan_worker = None

    def _on_plan_ready(self, plan) -> None:
        self._plan = plan
        self._merged = {}
        self._saved_checks = {}
        self.combo_filter.blockSignals(True)
        self.combo_filter.setCurrentIndex(0)
        self.combo_filter.blockSignals(False)
        self._current_filter = None
        self._group_map = {g.number: g for g in plan.groups}
        self._discover_pending_merged_outputs()
        self._populate_tree()
        self._refresh_summary()
        self.status_label.setText("扫描完成")
        self.log_panel.log_success(
            f"扫描完成：可合并 {self._plan.stats.get('ready', 0)}，"
            f"待确认 {self._plan.stats.get('needs_review', 0)}，"
            f"单文件已略过 {self._plan.stats.get('skip', 0)}")

    def _discover_pending_merged_outputs(self) -> None:
        """重启后从磁盘重新发现已生成但未替换的「番号 合并.mp4」。"""
        if not self._group_map:
            return
        discovered = discover_pending_merged_outputs(list(self._group_map.values()))
        for number, info in discovered.items():
            info["archived"] = False
            info["use_tree_selection"] = True
            self._merged[number] = info
        if discovered:
            self.log_panel.log_success(
                f"发现 {len(discovered)} 个未替换的合并文件，可直接点击“替换原文件”。")

    def _refresh_summary(self) -> None:
        if not self._plan:
            return
        from collections import Counter
        counts = Counter(g.status for g in self._plan.groups)
        self._plan.stats = {
            "total_groups": len(self._plan.groups),
            "unrecognized": len(self._plan.unrecognized),
            "ready": counts.get("ready", 0),
            "needs_review": counts.get("needs_review", 0),
            "skip_variant": counts.get("skip_variant", 0),
            "skip_duplicate": counts.get("skip_duplicate", 0),
            "conflict": counts.get("conflict", 0),
            "skip": counts.get("skip", 0),
            "by_status": dict(counts),
        }
        s = self._plan.stats
        self.summary_label.setText(
            f"共 {s['total_groups']} 个番号分组｜可合并 {s['ready']}｜待确认 {s['needs_review']}"
            f"｜冲突 {s['conflict']}｜多版本 {s['skip_variant']}｜重复 {s['skip_duplicate']}"
            f"｜未识别 {s['unrecognized']}｜单文件已略过 {s['skip']}")

    def _on_filter_changed(self) -> None:
        data = self.combo_filter.currentData()
        self._current_filter = (data,) if data else None
        self._populate_tree()

    def _populate_tree(self) -> None:
        groups = [
            g for g in self._group_map.values()
            if g.status != "skip"
            and (self._current_filter is None or g.status in self._current_filter)
        ]
        self.tree.blockSignals(True)
        self.tree.clear()
        self._group_items = {}
        for g in groups:
            top = MergeTreeItem(self.tree)
            label = display_number_for_group(g) or g.number
            title = next((p.title.strip() for p in g.parts
                          if p.title and p.title.strip()), "")
            top.setText(0, f"{label} — {title}"
                        if title and title.lower() != label.lower() else label)
            top.setData(0, Qt.UserRole, ("group", g.number))
            top.setData(0, Qt.UserRole + 1, g.number)
            top.setText(1, _STATUS_CN.get(g.status, g.status))
            top.setForeground(1, QColor(theme.color(_STATUS_COLOR.get(g.status, "text_secondary"))))
            group_duration = sum((p.probe.duration or 0) for p in g.parts)
            group_size = sum((p.probe.size or 0) for p in g.parts)
            top.setText(2, f"{group_duration:.0f}s" if group_duration else "?s")
            top.setData(2, Qt.UserRole + 1, float(group_duration))
            top.setText(3, f"{group_size / 1024 / 1024:.1f}MB" if group_size else "?MB")
            top.setData(3, Qt.UserRole + 1, float(group_size))
            reason = "；".join(g.issues) if g.issues else ""
            if g.status in _MERGEABLE and g.status != "ready":
                reason += ("；" if reason else "") + "请勾选要合并的分集"
            if g.duplicate_paths:
                reason += ("；" if reason else "") + f"排除重复 {len(g.duplicate_paths)} 个"
            top.setText(4, reason)
            replace_state = self._replace_state(g)
            top.setText(5, replace_state)
            for p in sorted(g.parts, key=lambda p: p.part_index):
                child = MergeTreeItem(top)
                child.setText(0, self._part_label(p, g))
                child.setData(0, Qt.UserRole, ("part", p.path))
                child.setData(0, Qt.UserRole + 1, p.part_index)
                child.setText(2, f"{p.probe.duration:.0f}s" if p.probe.duration else "?s")
                child.setData(2, Qt.UserRole + 1, float(p.probe.duration or 0))
                child.setText(3, f"{p.probe.size / 1024 / 1024:.1f}MB" if p.probe.size else "?MB")
                child.setData(3, Qt.UserRole + 1, float(p.probe.size or 0))
                tags = []
                if p.path in g.duplicate_paths:
                    tags.append("重复")
                if p.variants:
                    tags.append(" ".join(p.variants))
                child.setText(4, " / ".join(tags))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                saved = self._saved_checks.get(p.path)
                if saved is not None:
                    child.setCheckState(0, Qt.CheckState(saved))
                else:
                    child.setCheckState(0, Qt.Checked if self._default_checked(g, p) else Qt.Unchecked)
                child.setToolTip(0, p.path)
            top.setFlags(top.flags() | Qt.ItemIsUserCheckable)
            top.setCheckState(0, self._child_check_summary(top))
            top.setExpanded(True)
            self._group_items[g.number] = top
        self.tree.blockSignals(False)
        self._update_counts()

    def _record_part_check(self, item) -> None:
        """把分集勾选状态写入持久缓存，切换筛选/重建树时不会丢失。"""
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != "part":
            return
        self._saved_checks[data[1]] = int(item.checkState(0).value)

    def _replace_state(self, g) -> str:
        if g.number not in self._merged:
            return "待合并"
        return "已替换" if self._merged[g.number].get("archived") else "待替换"

    def _part_label(self, p, g) -> str:
        if p.part_kind == "base":
            idx = "完整版"
        elif p.part_kind == "cd":
            idx = f"CD{p.part_index}"
        elif p.part_kind == "letter":
            idx = chr(ord("A") + p.part_index - 1) if 1 <= p.part_index <= 26 else f"字母{p.part_index}"
        else:
            idx = f"第{p.part_index}集"
        name = p.title or p.name
        return f"{idx} — {name}"

    def _default_checked(self, g, p) -> bool:
        if g.status in _MERGEABLE:
            return g.status == "ready" and p.path not in g.duplicate_paths
        if g.status == _DUPLICATE_STATUS:
            return p.path in g.duplicate_paths
        return False

    # ── 勾选与计数 ──
    def _child_check_summary(self, top) -> Qt.CheckState:
        total = top.childCount()
        if total == 0:
            return Qt.Unchecked
        checked = sum(1 for i in range(total) if top.child(i).checkState(0) == Qt.Checked)
        if checked == total:
            return Qt.Checked
        if checked == 0:
            return Qt.Unchecked
        return Qt.PartiallyChecked

    def _sync_parents_from_children(self) -> None:
        self.tree.blockSignals(True)
        try:
            for top in self._group_items.values():
                top.setCheckState(0, self._child_check_summary(top))
        finally:
            self.tree.blockSignals(False)

    def _on_tree_item_changed(self, item, column) -> None:
        if self._syncing_checks or column != 0:
            return
        if self._is_top_item(item):
            group = self._group_for_item(item)
            if group is not None:
                self._syncing_checks = True
                try:
                    self._set_group_checked(group, item.checkState(0) == Qt.Checked)
                finally:
                    self._syncing_checks = False
                self._update_counts()
            return
        if self._is_part_item(item):
            self._record_part_check(item)
            self._update_parent_check_state(item)
            self._update_counts()

    def _update_parent_check_state(self, child) -> None:
        parent = child.parent()
        if parent is None:
            return
        self._syncing_checks = True
        try:
            parent.setCheckState(0, self._child_check_summary(parent))
        finally:
            self._syncing_checks = False

    def _set_group_checked(self, group, checked: bool) -> None:
        top = self._group_items.get(group.number)
        if not top:
            return
        self.tree.blockSignals(True)
        try:
            for i in range(top.childCount()):
                child = top.child(i)
                child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                self._record_part_check(child)
            top.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        finally:
            self.tree.blockSignals(False)
        self._update_counts()

    def _set_all_visible_checked(self, checked: bool) -> None:
        self.tree.blockSignals(True)
        try:
            for top in self._group_items.values():
                for i in range(top.childCount()):
                    child = top.child(i)
                    child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                    self._record_part_check(child)
                top.setCheckState(0, self._child_check_summary(top))
        finally:
            self.tree.blockSignals(False)
        self._update_counts()

    def _set_mergeable_checked(self, checked: bool) -> None:
        self.tree.blockSignals(True)
        try:
            for number, top in self._group_items.items():
                group = self._group_map.get(number)
                if group is None or group.status not in _MERGEABLE:
                    continue
                for i in range(top.childCount()):
                    child = top.child(i)
                    path = child.data(0, Qt.UserRole)[1]
                    if checked and path in group.duplicate_paths:
                        child.setCheckState(0, Qt.Unchecked)
                    else:
                        child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                    self._record_part_check(child)
                top.setCheckState(0, self._child_check_summary(top))
        finally:
            self.tree.blockSignals(False)
        self._update_counts()

    def _check_selected(self, checked: bool) -> None:
        items = [it for it in self.tree.selectedItems() if self._is_part_item(it)]
        if not items:
            QMessageBox.information(self, "提示", "请先在树中按住 Ctrl/Shift 选中分集。")
            return
        self.tree.blockSignals(True)
        try:
            for it in items:
                it.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                self._record_part_check(it)
        finally:
            self.tree.blockSignals(False)
        self._sync_parents_from_children()
        self._update_counts()

    def _checked_originals_for_group(self, number: str) -> list | None:
        """读取树中某番号当前已勾选的真实分集，用于继续替换时确认原分集范围。"""
        top = self._group_items.get(number)
        group = self._group_map.get(number)
        if top is None or group is None:
            return None
        paths = []
        for i in range(top.childCount()):
            child = top.child(i)
            if child.checkState(0) != Qt.Checked:
                continue
            data = child.data(0, Qt.UserRole)
            if not data or data[0] != "part":
                continue
            path = data[1]
            if path in group.duplicate_paths:
                continue
            paths.append(path)
        return paths

    def _checked_merge_groups(self) -> list:
        out = []
        for number, top in self._group_items.items():
            group = self._group_map.get(number)
            if group is None or group.status not in _MERGEABLE:
                continue
            paths = []
            for i in range(top.childCount()):
                child = top.child(i)
                if child.checkState(0) == Qt.Checked:
                    path = child.data(0, Qt.UserRole)[1]
                    if path in group.duplicate_paths:
                        continue
                    paths.append(path)
            if len(paths) >= 2:
                out.append((group, paths))
        return out

    def _any_worker_running(self) -> bool:
        workers = (
            self._scan_worker, self._merge_worker, self._trash_worker,
            self._rollback_worker, self._repair_scan_worker, self._repair_worker,
            self._legacy_worker, self._jellyfin_worker,
        )
        return any(w is not None and w.isRunning() for w in workers)

    def _checked_delete_items(self) -> list:
        out = []
        for number, top in self._group_items.items():
            group = self._group_map.get(number)
            if group is None:
                continue
            for i in range(top.childCount()):
                child = top.child(i)
                if child.checkState(0) == Qt.Checked:
                    out.append((group, child.data(0, Qt.UserRole)[1]))
        return out

    def _update_counts(self) -> None:
        merge_n = len(self._checked_merge_groups())
        delete_n = len(self._checked_delete_items())
        self.count_label.setText(f"可合并 {merge_n} 组｜已勾选 {delete_n} 个文件")
        running = ((self._merge_worker is not None and self._merge_worker.isRunning())
                   or (self._trash_worker is not None and self._trash_worker.isRunning()))
        self.btn_merge_checked.setEnabled(merge_n > 0 and not running)
        self.btn_delete_checked.setEnabled(delete_n > 0 and not running)
        self.btn_replace.setEnabled(any(
            info.get("archived") is not True for info in self._merged.values()))

    def _on_shortcut(self, action: str) -> None:
        if action == "all":
            self._set_mergeable_checked(True)
            return
        if action == "none":
            self._set_all_visible_checked(False)
            return
        if action == "invert":
            self.tree.blockSignals(True)
            try:
                for top in self._group_items.values():
                    for i in range(top.childCount()):
                        child = top.child(i)
                        child.setCheckState(
                            0, Qt.Unchecked if child.checkState(0) == Qt.Checked else Qt.Checked)
                        self._record_part_check(child)
            finally:
                self.tree.blockSignals(False)
            self._sync_parents_from_children()
            self._update_counts()
            return
        item = self.tree.currentItem()
        if item is None or not self._is_part_item(item):
            return
        if action == "toggle":
            item.setCheckState(0, Qt.Unchecked if item.checkState(0) == Qt.Checked else Qt.Checked)
            self._record_part_check(item)
            self._update_counts()
        elif action == "toggle_selected":
            items = [it for it in self.tree.selectedItems() if self._is_part_item(it)]
            self.tree.blockSignals(True)
            try:
                for it in items:
                    it.setCheckState(0, Qt.Unchecked if it.checkState(0) == Qt.Checked else Qt.Checked)
                    self._record_part_check(it)
            finally:
                self.tree.blockSignals(False)
            self._sync_parents_from_children()
            self._update_counts()

    def _open_path_in_explorer(self, path: str) -> None:
        if not path:
            return
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        except OSError:
            pass

    # ── 合并 ──
    def _merge_checked(self) -> None:
        tasks = self._checked_merge_groups()
        if not tasks:
            QMessageBox.information(self, "提示", "请先在树中勾选至少两个分集。")
            return
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        ffmpeg = resolve_tool("ffmpeg") or ""
        if not ffmpeg:
            QMessageBox.warning(self, "缺少 ffmpeg", "未找到 ffmpeg，无法合并。")
            return
        ffprobe = _resolve_ffprobe(ffmpeg)
        if not ffprobe:
            QMessageBox.warning(self, "缺少 ffprobe", "未找到 ffprobe，无法校验合并结果。")
            return
        for group, paths in tasks:
            group.selected_paths = paths
        n_parts = sum(len(paths) for _, paths in tasks)
        first_group, first_paths = tasks[0]
        first_label = display_number_for_group(first_group) or first_group.number
        out_dir = str(Path(first_paths[0]).parent)
        ext = self.cfg.merge_output_container
        options = self._collect_options()
        need_reencode = False
        for group, paths in tasks:
            path_set = set(paths)
            parts = [p for p in group.parts if p.path in path_set]
            parts.sort(key=lambda p: p.part_index)
            if len(parts) >= 2 and not check_merge_compat(parts)[0]:
                need_reencode = True
                break
        transcode_hint = (
            "\n\n⚠️ 检测到部分分集参数不一致（帧率/采样率/分辨率等）。"
            "将先转码统一后再合并，会占用较多 CPU、耗时较长。"
            if need_reencode else ""
        )
        r = QMessageBox.question(
            self, "确认合并",
            f"将拼接 {len(tasks)} 个分集组（共 {n_parts} 个分集），"
            f"输出 {ext}，不重编码。\n输出示例：{out_dir}\\{first_label} 合并.{ext}\n"
            "原文件不会删除，合并完成后再由你确认是否替换。"
            f"{transcode_hint}\n继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        if need_reencode:
            options["merge_allow_reencode_fallback"] = True
        self.btn_merge_checked.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.status_label.setText("正在合并…")
        self._merge_worker = MergeWorker(
            [group for group, _ in tasks], ffmpeg, options, ffprobe)
        self._merge_worker.log.connect(self.log_panel.log)
        self._merge_worker.progress.connect(self.progress.setValue)
        self._merge_worker.group_done.connect(self._on_group_done)
        self._merge_worker.finished.connect(self._on_merge_finished)
        self._merge_worker.start()

    def _on_group_done(self, number: str, ok: bool, msg: str) -> None:
        if not ok:
            return
        group = self._group_map.get(number)
        label = display_number_for_group(group) if group else number
        self._merged[number] = {
            "output": msg,
            "originals": self._originals_for(number),
            "display_number": label,
        }
        for path in self._originals_for(number):
            self._saved_checks[path] = int(Qt.Unchecked.value)
        top = self._group_items.get(number)
        if top is not None:
            top.setText(5, "待替换")
            self.tree.blockSignals(True)
            try:
                for i in range(top.childCount()):
                    child = top.child(i)
                    child.setCheckState(0, Qt.Unchecked)
                    self._record_part_check(child)
                top.setCheckState(0, Qt.Unchecked)
            finally:
                self.tree.blockSignals(False)
        self._update_counts()

    def _originals_for(self, number: str) -> list:
        group = self._group_map.get(number)
        return list(group.selected_paths) if group else []

    def _on_merge_finished(self) -> None:
        self._merge_worker = None
        self._update_counts()
        self.btn_stop.setEnabled(False)
        self.status_label.setText("合并结束")

    # ── 删除勾选文件 ──
    def _delete_checked(self) -> None:
        self._delete_items(self._checked_delete_items())

    def _delete_items(self, items: list) -> None:
        if not items:
            QMessageBox.information(self, "提示", "没有要删除的文件。")
            return
        from collections import defaultdict
        by_group = defaultdict(list)
        for group, path in items:
            by_group[group.number].append(path)
        blocked = []
        targets = []
        group_map = {}
        for number, paths in by_group.items():
            group = self._group_map.get(number)
            if group is not None and len(paths) >= len(group.parts):
                blocked.append(number)
                continue
            for path in paths:
                group_map[path] = group
                part = next((p for p in group.parts if p.path == path), None)
                size = part.probe.size if part else None
                targets.append((path, size))
        if not targets:
            QMessageBox.warning(self, "不能全部删除",
                                f"以下番号至少保留一个文件，已跳过：{', '.join(blocked)}")
            return
        total = sum(s or 0 for _, s in targets)
        suffix = ""
        if blocked:
            suffix = f"\n\n已跳过整组删除：{', '.join(blocked)}"
        r = QMessageBox.question(
            self, "确认移入回收站",
            f"将把 {len(targets)} 个文件移入回收站（约 {total / 1024 / 1024:.0f} MB）。\n"
            "回收站中可恢复，但请先核对路径。确定继续？" + suffix,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        self._trash_groups = group_map
        self._trash_paths = [path for path, _ in targets]
        self.btn_stop.setEnabled(True)
        self.btn_delete_checked.setEnabled(False)
        self.btn_merge_checked.setEnabled(False)
        self.progress.setValue(0)
        self._trash_worker = TrashWorker(targets, journal=self.journal, reason="分集树勾选删除")
        self._trash_worker.log.connect(self.log_panel.log)
        self._trash_worker.progress.connect(self.progress.setValue)
        self._trash_worker.finished.connect(self._on_trash_done)
        self._trash_worker.start()

    def _on_trash_done(self, stats: dict) -> None:
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self.log_panel.log_success(
            f"回收站操作完成：成功 {stats.get('ok', 0)} / 失败 {stats.get('fail', 0)}")
        if stats.get("fail"):
            QMessageBox.warning(self, "部分失败",
                                f"{stats.get('fail')} 个文件未能移入回收站（原文件已保留）")
        fail_paths = {path for path, _ in stats.get("fails", [])}
        removed = [p for p in self._trash_paths if p not in fail_paths]
        if removed:
            affected = {}
            for path in removed:
                group = self._trash_groups.get(path)
                if group:
                    affected.setdefault(group.number, (group, []))[1].append(path)
            for number, (group, paths) in affected.items():
                path_set = set(paths)
                group.parts = [p for p in group.parts if p.path not in path_set]
                group.duplicate_paths = [d for d in group.duplicate_paths if d not in path_set]
                group.selected_paths = [s for s in group.selected_paths if s not in path_set]
                if len(group.parts) < 2:
                    group.status = "skip"
                    group.issues = ["删除后剩余分集不足 2 个"]
            self._populate_tree()
            self._refresh_summary()
        self._trash_groups = {}
        self._trash_paths = []
        self._trash_worker = None
        self._update_counts()

    # ── 最终确认替换 ──
    def _ask_batch_replace_confirm(self, total_groups: int, total_parts: int,
                                   lines: list) -> bool:
        """批量替换确认：明细放入可滚动文本区，按钮始终可见。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("确认替换原文件")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel(
            f"将把 {total_groups} 个合并结果替换原分集，共 {total_parts} 个原分集。\n"
            f"原文件及同名 NFO/字幕/图片会备份到：\n{self._backup_root_text()}\n确认后开始逐个替换。")
        title.setWordWrap(True)
        layout.addWidget(title)

        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText("\n".join(lines))
        detail.setMinimumHeight(260)
        layout.addWidget(detail, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("确认替换")
        buttons.button(QDialogButtonBox.No).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        return dialog.exec() == QDialog.Accepted

    def _replace_merged(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        pending = [(number, info) for number, info in self._merged.items()
                   if info.get("archived") is not True]
        if not pending:
            QMessageBox.information(self, "提示", "没有待替换的合并结果。")
            return
        skipped = []
        usable = []
        for number, info in pending:
            if info.get("use_tree_selection"):
                originals = self._checked_originals_for_group(number)
                if not originals or len(originals) < 2:
                    skipped.append(info.get("display_number") or number)
                    continue
                info["originals"] = originals
            usable.append((number, info))
        if skipped:
            QMessageBox.information(
                self, "部分待替换项已跳过",
                "以下番号没有在树中勾选至少两个分集，已跳过：\n"
                + "\n".join(skipped) + "\n\n请在树中勾选原分集后重试。")
        if not usable:
            return
        pending = usable

        lines = []
        for number, info in pending:
            originals = info.get("originals") or []
            label = info.get("display_number") or number
            lines.append(f"{label}：{len(originals)} 个分集 → {info.get('output', '')}")
        total_parts = sum(len(info.get("originals") or []) for _, info in pending)
        if not self._ask_batch_replace_confirm(len(pending), total_parts, lines):
            return
        for number, info in pending:
            self._archive_one(number, info, confirm=False)
        if self._merged:
            self._request_jellyfin_refresh()

    def _request_jellyfin_refresh(self) -> None:
        """替换完成后，异步请求 Jellyfin 全库刷新以同步文件名/NFO。"""
        server = getattr(self.cfg, "jellyfin_server", "")
        api_key = getattr(self.cfg, "jellyfin_api_key", "")
        if not server or not api_key:
            self.log_panel.log_warning(
                "⚠️ 未配置 Jellyfin 服务器/API Key，已跳过自动同步；请手动刷新媒体库。")
            return
        self._jellyfin_worker = JellyfinRefreshWorker(server, api_key)
        self._jellyfin_worker.log.connect(self.log_panel.log)
        self._jellyfin_worker.finished.connect(self._on_jellyfin_refresh_finished)
        self._jellyfin_worker.start()

    def _on_jellyfin_refresh_finished(self, stats: dict) -> None:
        self._jellyfin_worker = None
        if not stats.get("ok"):
            self.log_panel.log_warning(
                f"⚠️ Jellyfin 刷新未完成：{stats.get('message')}")

    def _archive_one(self, number: str, info: dict, confirm: bool = True) -> None:
        originals = info.get("originals") or []
        output = info.get("output") or ""
        label = info.get("display_number") or number
        if not originals or not output:
            QMessageBox.information(self, "提示", "该组缺少原分集或合并输出路径。")
            return
        backup = group_backup_dir(self.cfg, label)
        if confirm:
            r = QMessageBox.question(
                self, "确认替换原文件",
                f"番号 {label}：\n合并文件已生成：{output}\n"
                f"将把 {len(originals)} 个原分集及同名 NFO/字幕/图片备份到：\n{backup}\n"
                "替换后合并文件会改名为「番号.<容器>」，并同步生成/更新对应 NFO。\n确认吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        ok, entries, errors, final_output = archive_group_for_merge(
            self.cfg, label, originals, output)
        if not ok:
            for e in errors:
                self.log_panel.log_error(f"  ❌ 替换失败: {e}")
            QMessageBox.warning(self, "替换失败", "\n".join(errors) or "替换失败")
            return
        add_journal_entries(entries)
        for e in errors:
            self.log_panel.log_warning(f"  ⚠️ 非致命问题: {e}")
        self.log_panel.log_success(
            f"✅ 番号 {label} 已替换：{final_output}\n（原分集已备份到 {self._backup_root_text()}）")
        info["archived"] = True
        info["output"] = final_output
        info["entries"] = entries
        self._mark_top_archived(number)
        self._archive_entries = load_journal()
        self._refresh_rollback_list()

    def _mark_top_archived(self, number: str) -> None:
        top = self._group_items.get(number)
        if top is not None:
            top.setText(5, "已替换")
        self._update_counts()

    # ── 精确回滚 ──
    def _refresh_rollback_list(self) -> None:
        self.rollback_list.clear()
        for e in rollbackable_entries(self._archive_entries):
            label = f"[{e.get('number', '?')}] {os.path.basename(e.get('original_path', ''))}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, e.get("id"))
            item.setToolTip(
                f"原路径：{e.get('original_path')}\n备份路径：{e.get('backup_path')}")
            self.rollback_list.addItem(item)
        self._update_rollback_button()

    def _update_rollback_button(self) -> None:
        checked = any(
            self.rollback_list.item(i).checkState() == Qt.Checked
            for i in range(self.rollback_list.count()))
        self.btn_rollback.setEnabled(checked)

    def _checked_rollback_entries(self) -> list:
        by_id = {e.get("id"): e for e in self._archive_entries}
        out = []
        for i in range(self.rollback_list.count()):
            item = self.rollback_list.item(i)
            if item.checkState() == Qt.Checked:
                entry = by_id.get(item.data(Qt.UserRole))
                if entry:
                    out.append(entry)
        return out

    def _rollback_selected(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        entries = self._checked_rollback_entries()
        if not entries:
            QMessageBox.information(self, "提示", "请先勾选要恢复的备份文件。")
            return
        r = QMessageBox.question(
            self, "确认回滚",
            f"将把 {len(entries)} 个备份文件恢复到原始路径。\n"
            "恢复的是单个文件，不会自动删除合并后的视频。继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        if self._rollback_worker and self._rollback_worker.isRunning():
            QMessageBox.warning(self, "提示", "回滚正在进行中。")
            return
        self.btn_stop.setEnabled(True)
        self.btn_rollback.setEnabled(False)
        self.progress.setValue(0)
        self._rollback_worker = RollbackWorker(entries)
        self._rollback_worker.log.connect(self.log_panel.log)
        self._rollback_worker.progress.connect(self.progress.setValue)
        self._rollback_worker.finished.connect(self._on_rollback_done)
        self._rollback_worker.start()

    def _on_rollback_done(self, stats: dict) -> None:
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        self.log_panel.log_success(
            f"回滚完成：成功 {stats.get('ok', 0)} / 失败 {stats.get('fail', 0)}")
        if stats.get("fail"):
            QMessageBox.warning(self, "部分失败",
                                f"{stats.get('fail')} 个文件恢复失败（备份文件仍保留）")
        self._archive_entries = load_journal()
        self._refresh_rollback_list()
        self._rollback_worker = None

    # ── 已损坏旧合并修复 ──
    def _list_repairs_quick(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        items = archived_merge_items(load_journal())
        self._repair_items = {item["number"]: item for item in items}
        self.repair_list.blockSignals(True)
        self.repair_list.clear()
        for item in items:
            path = item.get("final_path") or ""
            li = QListWidgetItem()
            li.setText(f"[待修复] {item['number']} — {os.path.basename(path) or path}")
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Checked)
            li.setData(Qt.UserRole, item["number"])
            li.setToolTip(path)
            self.repair_list.addItem(li)
        self.repair_list.blockSignals(False)
        self.repair_status_label.setText(
            f"已列出 {len(items)} 个已处理合并，默认全选；可手动取消个别项后开始修复。")
        self.btn_repair_start.setEnabled(True)
        self._update_repair_buttons()

    def _repair_all_quick(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        self._list_repairs_quick()
        if self._repair_items:
            self._start_repairs()

    def _scan_repairs(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        ffmpeg = resolve_tool("ffmpeg") or ""
        if not ffmpeg:
            QMessageBox.warning(self, "缺少 ffmpeg", "未找到 ffmpeg，无法校验旧合并。")
            return
        ffprobe = _resolve_ffprobe(ffmpeg)
        self._repair_items = {
            item["number"]: item for item in archived_merge_items(load_journal())
        }
        self.repair_list.blockSignals(True)
        self.repair_list.clear()
        self.repair_list.blockSignals(False)
        self.btn_repair_scan.setEnabled(False)
        self.btn_repair_check.setEnabled(False)
        self.btn_repair_start.setEnabled(False)
        self.btn_repair_all.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.repair_status_label.setText("正在扫描并校验…")
        self.progress.setValue(0)
        self._repair_scan_worker = RepairScanWorker(
            ffmpeg, load_journal(), ffprobe or ""
        )
        self._repair_scan_worker.log.connect(self.log_panel.log)
        self._repair_scan_worker.progress.connect(self.progress.setValue)
        self._repair_scan_worker.item_done.connect(self._on_repair_scan_item)
        self._repair_scan_worker.finished.connect(self._on_repair_scan_finished)
        self._repair_scan_worker.start()

    def _on_repair_scan_item(self, number: str, path: str, ok: bool, detail: str) -> None:
        info = self._repair_items.get(number)
        if info is None:
            return
        info["ok"] = ok
        info["detail"] = detail
        info["final_path"] = path
        li = QListWidgetItem()
        status = "正常" if ok else "疑似损坏"
        li.setText(f"[{status}] {number} — {os.path.basename(path)} — {detail}")
        li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
        li.setCheckState(Qt.Checked if not ok else Qt.Unchecked)
        li.setData(Qt.UserRole, number)
        li.setToolTip(path)
        self.repair_list.blockSignals(True)
        self.repair_list.addItem(li)
        self.repair_list.blockSignals(False)

    def _on_repair_scan_finished(self, stats: dict) -> None:
        self.btn_stop.setEnabled(False)
        self.btn_repair_scan.setEnabled(True)
        self.btn_repair_check.setEnabled(True)
        self._repair_scan_worker = None
        total = stats.get("total", 0)
        damaged = stats.get("damaged", 0)
        self.repair_status_label.setText(f"共 {total} 个已完成合并，疑似损坏 {damaged} 个。")
        self._update_repair_buttons()

    def _repair_set_checks(self, checked: bool, damaged_only: bool = False) -> None:
        self.repair_list.blockSignals(True)
        for i in range(self.repair_list.count()):
            li = self.repair_list.item(i)
            info = self._repair_items.get(li.data(Qt.UserRole))
            if damaged_only and info and info.get("ok"):
                continue
            li.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.repair_list.blockSignals(False)
        self._update_repair_buttons()

    def _checked_repair_items(self) -> list:
        out = []
        for i in range(self.repair_list.count()):
            li = self.repair_list.item(i)
            if li.checkState() == Qt.Checked:
                info = self._repair_items.get(li.data(Qt.UserRole))
                if info:
                    out.append(info)
        return out

    def _update_repair_buttons(self) -> None:
        checked = any(
            self.repair_list.item(i).checkState() == Qt.Checked
            for i in range(self.repair_list.count())
        )
        running = ((self._repair_worker is not None and self._repair_worker.isRunning())
                   or (self._repair_scan_worker is not None and self._repair_scan_worker.isRunning()))
        self.btn_repair_start.setEnabled(checked and not running)
        self.btn_repair_all.setEnabled(self.repair_list.count() > 0 and not running)

    def _start_repairs(self) -> None:
        items = self._checked_repair_items()
        if not items:
            QMessageBox.information(self, "提示", "请先勾选要修复的旧合并。")
            return
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        ffmpeg = resolve_tool("ffmpeg") or ""
        ffprobe = _resolve_ffprobe(ffmpeg)
        if not ffmpeg or not ffprobe:
            QMessageBox.warning(self, "缺少 FFmpeg", "需要 ffmpeg 和 ffprobe 才能重新合并。")
            return
        r = QMessageBox.question(
            self, "确认批量修复",
            f"将修复 {len(items)} 个旧合并：\n"
            "恢复原分集 → 删除坏文件 → 用新链路重新合并 → 重新替换。\n"
            f"原分集会再次备份到：\n{self._backup_root_text()}\n不会直接物理删除。继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self.btn_repair_start.setEnabled(False)
        self.btn_repair_scan.setEnabled(False)
        self.btn_repair_check.setEnabled(False)
        self.btn_repair_all.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.repair_status_label.setText(f"正在修复 {len(items)} 个合并…")
        self._repair_worker = RepairMergeWorker(
            items, self.cfg, ffmpeg, ffprobe, self._collect_options())
        self._repair_worker.log.connect(self.log_panel.log)
        self._repair_worker.progress.connect(self.progress.setValue)
        self._repair_worker.item_done.connect(self._on_repair_item_done)
        self._repair_worker.finished.connect(self._on_repair_finished)
        self._repair_worker.start()

    def _on_repair_item_done(self, number: str, ok: bool, detail: str) -> None:
        for i in range(self.repair_list.count()):
            li = self.repair_list.item(i)
            if li.data(Qt.UserRole) == number:
                status = "已修复" if ok else "修复失败"
                li.setText(f"[{status}] {number} — {detail}")
                break

    def _on_repair_finished(self, stats: dict) -> None:
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100)
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        self.log_panel.log_success(f"批量修复完成：成功 {ok} / 失败 {fail}")
        self.repair_status_label.setText(f"修复完成：成功 {ok} / 失败 {fail}")
        if fail:
            QMessageBox.warning(self, "部分失败",
                                f"{fail} 个合并修复失败，原分集/备份仍保留，请查看日志。")
        self._archive_entries = load_journal()
        self._refresh_rollback_list()
        self._repair_worker = None
        self.repair_list.blockSignals(True)
        try:
            for i in range(self.repair_list.count()):
                self.repair_list.item(i).setCheckState(Qt.Unchecked)
        finally:
            self.repair_list.blockSignals(False)
        self.btn_repair_scan.setEnabled(True)
        self.btn_repair_check.setEnabled(True)
        self._update_repair_buttons()

    # ── 旧合并整理（仅重命名 + NFO 同步 + Jellyfin 刷新） ──
    def _legacy_rename_clicked(self) -> None:
        if self._any_worker_running():
            QMessageBox.warning(self, "提示", "有任务正在进行，请稍候或先停止！")
            return
        roots = self.file_list.get_all_paths()
        if not roots:
            chosen = QFileDialog.getExistingDirectory(
                self, "选择要整理旧合并的目录", str(Path.home()))
            if not chosen:
                return
            self.file_list.add_item(os.path.basename(chosen), chosen)
            roots = [chosen]
        items = discover_legacy_merged_files(roots)
        if not items and roots:
            chosen = QFileDialog.getExistingDirectory(
                self, "当前列表未发现旧合并，请选择其他目录", str(Path.home()))
            if chosen:
                self.file_list.add_item(os.path.basename(chosen), chosen)
                roots.append(chosen)
                items = discover_legacy_merged_files(roots)
        if not items:
            QMessageBox.information(
                self, "提示",
                "没有发现旧合并文件。请确认目录里存在「番号 合并.mp4」这类文件，"
                "或先把对应目录拖入上方列表后再试。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("整理旧合并")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(QLabel(
            f"将整理 {len(items)} 个旧合并文件：去掉「合并」后缀、同步同名 NFO/字幕，"
            "并按设置请求 Jellyfin 刷新。"))
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlainText("\n".join(
            f"{Path(item['path']).name}  →  {Path(item['target']).name}"
            for item in items))
        detail.setMinimumHeight(260)
        layout.addWidget(detail, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        buttons.button(QDialogButtonBox.Yes).setText("开始整理")
        buttons.button(QDialogButtonBox.No).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        self.btn_legacy.setEnabled(False)
        self.btn_scan.setEnabled(False)
        self.btn_rediscover.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.status_label.setText(f"正在整理 {len(items)} 个旧合并…")
        self._legacy_worker = LegacyMergeRenameWorker(
            items,
            getattr(self.cfg, "jellyfin_server", ""),
            getattr(self.cfg, "jellyfin_api_key", ""),
        )
        self._legacy_worker.log.connect(self.log_panel.log)
        self._legacy_worker.progress.connect(self.progress.setValue)
        self._legacy_worker.item_done.connect(self._on_legacy_item_done)
        self._legacy_worker.finished.connect(self._on_legacy_finished)
        self._legacy_worker.start()

    def _on_legacy_item_done(self, path: str, ok: bool, detail: str) -> None:
        if ok:
            self.log_panel.log_success(f"✅ 已整理：{detail}")
        else:
            self.log_panel.log_error(f"❌ 整理失败：{Path(path).name} — {detail}")

    def _on_legacy_finished(self, stats: dict) -> None:
        self._legacy_worker = None
        self.btn_stop.setEnabled(False)
        self.btn_legacy.setEnabled(True)
        self.btn_scan.setEnabled(True)
        self.btn_rediscover.setEnabled(True)
        self.progress.setValue(100)
        self.status_label.setText(
            f"旧合并整理完成：成功 {stats.get('ok', 0)} / 失败 {stats.get('fail', 0)}")
        if stats.get("fail"):
            QMessageBox.warning(self, "部分失败",
                                f"{stats.get('fail')} 个文件整理失败，请查看日志。")
        self._discover_pending_merged_outputs()
        self._populate_tree()
        self._update_counts()

    # ── 停止/清理 ──
    def _stop(self) -> None:
        if self._merge_worker and self._merge_worker.isRunning():
            self._merge_worker.stop()
            self.log_panel.log_warning("已请求停止合并，正在结束当前 ffmpeg，原文件不受影响")
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.stop()
            self.log_panel.log_warning("已请求停止扫描")
        if self._repair_scan_worker and self._repair_scan_worker.isRunning():
            self._repair_scan_worker.stop()
            self.log_panel.log_warning("已请求停止旧合并扫描")
        if self._repair_worker and self._repair_worker.isRunning():
            self._repair_worker.stop()
            self.log_panel.log_warning("已请求停止旧合并修复，正在结束当前 ffmpeg")
        if self._trash_worker and self._trash_worker.isRunning():
            self._trash_worker.stop()
            self.log_panel.log_warning("已请求停止回收站操作")
        if self._rollback_worker and self._rollback_worker.isRunning():
            self._rollback_worker.stop()
            self.log_panel.log_warning("已请求停止回滚")
        if self._legacy_worker and self._legacy_worker.isRunning():
            self._legacy_worker.stop()
            self.log_panel.log_warning("已请求停止旧合并整理")
        if self._jellyfin_worker and self._jellyfin_worker.isRunning():
            self._jellyfin_worker.stop()
        self.btn_stop.setEnabled(False)

    def shutdown(self) -> None:
        self._stop()
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.wait(3000)
        if self._merge_worker and self._merge_worker.isRunning():
            self._merge_worker.wait(3000)
        if self._trash_worker and self._trash_worker.isRunning():
            self._trash_worker.stop()
            self._trash_worker.wait(3000)
        if self._rollback_worker and self._rollback_worker.isRunning():
            self._rollback_worker.stop()
            self._rollback_worker.wait(3000)
        if self._legacy_worker and self._legacy_worker.isRunning():
            self._legacy_worker.stop()
            self._legacy_worker.wait(3000)
        if self._jellyfin_worker and self._jellyfin_worker.isRunning():
            self._jellyfin_worker.wait(3000)
        if self._repair_scan_worker and self._repair_scan_worker.isRunning():
            self._repair_scan_worker.stop()
            self._repair_scan_worker.wait(3000)
        if self._repair_worker and self._repair_worker.isRunning():
            self._repair_worker.wait(3000)
        if self._repair_worker and self._repair_worker.isRunning():
            self._repair_worker.stop()
            self._repair_worker.wait(3000)
