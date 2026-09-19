"""删除暂存页 - 建议删除清单 → 一键移入回收站(可恢复) + 删除日志。

删除红线：
- 只移入回收站，绝不物理删除（失败保留原文件并提示）
- 破坏性操作二次确认
- 全程日志留痕 + 落库记录
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QMessageBox, QGroupBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTabWidget, QFileDialog,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import LogPanel, DragDropListWidget, CoverThumbDelegate
from utils import scoring
from utils.labels import LabelStore
from utils.trash import DeleteJournal
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
        self.store = LabelStore(DATA_DIR / "smart.db")
        self.journal = DeleteJournal(DATA_DIR / "trash_journal.db")
        self.analyzer: AnalyzeWorker | None = None
        self.trash_worker: TrashWorker | None = None
        self.ai_worker = None
        self.labels: dict = self.store.all()
        self._delete_rows: list[dict] = []      # 建议删除(自动+人工标DELETE)
        self._manual_paths: set[str] = set()    # 手动添加的文件
        self._setup_ui()

    # ── UI ──
    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("删除暂存")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "这里是「建议删除」清单（自动零命中 + 垃圾附属文件 + 你在打标页标了删除的）。\n"
            "删除只移入回收站（可恢复），不会物理删除；操作前会二次确认并留痕。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        act = QHBoxLayout()
        self.btn_load = QPushButton("🔍 加载建议删除清单")
        self.btn_load.setProperty("cssClass", "accent")
        self.btn_load.clicked.connect(self._load)
        act.addWidget(self.btn_load)
        self.btn_del_sel = QPushButton("🗑️ 移入回收站(选中)")
        self.btn_del_sel.clicked.connect(lambda: self._trash(selected_only=True))
        act.addWidget(self.btn_del_sel)
        self.btn_del_all = QPushButton("🗑️ 移入回收站(全部)")
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
        act.addStretch()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setValue(0)
        act.addWidget(self.progress)
        root.addLayout(act)

        self.summary = QLabel("尚未加载清单")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        tabs = QTabWidget()
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
        if self.analyzer and self.analyzer.isRunning():
            return
        self.btn_load.setEnabled(False)
        self.log.clear()
        self.progress.setValue(0)
        self.analyzer = AnalyzeWorker(
            self.cfg, self.labels,
            exclude_keywords=self.cfg.exclude_path_keywords,
            th_keep=self.cfg.score_th_keep / 100, th_delete=self.cfg.score_th_delete / 100)
        self.analyzer.log.connect(self.log.log)
        self.analyzer.progress.connect(self.progress.setValue)
        self.analyzer.finished.connect(self._on_loaded)
        self.analyzer.start()

    def _on_loaded(self, data: dict) -> None:
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
            self._delete_rows.append({
                "num": it.num or "", "name": it.name,
                "hit": f"{r.s_content:.0%}", "reason": r.reason,
                "size": it.size, "path": it.path, "cover": it.cover,
                "actors": list(it.actors), "studios": list(it.studios),
                "genres": list(it.genres),
            })
        self._render_table()
        self.log.log_success(f"建议删除 {len(self._delete_rows)} 条（排除附属文件 {skipped} 条）")

    # ── 手动添加 ──
    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要删除(移入回收站)的文件",
            "", "视频 (*.mp4 *.mkv *.avi *.ts *.mov *.wmv *.flv *.rmvb);;所有文件 (*.*)")
        self._add_paths(files)

    def _add_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要删除(移入回收站)的文件夹")
        if d:
            self._add_paths([d])

    def _add_paths(self, paths: list) -> None:
        for p in paths:
            p = os.path.normpath(p)
            if os.path.exists(p):
                self._manual_paths.add(p)
                self.manual_list.add_item(os.path.basename(p), p)
        self.log.log_info(f"手动添加 {len(paths)} 项")

    # ── 删除执行 ──
    def _trash(self, selected_only: bool) -> None:
        targets: list = []
        if selected_only:
            # 关键修复：排序后 selectedItems().row() 与 _delete_rows 索引不再对应，
            # 必须从单元格 Qt.UserRole 里取文件路径，而不是用行号去映射。
            seen_rows: set[int] = set()
            for it in self.table.selectedItems():
                r = it.row()
                if r in seen_rows:
                    continue
                seen_rows.add(r)
                path = it.data(Qt.UserRole)
                if path:
                    # 从 _delete_rows 里按路径找回 size（用 path 兜底）
                    size = next((d["size"] for d in self._delete_rows
                                 if d["path"] == path), None)
                    targets.append((path, size))
        else:
            targets = [(d["path"], d["size"]) for d in self._delete_rows]
            targets += [(p, None) for p in self._manual_paths]
        # 去重
        seen = set()
        uniq = []
        for p, s in targets:
            if p not in seen:
                seen.add(p)
                uniq.append((p, s))
        if not uniq:
            QMessageBox.information(self, "提示", "没有可删除的项")
            return

        total = sum((s or 0) for _, s in uniq)
        warn = (
            f"⚠️ 此操作非常危险，可能导致不可逆的数据丢失！\n\n"
            f"将把 {len(uniq)} 个文件/文件夹移入【回收站】\n"
            f"预计释放约 {_size_str(total) or '未知'} 空间\n\n"
            f"回收站中可恢复；本工具不会物理删除。\n确定继续？"
        )
        if QMessageBox.question(self, "确认移入回收站", warn) != QMessageBox.Yes:
            return

        self.btn_del_sel.setEnabled(False)
        self.btn_del_all.setEnabled(False)
        self.progress.setValue(0)
        self.trash_worker = TrashWorker(uniq, journal=self.journal, reason="删除暂存")
        self.trash_worker.log.connect(self.log.log)
        self.trash_worker.progress.connect(self.progress.setValue)
        self.trash_worker.finished.connect(self._on_trashed)
        self.trash_worker.start()

    def _on_trashed(self, stats: dict) -> None:
        self.btn_del_sel.setEnabled(True)
        self.btn_del_all.setEnabled(True)
        self.progress.setValue(100)
        # 从清单移除已移入回收站的（按存在性过滤）
        self._delete_rows = [d for d in self._delete_rows if os.path.exists(d["path"])]
        self._manual_paths = {p for p in self._manual_paths if os.path.exists(p)}
        self._render_table()
        self._refresh_journal()

    # ── AI 二次复核 ──
    def _select_all(self) -> None:
        if self.table.rowCount():
            self.table.selectAll()

    def _ai_recheck(self) -> None:
        from workers.ai import AIRecheckWorker
        if not self._delete_rows:
            QMessageBox.information(self, "提示", "请先加载建议删除清单")
            return
        if not self.cfg.ai_enabled:
            QMessageBox.information(self, "未启用 AI",
                                    "请在「设置」→「本地 AI 二次校验」启用并配置本地 LLM（如 Ollama）。")
            return
        candidates = [{
            "num": d["num"], "name": d["name"], "hit": d["hit"], "reason": d["reason"],
            "actors": d.get("actors", []) or [], "studios": d.get("studios", []) or [],
            "genres": d.get("genres", []) or [],
        } for d in self._delete_rows]
        self.btn_ai.setEnabled(False)
        self._ai_keep_nums = set()
        self.log.log_info(f"🤖 正在请求本地 LLM 复核前 {min(len(candidates), 25)} 条（防误删）…")
        self.ai_worker = AIRecheckWorker(candidates, self.cfg.ai_base_url,
                                         self.cfg.ai_key, self.cfg.ai_model)
        self.ai_worker.log.connect(self.log.log)
        self.ai_worker.result.connect(self._collect_keep)
        self.ai_worker.finished.connect(self._on_ai_recheck_done)
        self.ai_worker.start()

    def _collect_keep(self, text: str) -> None:
        import re as _re
        for line in (_re.split(r"[\r\n]+", str(text)) if text else []):
            m = _re.match(r"\s*([A-Za-z0-9\-]+)\s*[:：]\s*KEEP\b", line.strip().upper())
            if m:
                num = line.strip().split(":")[0].strip().split("：")[0].strip()
                self._ai_keep_nums.add(num)

    def _on_ai_recheck_done(self) -> None:
        self.btn_ai.setEnabled(True)
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
                "删除前请在「删除选中」时避开这些行，或取消它们的勾选。")
            QMessageBox.warning(
                self, "AI 误删过滤提醒",
                f"AI 判定以下 {len(keeps)} 条建议删除作品可能其实有用(已标黄)：\n"
                f"{', '.join(keeps[:20])}\n\n删除时请避开这些行，避免误删。")
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
        for w in (self.analyzer, self.trash_worker, getattr(self, "ai_worker", None)):
            if w and w.isRunning():
                w.stop() if hasattr(w, "stop") else None
                w.wait(3000)
