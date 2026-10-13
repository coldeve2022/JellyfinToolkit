"""删除暂存页 - 建议删除清单 → 一键移入可恢复隔离区(可恢复) + 删除日志。

删除红线：
- 只移入可恢复隔离区，绝不物理删除（失败保留原文件并提示）
- 破坏性操作二次确认
- 全程日志留痕 + 落库记录
"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page, events, state_context, import_media

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QMessageBox,
    QGroupBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QTabWidget,
    QFileDialog,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import LogPanel, DragDropListWidget, CoverThumbDelegate
from utils import scoring
from utils.labels import LabelStore
from utils.trash import DeleteJournal
from ui.services import analysis_worker, accept_snapshot
from workers.scorer import AnalyzeWorker
from workers.trash import TrashWorker


def _size_str(n: int | None) -> str:
    if not n:
        return ""
    return f"{n / 1024 / 1024:.0f} MB"


class DeletePage(QWidget):
    """删除暂存页。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._state_context = state_context(cfg)
        self.store = LabelStore(DATA_DIR / "smart.db", context=state_context(self.cfg))
        self.journal = DeleteJournal(DATA_DIR / "trash_journal.db")
        self.analyzer: AnalyzeWorker | None = None
        self.trash_worker: TrashWorker | None = None
        self.ai_worker = None
        self.labels: dict = self.store.all()
        events.labels_changed.connect(self._refresh_labels)
        self._delete_rows: list[dict] = []  # 建议删除(自动+人工标DELETE)
        self._manual_paths: set[str] = set()  # 手动添加的文件
        self._setup_ui()

    # ── UI ──
    def _refresh_labels(self):
        self.store = LabelStore(DATA_DIR / "smart.db", context=state_context(self.cfg))
        self.labels = self.store.all()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("删除暂存")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "这里是「建议删除」清单（自动零命中 + 垃圾附属文件 + 你在打标页标了删除的）。\n"
            "删除只移入可恢复隔离区（可恢复），不会物理删除；操作前会二次确认并留痕。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        act = QHBoxLayout()
        self.btn_load = QPushButton("🔍 加载建议删除清单")
        self.btn_load.setProperty("cssClass", "accent")
        self.btn_load.clicked.connect(self._load)
        act.addWidget(self.btn_load)
        self.btn_del_sel = QPushButton("🗑️ 归档当前列表选中项")
        self.btn_del_sel.clicked.connect(lambda: self._trash(selected_only=True))
        act.addWidget(self.btn_del_sel)
        self.btn_del_all = QPushButton("🗑️ 归档当前列表全部项")
        self.btn_del_all.clicked.connect(lambda: self._trash(selected_only=False))
        act.addWidget(self.btn_del_all)
        self.btn_sel_all = QPushButton("✅ 全选")
        self.btn_sel_all.clicked.connect(self._select_all)
        act.addWidget(self.btn_sel_all)
        self.btn_sel_clear = QPushButton("✖ 清除选择")
        self.btn_sel_clear.clicked.connect(lambda: self.table.clearSelection())
        act.addWidget(self.btn_sel_clear)
        self.btn_ai = QPushButton("🤖 AI 复核(前25条)")
        self.btn_ai.clicked.connect(self._ai_recheck)
        act.addWidget(self.btn_ai)
        override = QPushButton("覆盖AI保留的选中项…")
        override.clicked.connect(self._override_ai_keep)
        act.addWidget(override)
        act.addStretch()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setValue(0)
        act.addWidget(self.progress)
        root.addLayout(act)

        self.summary = QLabel("尚未加载清单")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        tabs = self.tabs = QTabWidget()
        # 清单表（含封面列：删除前可放大缩略图肉眼确认，防误删）
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["封面", "番号", "名称", "命中度", "理由", "大小"])
        # 全 Interactive + 固定初始宽：拖任意列只改该列（去掉 Stretch 联动）
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setDefaultSectionSize(120)
        h.setMinimumSectionSize(70)
        h.setStretchLastSection(False)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 380)
        self.table.setColumnWidth(4, 300)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setItemDelegateForColumn(0, CoverThumbDelegate(self.table))
        tabs.addTab(self.table, "建议删除清单")

        # 手动添加
        manual = QWidget()
        ml = QVBoxLayout(manual)
        row = QHBoxLayout()
        row.addWidget(QLabel("拖入文件/文件夹，或"))
        btn_add = QPushButton("添加文件")
        btn_add.clicked.connect(self._add_files)
        row.addWidget(btn_add)
        btn_add_dir = QPushButton("添加文件夹")
        btn_add_dir.clicked.connect(self._add_dir)
        row.addWidget(btn_add_dir)
        row.addStretch()
        ml.addLayout(row)
        self.manual_list = DragDropListWidget()
        self.manual_list.paths_changed.connect(self._sync_manual_paths)
        self.manual_list.files_dropped.connect(self._add_paths)
        ml.addWidget(self.manual_list)
        tabs.addTab(manual, "手动添加")

        # 已删记录
        self.journal_tab = QTableWidget(0, 4)
        self.journal_tab.setHorizontalHeaderLabels(["删除时间", "大小", "原因", "路径"])
        jh = self.journal_tab.horizontalHeader()
        jh.setSectionResizeMode(QHeaderView.Interactive)
        jh.setDefaultSectionSize(120)
        jh.setMinimumSectionSize(70)
        jh.setStretchLastSection(False)
        self.journal_tab.setColumnWidth(3, 420)
        self.journal_tab.verticalHeader().setVisible(False)
        self.journal_tab.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tabs.addTab(self.journal_tab, "已删除记录")
        root.addWidget(tabs, 1)

        g = QGroupBox("日志")
        gl = QVBoxLayout(g)
        self.log = LogPanel()
        self.log.setMaximumHeight(130)
        gl.addWidget(self.log)
        root.addWidget(g)

        self._refresh_journal()

    # ── 加载清单 ──
    def _load(self) -> None:
        if busy(self):
            return
        if self.analyzer and self.analyzer.isRunning():
            return
        self._refresh_labels()
        self.btn_load.setEnabled(False)
        self.log.clear()
        self.progress.setValue(0)
        self.analyzer = analysis_worker(
            self.cfg,
            self.labels,
            exclude_keywords=self.cfg.exclude_path_keywords,
            th_keep=self.cfg.score_th_keep / 100,
            th_delete=self.cfg.score_th_delete / 100,
        )
        self.analyzer.log.connect(self.log.log)
        self.analyzer.progress.connect(self.progress.setValue)
        self.analyzer.finished.connect(self._on_loaded)
        start_task(self, self.analyzer)

    def _on_loaded(self, data: dict) -> None:
        accept_snapshot(self, data)
        self.btn_load.setEnabled(True)
        self.progress.setValue(100 if "results" in data else 0)
        if "error" in data:
            QMessageBox.warning(self, "错误", str(data["error"]))
            return
        self._delete_rows = []
        skipped = 0
        for r in data["results"]:
            if r.decision != scoring.DEC_DELETE or r.is_seen:
                continue
            it = r.item
            # 排除 Jellyfin 附属文件(theme/缩略图/fanart 等) —— 不是作品，不该删除
            if r.is_junk:
                skipped += 1
                continue
            self._delete_rows.append(
                {
                    "num": it.num or "",
                    "name": it.name,
                    "hit": f"{r.s_content:.0%}",
                    "reason": r.reason,
                    "size": it.size,
                    "path": it.path,
                    "cover": it.cover,
                    "actors": list(it.actors),
                    "studios": list(it.studios),
                    "genres": list(it.genres),
                    "item_id": it.id,
                }
            )
        self._render_table()
        self.log.log_success(f"建议删除 {len(self._delete_rows)} 条（排除附属文件 {skipped} 条）")

    # ── 手动添加 ──
    def _sync_manual_paths(self, paths: list) -> None:
        self._manual_paths = set(paths)

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要删除(移入可恢复隔离区)的文件",
            "",
            "视频 (*.mp4 *.mkv *.avi *.ts *.mov *.wmv *.flv *.rmvb);;所有文件 (*.*)",
        )
        self._add_paths(files)

    def _add_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要删除(移入可恢复隔离区)的文件夹")
        if d:
            self._add_paths([d])

    def _add_paths(self, paths: list) -> None:
        if busy(self):
            return
        if import_media(self, paths, self._add_paths):
            return
        if paths:
            self.tabs.setCurrentIndex(1)
        for p in paths:
            p = os.path.normpath(p)
            if os.path.exists(p):
                self._manual_paths.add(p)
                self.manual_list.add_item(os.path.basename(p), p)
        self.log.log_info(f"手动添加 {len(paths)} 项")

    # ── 删除执行 ──
    def _trash(self, selected_only: bool) -> None:
        if busy(self, mutating=True):
            return
        targets = self._planned_targets(selected_only)
        if not targets:
            QMessageBox.information(
                self, "没有待执行项", "当前范围没有可删除文件；AI建议保留项已排除。"
            )
            return
        total = sum(size or 0 for _, size in targets)
        preview = "\n".join(path for path, _ in targets)
        if (
            QMessageBox.question(
                self,
                "审核删除计划",
                f"将可恢复归档 {len(targets)} 项（{_size_str(total)}）：\n\n{preview}\n\n确认执行以上同一清单？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.btn_del_sel.setEnabled(False)
        self.btn_del_all.setEnabled(False)
        self.progress.setValue(0)
        self.trash_worker = TrashWorker(targets, journal=self.journal, reason="删除暂存")
        self.trash_worker.log.connect(self.log.log)
        self.trash_worker.progress.connect(self.progress.setValue)
        self.trash_worker.finished.connect(self._on_trashed)
        start_task(self, self.trash_worker)

    def _planned_targets(self, selected_only: bool) -> list:
        targets: list = []
        scope = self.tabs.currentIndex()
        if scope == 2:
            return []
        if scope == 1:
            paths = (
                [item.data(Qt.UserRole) for item in self.manual_list.selectedItems()]
                if selected_only
                else self.manual_list.get_all_paths()
            )
            targets = [(path, None) for path in paths]
        elif selected_only:
            for row in {item.row() for item in self.table.selectedItems()}:
                item = self.table.item(row, 0)
                path = item.data(Qt.UserRole) if item else None
                if path:
                    size = next(
                        (entry["size"] for entry in self._delete_rows if entry["path"] == path),
                        None,
                    )
                    targets.append((path, size))
        else:
            targets = [(entry["path"], entry["size"]) for entry in self._delete_rows]
        protected = {
            row["path"]
            for row in self._delete_rows
            if row.get("num") in getattr(self, "_ai_keep_nums", set())
        }
        protected -= getattr(self, "_ai_override_paths", set())
        from utils import scoring

        fresh_labels = self.store.all()
        protected |= {
            row["path"]
            for row in self._delete_rows
            if fresh_labels.get(row.get("item_id")) in scoring.POSITIVE_LABELS
        }
        targets = [(path, size) for path, size in targets if path and path not in protected]
        # 去重
        seen = set()
        uniq = []
        for p, s in targets:
            if p not in seen:
                seen.add(p)
                uniq.append((p, s))
        return uniq

    def _override_ai_keep(self):
        if busy(self):
            return
        paths = {
            item.data(Qt.UserRole) for item in self.table.selectedItems() if item.data(Qt.UserRole)
        }
        paths &= {
            row["path"]
            for row in self._delete_rows
            if row.get("num") in getattr(self, "_ai_keep_nums", set())
        }
        if not paths:
            return
        if (
            QMessageBox.question(
                self,
                "明确覆盖AI保留建议",
                "\n".join(sorted(paths))
                + "\n\n这些项将重新允许进入删除计划；之后仍需审核执行。确认覆盖？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            == QMessageBox.Yes
        ):
            self._ai_override_paths = getattr(self, "_ai_override_paths", set()) | paths

    def _on_trashed(self, stats: dict) -> None:
        events.media_changed.emit([])
        self.btn_del_sel.setEnabled(True)
        self.btn_del_all.setEnabled(True)
        self.progress.setValue(
            0 if stats.get("error") or stats.get("fail") or stats.get("cancelled") else 100
        )
        if stats.get("error") or stats.get("fail"):
            QMessageBox.warning(
                self,
                "归档部分失败",
                str(stats.get("error") or stats.get("fails") or "部分文件归档失败，原文件已保留。"),
            )
        # 从清单移除已移入可恢复隔离区的（按存在性过滤）
        self._delete_rows = [d for d in self._delete_rows if os.path.exists(d["path"])]
        self._manual_paths = {p for p in self._manual_paths if os.path.exists(p)}
        for idx in range(self.manual_list.count() - 1, -1, -1):
            if not os.path.exists(self.manual_list.item(idx).data(Qt.UserRole)):
                self.manual_list.takeItem(idx)
        self._render_table()
        self._refresh_journal()

    # ── AI 二次复核 ──
    def _select_all(self) -> None:
        if self.tabs.currentIndex() == 1:
            self.manual_list.selectAll()
        elif self.tabs.currentIndex() == 0:
            self.table.selectAll()

    def _ai_recheck(self) -> None:
        if busy(self):
            return
        from workers.ai import AIRecheckWorker

        if not self._delete_rows:
            QMessageBox.information(self, "提示", "请先加载建议删除清单")
            return
        if not self.cfg.ai_enabled:
            QMessageBox.information(
                self,
                "未启用 AI",
                "请在「设置」→「本地 AI 二次校验」启用并配置本地 LLM（如 Ollama）。",
            )
            return
        candidates = [
            {
                "item_id": d.get("item_id") or d["path"],
                "num": d["num"],
                "name": d["name"],
                "hit": d["hit"],
                "reason": d["reason"],
                "actors": d.get("actors", []) or [],
                "studios": d.get("studios", []) or [],
                "genres": d.get("genres", []) or [],
            }
            for d in self._delete_rows
        ]
        self.btn_ai.setEnabled(False)
        self._ai_keep_nums = set()
        self._ai_override_paths = set()
        self.log.log_info(f"🤖 正在请求本地 LLM 复核前 {min(len(candidates), 25)} 条（防误删）…")
        self.ai_worker = AIRecheckWorker(
            candidates, self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model
        )
        self.ai_worker.log.connect(self.log.log)
        self.ai_worker.result.connect(self._collect_keep)
        self.ai_worker.verdict.connect(self._collect_keep_verdict)
        self.ai_worker.finished.connect(self._on_ai_recheck_done)
        start_task(self, self.ai_worker)

    def _collect_keep(self, text: str) -> None:
        import re as _re

        for line in _re.split(r"[\r\n]+", str(text)) if text else []:
            m = _re.match(r"\s*([A-Za-z0-9\-]+)\s*[:：]\s*KEEP\b", line.strip().upper())
            if m:
                num = line.strip().split(":")[0].strip().split("：")[0].strip()
                self._ai_keep_nums.add(num)

    def _collect_keep_verdict(self, data):
        if data.get("verdict") == "KEEP":
            item_id = data.get("item_id")
            for row in self._delete_rows:
                if (row.get("item_id") or row["path"]) == item_id:
                    self._ai_keep_nums.add(row.get("num", ""))

    def _on_ai_recheck_done(self, data=None) -> None:
        self.btn_ai.setEnabled(True)
        if isinstance(data, dict) and data.get("error"):
            self.log.log_error("AI 复核失败：" + str(data["error"]))
            self._ai_keep_nums = set()
            return
        keeps = [num for num in self._ai_keep_nums]
        if keeps:
            warning = QColor(theme.color("warning"))
            # 按"文件路径"匹配行（表格可排序，行号≠原索引，否则会标黄错行）
            keep_paths = {d["path"] for d in self._delete_rows if d["num"] in self._ai_keep_nums}
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0) and self.table.item(r, 0).data(Qt.UserRole) in keep_paths:
                    for c in range(self.table.columnCount()):
                        it = self.table.item(r, c)
                        if it:
                            it.setForeground(warning)
                            if c == 0:
                                it.setText((it.text() or "") + " ⚠AI建议保留")
            self.log.log_warning(
                f"AI 误删过滤：{len(keeps)} 条被判为「可能有用/别删」已标黄——"
                "删除前请在「删除选中」时避开这些行，或取消它们的勾选。"
            )
            QMessageBox.warning(
                self,
                "AI 误删过滤提醒",
                f"AI 判定以下 {len(keeps)} 条建议删除作品可能其实有用(已标黄)：\n"
                f"{', '.join(keeps[:20])}\n\n删除时请避开这些行，避免误删。",
            )
        else:
            self.log.log_success("AI 复核完成：未发现明显误删。")

    # ── 渲染 ──
    def _render_table(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        for d in self._delete_rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            # 封面列(0) 用 CoverThumbDelegate，把路径放 DecorationRole
            cover_it = QTableWidgetItem()
            cover_it.setData(Qt.DecorationRole, d.get("cover") or "")
            cover_it.setData(Qt.UserRole, d["path"])
            self.table.setItem(r, 0, cover_it)
            vals = [d["num"], d["name"], d["hit"], d["reason"], _size_str(d["size"])]
            for c, v in enumerate(vals, start=1):
                it = QTableWidgetItem(str(v))
                it.setData(Qt.UserRole, d["path"])
                it.setToolTip(d["path"])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        total = sum(d["size"] or 0 for d in self._delete_rows)
        self.summary.setText(
            f"建议删除 {len(self._delete_rows)} 条（约 {_size_str(total)}）"
            f" ｜ 手动添加 {len(self._manual_paths)} 项"
            f" ｜ 已删除记录 {self.journal.count()} 条（约 {_size_str(self.journal.total_size())}）"
        )

    def _refresh_journal(self) -> None:
        self.journal_tab.setRowCount(0)
        for rec in self.journal.all():
            r = self.journal_tab.rowCount()
            self.journal_tab.insertRow(r)
            vals = [rec["deleted_at"], _size_str(rec["size_bytes"]), rec["reason"], rec["path"]]
            for c, v in enumerate(vals):
                self.journal_tab.setItem(r, c, QTableWidgetItem(str(v)))

    def shutdown(self) -> None:
        stop_page(self)
