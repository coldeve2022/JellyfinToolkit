"""重复版本处理页 - 同番号分组，留优删差(移入可恢复隔离区)；CD 分卷保护。

依据 scoring.dedup_groups：
- 真重复（同番号 >=2 且非全 CD）→ 组内按 无码/完整/大/新 排序，差版本一键可恢复隔离区
- CD 全分卷组 → 保护，不自动处理，提示整体查看

本页改进（修复"无法手动选 + 只显示名称 + 误识别附属文件"）：
1. `dedup_groups` 已排除 theme/fanart/backdrop/trailer/极小占位等 Jellyfin 附属文件，
   不再把片头/背景当成"同番号重复版本"而误删正片。
2. 每组每一版本行都带【勾选框】+ 完整【标签/演员/类型】辅助信息 + 封面，
   默认勾选"建议删差"版本；可手动勾选/取消，删除只删勾选版本。
"""

from __future__ import annotations

from ui.services import busy, start_task, stop_page, events, state_context

import os

from PySide6.QtCore import Qt, QTimer
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
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QAbstractItemView,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import LogPanel
from utils import scoring
from utils.labels import LabelStore
from utils.trash import DeleteJournal
from ui.services import analysis_worker, accept_snapshot
from workers.scorer import AnalyzeWorker
from workers.trash import TrashWorker

ROLE_ITEM_ID = Qt.UserRole + 1  # 版本 item id
ROLE_PATH = Qt.UserRole + 2  # 版本文件路径
ROLE_SIZE = Qt.UserRole + 3  # 版本字节数
ROLE_IS_LOSER = Qt.UserRole + 4  # 是否系统建议删差


def _size_str(n: int | None) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n else "—"


def _feat_summary(it) -> str:
    """版本辅助信息：演员/厂商/类型/标签（供用户区分保留哪个）。"""
    a = ", ".join(str(x) for x in it.actors[:4]) if it.actors else ""
    s = ", ".join(str(x) for x in it.studios[:2]) if it.studios else ""
    g = ", ".join(str(x) for x in it.genres[:3]) if it.genres else ""
    t = ", ".join(str(x) for x in it.tags[:6]) if it.tags else ""
    parts = []
    if a:
        parts.append(f"演员:{a}")
    if s:
        parts.append(f"厂商:{s}")
    if g:
        parts.append(f"类型:{g}")
    if t:
        parts.append(f"标签:{t}")
    if not parts:
        return "无标签/演员信息（仅凭名称、大小、码率判断）"
    return " ｜ ".join(parts)


class DedupPage(QWidget):
    """重复版本处理页。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._state_context = state_context(cfg)
        self.store = LabelStore(DATA_DIR / "smart.db", context=state_context(self.cfg))
        self.journal = DeleteJournal(DATA_DIR / "trash_journal.db")
        self.analyzer: AnalyzeWorker | None = None
        self.trash_worker: TrashWorker | None = None
        self.labels: dict = self.store.all()
        self.groups: list[dict] = []
        self._item2id: dict[str, str] = {}  # path -> item id（查勾选）
        self._in_check_sync: bool = False  # 防止勾选联动递归触发
        self._ai_predictions = {}
        self._setup_ui()
        events.labels_changed.connect(self._refresh_labels)

    def _refresh_labels(self):
        self._ai_predictions = {}
        self.store = LabelStore(DATA_DIR / "smart.db", context=state_context(self.cfg))
        self.labels = self.store.all()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("重复版本处理")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "同一番号多版本自动分组：无码/破解版 > 有码版，高分辨率 > 低，完整 > 分卷，新 > 旧。\n"
            "每一行都可手动勾选「删除」/取消，勾选的版本移入可恢复隔离区（可恢复）。"
            "CD 分卷组是同一作品分集，已保护，不会自动勾选。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        act = QHBoxLayout()
        self.btn_load = QPushButton("🔍 加载重复分组")
        self.btn_load.setProperty("cssClass", "accent")
        self.btn_load.clicked.connect(self._load)
        act.addWidget(self.btn_load)
        self.btn_reset = QPushButton("♻️ 恢复默认勾选")
        self.btn_reset.setToolTip("重新按「建议删差」规则勾选每个版本，取消你手动改动。")
        self.btn_reset.clicked.connect(self._reset_checks)
        act.addWidget(self.btn_reset)
        self.btn_all = QPushButton("🗑️ 删除勾选版本 (移入可恢复隔离区)")
        self.btn_all.clicked.connect(self._trash_checked)
        act.addWidget(self.btn_all)
        self.btn_ai_dedup = QPushButton("🤖 AI 判定重复组")
        self.btn_ai_dedup.setToolTip(
            "AI给出逐组建议。你明确采纳后才改变勾选，删除仍须审阅路径计划。"
        )
        self.btn_ai_dedup.clicked.connect(self._ai_dedup)
        act.addWidget(self.btn_ai_dedup)
        adopt_ai = QPushButton("审阅并采纳 AI 勾选建议…")
        adopt_ai.clicked.connect(self._apply_ai_predictions)
        act.addWidget(adopt_ai)
        act.addStretch()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setValue(0)
        act.addWidget(self.progress)
        root.addLayout(act)

        self.summary = QLabel("尚未加载")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["版本文件", "辅助标签 / 演员 / 类型", "大小", "路径"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.header().setDefaultSectionSize(360)
        self.tree.setColumnWidth(1, 420)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemChanged.connect(self._on_item_check_changed)  # 组头全选 ↔ 子项联动
        root.addWidget(self.tree, 1)

        g = QGroupBox("日志")
        gl = QVBoxLayout(g)
        self.log = LogPanel()
        self.log.setMaximumHeight(130)
        gl.addWidget(self.log)
        root.addWidget(g)

    def _load(self) -> None:
        self._refresh_labels()
        if busy(self):
            return
        if self.analyzer and self.analyzer.isRunning():
            return
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
        self.progress.setValue(100 if "items" in data else 0)
        if "error" in data:
            QMessageBox.warning(self, "错误", str(data["error"]))
            return
        self._ai_predictions = {}
        from utils.labels import legacy_protection_labels

        self._legacy_protection = legacy_protection_labels(
            DATA_DIR / "smart.db", data["items"], self.labels, cfg=self.cfg
        )
        self.groups = scoring.dedup_groups(
            data["items"], labels={**self._legacy_protection, **self.labels}
        )
        self._render_tree()
        n_group = len(self.groups)
        n_loser = sum(len(g["losers"]) for g in self.groups)
        self.summary.setText(
            f"重复组 {n_group} 个 ｜ 建议删差版本 {n_loser} 条 ｜ "
            f"已删除记录 {self.journal.count()} 条"
        )
        self.log.log_success(
            f"发现重复组 {n_group} 个，差版本 {n_loser} 条（已排除 theme/fanart/预告等附属文件）"
        )

    # ── 树渲染 ──
    def _on_item_check_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """组头「全选」勾选框 ↔ 组内子项 双向联动。

        ⚠ PySide6 信号时序坑：itemChanged 发出时 item.checkState() 还是旧值
        （实测 setCheckState 后 handler 内读到的仍是修改前状态），所以不能在
        handler 里直接读 item.checkState() 判断方向，必须用 QTimer 延迟到事件循环，
        此时 checkState() 已是新值。
        """
        if column != 0 or self._in_check_sync:
            return
        # 延迟到下一个事件循环，确保 item.checkState() 已更新为点击后的新值
        QTimer.singleShot(0, lambda: self._apply_check_sync(item))

    def _apply_check_sync(self, item: QTreeWidgetItem) -> None:
        if self._in_check_sync:
            return
        self._in_check_sync = True
        try:
            grp = item.parent()
            if grp is None:
                # 组头变化 → 同步所有子项到组头当前状态
                check = item.checkState(0)
                for i in range(item.childCount()):
                    child = item.child(i)
                    if child.checkState(0) != check:
                        child.setCheckState(0, check)
            else:
                # 子项变化 → 反向更新组头状态（全勾/全不勾/三态半选）
                # 仅当真重复组头可勾选时更新（全 CD 保护组组头无勾选框）
                if not (grp.flags() & Qt.ItemIsUserCheckable):
                    return
                yes = 0
                cnt = grp.childCount()
                for i in range(cnt):
                    if grp.child(i).checkState(0) == Qt.Checked:
                        yes += 1
                if yes == 0:
                    grp.setCheckState(0, Qt.Unchecked)
                elif yes == cnt:
                    grp.setCheckState(0, Qt.Checked)
                else:
                    grp.setCheckState(0, Qt.PartiallyChecked)
        finally:
            self._in_check_sync = False

    def _reset_checks(self) -> None:
        """重新按"建议删差"规则勾选每个版本，取消手动改动。"""
        self._in_check_sync = True
        try:
            start = 0
            for g in self.groups:
                grp_item = self.tree.topLevelItem(start)
                start += 1
                if grp_item is None:
                    continue
                # 同步组头默认态（全 CD 组无勾选框）
                if grp_item.flags() & Qt.ItemIsUserCheckable:
                    grp_item.setCheckState(0, Qt.Unchecked)
                for i, it in enumerate(g["items"]):
                    child = grp_item.child(i)
                    if child is None:
                        continue
                    loser = it.id in g["losers"] and not g["all_cd"]
                    child.setCheckState(0, Qt.Checked if loser else Qt.Unchecked)
        finally:
            self._in_check_sync = False
        self.log.log_info("已恢复默认勾选（按建议删差规则）。")

    def _qcolor(self, key: str):
        return QColor(theme.color(key))

    def _render_tree(self) -> None:
        checks = {}
        for row in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(row)
            for idx in range(top.childCount()):
                child = top.child(idx)
                checks[child.data(0, ROLE_PATH)] = child.checkState(0)
        self.tree.clear()
        self._item2id = {}
        muted = theme.color("text_muted")
        danger = theme.color("danger")
        success = theme.color("success")
        self._in_check_sync = True  # 填充期间屏蔽勾选联动
        try:
            for g in self.groups:
                names = sorted({name for item in g["items"] for name in getattr(item, "library_names", [])})
                scope = " ｜ " + " / ".join(names) if names else ""
                grp_item = QTreeWidgetItem([f"{g['num']}  ({len(g['items'])}个版本){scope}", "", "", ""])
                if g["all_cd"]:
                    # 全 CD 分卷组：保护，移除"全选"勾选框（避免误删分卷）。
                    # QTreeWidgetItem 默认 flags 就含 ItemIsUserCheckable（实测），必须显式移除。
                    grp_item.setFlags(grp_item.flags() & ~Qt.ItemIsUserCheckable)
                    grp_item.setText(1, "全 CD 分卷(保护)")
                    grp_item.setForeground(1, QColor(muted))
                else:
                    # 真重复组：组头"全选"勾选框（勾=整组删除，默认不勾）
                    grp_item.setFlags(grp_item.flags() | Qt.ItemIsUserCheckable)
                    grp_item.setCheckState(0, Qt.Unchecked)
                    grp_item.setToolTip(0, "勾选=整组删除（通常不建议，改用子行分别勾）")
                    grp_item.setText(1, "真重复（建议删差）")
                self.tree.addTopLevelItem(grp_item)

                for it in g["items"]:
                    is_loser = it.id in g["losers"] and not g["all_cd"]
                    desc = []
                    desc.append("无码" if it.is_uncensored else "有码")
                    if it.is_cd:
                        desc.append(f"CD{it.cd_index or ''}")
                    desc.append("完整" if not it.is_cd else "分卷")
                    desc.append(f"{it.year or '?'}")
                    name_line = f"{os.path.basename(it.path)}"
                    child = QTreeWidgetItem(
                        [
                            name_line,
                            _feat_summary(it),
                            _size_str(it.size),
                            it.path,
                        ]
                    )
                    # 可勾选：默认勾选"建议删差"版本；CD 全分卷组不勾
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(
                        0, checks.get(it.path, Qt.Checked if is_loser else Qt.Unchecked)
                    )
                    child.setToolTip(
                        0, f"{'⚠ 建议删差（保留更优版本）' if is_loser else '保留'}\n{it.path}"
                    )
                    child.setToolTip(1, " ｜ ".join(desc) + " ｜ " + (it.num or "无番号"))
                    child.setToolTip(3, it.path)
                    # 颜色：建议删差红，保留绿
                    child.setForeground(1, QColor(danger if is_loser else success))
                    # 存 id 供删除勾选时反查
                    self._item2id[it.path] = it.id
                    # 存元数据到自定义角色，供删除时取 size
                    child.setData(0, ROLE_ITEM_ID, it.id)
                    child.setData(0, ROLE_PATH, it.path)
                    child.setData(0, ROLE_SIZE, it.size)
                    child.setData(0, ROLE_IS_LOSER, is_loser)
                    grp_item.addChild(child)
            self.tree.expandAll()
        finally:
            self._in_check_sync = False

    # ── 勾选统计与删除 ──
    def _collect_checked(self) -> list[tuple[str, int]]:
        """收集所有被勾选"删除"的版本（不含已勾选为保护态的 CD 整组）。返回 [(path, size)]。"""
        targets: list[tuple[str, int]] = []
        seen = set()
        top_count = self.tree.topLevelItemCount()
        for gi in range(top_count):
            grp = self.tree.topLevelItem(gi)
            c = grp.childCount()
            for i in range(c):
                child = grp.child(i)
                if child.checkState(0) == Qt.Checked:
                    p = child.data(0, ROLE_PATH)
                    if p and p not in seen:
                        seen.add(p)
                        targets.append((p, child.data(0, ROLE_SIZE)))
        return targets

    def _planned_targets(self):
        targets = self._collect_checked()
        selected = {path for path, _ in targets}
        blocked = set()
        for group in self.groups:
            paths = {item.path for item in group["items"]}
            if paths and paths <= selected:
                blocked.update(paths)
            for item in group["items"]:
                if (
                    (
                        self.labels.get(item.id)
                        or getattr(self, "_legacy_protection", {}).get(item.id)
                    )
                    in scoring.POSITIVE_LABELS
                    or getattr(item, "is_favorite", False)
                    or getattr(item, "is_played", False)
                ):
                    blocked.add(item.path)
        return [(path, size) for path, size in targets if path not in blocked]

    def _trash_checked(self) -> None:
        if busy(self, mutating=True):
            return
        targets = self._planned_targets()
        if not targets:
            QMessageBox.information(
                self, "提示", "没有勾选任何要删除的版本。\n先勾选要删的「重复差版本」行。"
            )
            return
        total = sum(s or 0 for _, s in targets)
        warn = (
            f"请审阅以下归档计划，执行后可在任务中心恢复。\n\n"
            f"将把 {len(targets)} 个勾选版本移入可恢复隔离区（约 {_size_str(total)}）\n"
            f"请核对以下实际计划（最后一个版本及受保护作品已排除）：\n"
            + "\n".join(path for path, _ in targets)
            + "\n确认执行同一计划？"
        )
        if QMessageBox.question(self, "确认移入可恢复隔离区", warn) != QMessageBox.Yes:
            return
        self.btn_all.setEnabled(False)
        self.progress.setValue(0)
        self.trash_worker = TrashWorker(targets, journal=self.journal, reason="重复去重(手动勾选)")
        self.trash_worker.log.connect(self.log.log)
        self.trash_worker.progress.connect(self.progress.setValue)
        self.trash_worker.finished.connect(self._on_done)
        start_task(self, self.trash_worker)

    def _on_done(self, stats: dict) -> None:
        self.btn_all.setEnabled(True)
        self.progress.setValue(100)
        self.log.log_success(f"本次：成功 {stats['ok']} / 失败 {stats['fail']}")
        if stats["fail"]:
            QMessageBox.warning(
                self, "部分失败", f"{stats['fail']} 个未能移入可恢复隔离区（原文件已保留）"
            )
        # 刷新已删数量；重载分组以移除已删版本
        self.summary.setText(
            f"重复组 {len(self.groups)} 个 ｜ 已删除记录 {self.journal.count()} 条"
        )
        self._on_loaded_dedup_refresh()

    def _on_loaded_dedup_refresh(self) -> None:
        """删除后重新加载分组（后台），确保界面与磁盘一致。"""
        if self.analyzer and self.analyzer.isRunning():
            return
        self.analyzer = analysis_worker(
            self.cfg,
            self.labels,
            exclude_keywords=self.cfg.exclude_path_keywords,
            th_keep=self.cfg.score_th_keep / 100,
            th_delete=self.cfg.score_th_delete / 100,
        )
        self.analyzer.finished.connect(self._on_loaded)
        start_task(self, self.analyzer)

    # ── AI 判定重复组 ──
    def _ai_dedup(self) -> None:
        if busy(self):
            return
        from workers.ai import AIDedupWorker

        if not self.cfg.ai_enabled:
            QMessageBox.information(
                self, "未启用 AI", "请在「设置」→「本地 AI 二次校验」启用本地 LLM。"
            )
            return
        if not getattr(self, "groups", None):
            QMessageBox.information(self, "无分组", "请先「🔍 加载重复分组」。")
            return
        if getattr(self, "_dedup_ai", None) and self._dedup_ai.isRunning():
            return
        groups = [g for g in self.groups if len(g["items"]) >= 2]
        if not groups:
            QMessageBox.information(self, "无候选", "没有可判定的重复组。")
            return
        payload = [
            {
                "num": g.get("group_id") or g["num"],
                "items": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "size_mb": getattr(it, "size_mb", ""),
                        "is_cd": it.is_cd,
                        "cd_index": it.cd_index,
                        "is_uncensored": it.is_uncensored,
                    }
                    for it in g["items"]
                ],
            }
            for g in groups
        ]
        self.btn_ai_dedup.setEnabled(False)
        self._dedup_ai = AIDedupWorker(
            payload, self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model
        )
        self._dedup_ai.log.connect(self.log.log)
        self._dedup_ai.result.connect(self._note_dedup)
        self._dedup_ai.verdict.connect(self._collect_ai_prediction)
        self._dedup_ai.finished.connect(self._on_dedup_ai_done)
        start_task(self, self._dedup_ai)

    def _note_dedup(self, s: str) -> None:
        self.log.log(str(s)[:160])

    def _collect_ai_prediction(self, data):
        candidates = [group for group in self.groups
                      if (group.get("group_id") or group["num"]) == data.get("num")
                      or group["num"] == data.get("num")]
        if len(candidates) != 1:
            return
        group = candidates[0]
        valid = {item.id for item in group["items"]}
        keeps, deletes = set(data.get("keep_ids") or []), set(data.get("delete_ids") or [])
        if keeps and keeps <= valid and deletes <= valid and not (keeps & deletes):
            self._ai_predictions[group.get("group_id") or group["num"]] = dict(data)
            self.log.log_info(group["num"] + "：" + str(data.get("reason", "")))

    def _apply_ai_predictions(self):
        if busy(self) or not self._ai_predictions:
            return
        details = "\n".join(
            num
            + " | 保留 "
            + ", ".join(row["keep_ids"])
            + " | 删除建议 "
            + ", ".join(row["delete_ids"])
            + " | "
            + row.get("reason", "")
            for num, row in self._ai_predictions.items()
        )
        if (
            QMessageBox.question(
                self,
                "确认采纳AI勾选建议",
                details + "\n\n只更新勾选，文件不会现在删除。确认？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return
        self.tree.blockSignals(True)
        try:
            for idx, group in enumerate(self.groups):
                prediction = self._ai_predictions.get(group.get("group_id") or group["num"])
                if prediction is None or group["all_cd"]:
                    continue
                top = self.tree.topLevelItem(idx)
                for child_idx in range(top.childCount()):
                    child = top.child(child_idx)
                    child.setCheckState(
                        0,
                        Qt.Checked
                        if child.data(0, ROLE_ITEM_ID) in prediction["delete_ids"]
                        else Qt.Unchecked,
                    )
        finally:
            self.tree.blockSignals(False)

    def _on_dedup_ai_done(self, data: dict) -> None:
        self.btn_ai_dedup.setEnabled(True)
        if data.get("error"):
            QMessageBox.warning(self, "AI 去重判定失败", data["error"])
            return
        self.log.log_warning("AI 去重判定完成。请人工核对勾选后点「🗑️ 删除勾选版本」执行。")
        QMessageBox.information(
            self,
            "AI 去重判定完成",
            f"已判定 {data.get('verdicts', 0)} 组。判定建议已写入日志（含每组保留哪个版本）。\n"
            "你可结合日志核对勾选，再点「🗑️ 删除勾选版本」执行删除（进可恢复隔离区可恢复）。",
        )

    def apply_theme(self) -> None:
        """主题切换后按当前主题重绘树（颜色取自 theme）。"""
        for row in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(row)
            for idx in range(group.childCount()):
                child = group.child(idx)
                role = "danger" if child.data(0, ROLE_IS_LOSER) else "success"
                child.setForeground(1, QColor(theme.color(role)))

    def shutdown(self) -> None:
        stop_page(self)

    def on_context_changed(self):
        self._ai_predictions = {}
        self._legacy_protection = {}
        self._item2id = {}
