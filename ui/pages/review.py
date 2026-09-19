"""快速打标页 - 卡片流(仅待复核) + 主动学习闭环 + 自动分类结果表。

分层：
- 已保留(看过/收藏) = 真值，无需处理
- 可能喜欢 / 建议删除 = 系统已取立场，表格可见
- 待复核(边界/无元数据/CD分卷) = 卡片流人工快判
每打一条回填画像、全库重排；绝不把"没看过"当删除依据。
"""
from __future__ import annotations

import os
import re as _re_num
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QFrame, QMessageBox, QGroupBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QTabWidget,
    QStackedWidget, QScrollArea,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import LogPanel, CoverThumb
from utils import scoring
from utils.deck_state import DeckState, ORDER_RANDOM, ORDER_SCORE
from utils.labels import LabelStore
from workers.scorer import AnalyzeWorker, RerankWorker

DEC_TEXT = {
    scoring.DEC_KEEP: "已看过 / 收藏",
    scoring.DEC_LIKE: "可能喜欢",
    scoring.DEC_DELETE: "建议删除",
    scoring.DEC_REVIEW: "待复核",
}
DEC_COLOR_KEY = {
    scoring.DEC_KEEP: "success",
    scoring.DEC_LIKE: "success",
    scoring.DEC_DELETE: "danger",
    scoring.DEC_REVIEW: "warning",
}


class NumItem(QTableWidgetItem):
    """数值型单元格：显示原文("74%")，但排序按 UserRole 数值（修"74%"被按字典序排错）。"""

    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole) or 0) < float(other.data(Qt.UserRole) or 0)
        except (TypeError, ValueError):
            return super().__lt__(other)


_NUM_RE = _re_num.compile(r"^[-\d.]+(?:\s*(?:%|MB|GB|TB))?$")
ROLE_PATH = Qt.UserRole + 1   # 文件路径专用角色（UserRole 留给数值排序）


class ReviewPage(QWidget):
    """快速打标页。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.store = LabelStore(DATA_DIR / "smart.db")
        self.deck_state = DeckState(DATA_DIR / "smart.db")
        self.analyzer: AnalyzeWorker | None = None
        self.reranker: RerankWorker | None = None

        self.items: list = []
        self.results: list = []
        self.labels: dict = self.store.all()
        self.deck_ids: list = []
        self.pos: int = 0
        self._label_anchor: str | None = None   # 打标后重排的定位锚点（当前这张的下一张）
        self._order_mode: str = self.deck_state.get_order()   # score / random
        self._skipped: set = self.deck_state.skipped()        # 跳过(搁置)的 item_id 集合
        self._last_action: tuple | None = None   # (item_id, old_label_or_None)
        self._rerank_pending: bool = False       # 连按时只保留最后一次重排
        self._last_counts: dict | None = None
        self._tab_rows: dict = {"like": [], "del": [], "done": []}
        # AI 批量预判待复核用
        self._ai_review_pool: list = []
        self._ai_review_num2id: dict = {}
        self._ai_review_marks: list = []
        self._ai_review_idx = 0

        self._setup_ui()
        self.setFocusPolicy(Qt.StrongFocus)

    # ── UI ──
    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("快速打标")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "系统已对未看作品自动分层：可能喜欢 / 建议删除 / 待复核。\n"
            "下方卡片只处理「待复核」（边界与信息不足的）。\n"
            "鼠标操作：点赞(=喜欢/保留)　|　点踩(=不喜欢/建议删)　|　跳过=暂时搁置(下次不再重复出现)\n"
            "键盘：→ 喜欢/保留　|　← 不喜欢(建议删)　|　↓ 跳过待定 |　空格=打开文件夹 |　Ctrl+Z 撤销。"
            "每判一条全库自动重排。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        act = QHBoxLayout()
        self.btn_analyze = QPushButton("🚀 开始分析")
        self.btn_analyze.setProperty("cssClass", "accent")
        self.btn_analyze.clicked.connect(self._start)
        act.addWidget(self.btn_analyze)
        self.btn_undo = QPushButton("撤销 (Ctrl+Z)")
        self.btn_undo.clicked.connect(self._undo)
        act.addWidget(self.btn_undo)
        act.addStretch()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.setValue(0)
        act.addWidget(self.progress)
        root.addLayout(act)

        # 排序模式 + 跳过(搁置)恢复
        order_row = QHBoxLayout()
        self._order_btn = QPushButton()
        self._order_btn.clicked.connect(self._toggle_order)
        order_row.addWidget(self._order_btn)
        self._restore_skipped_btn = QPushButton("♻️ 恢复跳过的")
        self._restore_skipped_btn.setToolTip("把之前「跳过/搁置」的作品重新纳回队列，可再次浏览。")
        self._restore_skipped_btn.clicked.connect(self._restore_skipped)
        order_row.addWidget(self._restore_skipped_btn)
        order_row.addStretch()
        self._order_label = QLabel("")
        self._order_label.setProperty("cssClass", "text-muted")
        order_row.addWidget(self._order_label)
        root.addLayout(order_row)
        self._sync_order_ui()

        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        root.addWidget(self.counts_label)

        # 全局工具栏（AI 复核 + 视图切换；作用于下面的列表 tab）
        tool2 = QHBoxLayout()
        self.btn_ai_like = QPushButton("🤖 AI 复核·可能喜欢")
        self.btn_ai_like.clicked.connect(lambda: self._ai_recheck("like"))
        self.btn_ai_del = QPushButton("🤖 AI 复核·建议删除")
        self.btn_ai_del.clicked.connect(lambda: self._ai_recheck("delete"))
        tool2.addWidget(self.btn_ai_like)
        tool2.addWidget(self.btn_ai_del)
        tool2.addStretch()
        self.btn_view = QPushButton("☷ 卡片视图")
        self.btn_view.clicked.connect(self._toggle_view)
        tool2.addWidget(self.btn_view)
        self.btn_ai_review = QPushButton("🤖 AI 批量预判待复核")
        self.btn_ai_review.setToolTip("让 AI 分批判断待复核作品 KEEP/DELETE；判 DELETE 的写入删除暂存(需到删除页确认)，大幅减少人工。")
        self.btn_ai_review.clicked.connect(self._ai_review_and_mark)
        tool2.addWidget(self.btn_ai_review)
        self.btn_ai_score = QPushButton("🧮 AI 语义打分")
        self.btn_ai_score.setToolTip("对边界(待复核)作品做 AI 内容相似度打分并存缓存，"
                                     "下次重排自动与规则命中度融合(算法一部分)。连不上本地 LLM 时自动退回纯规则。")
        self.btn_ai_score.clicked.connect(self._ai_score_all)
        tool2.addWidget(self.btn_ai_score)
        root.addLayout(tool2)

        # 主区：单一 tabs 占满（①待复核快判 独立栏目，②③④ 三个列表占满宽度）
        self.main_tabs = QTabWidget()
        rev_tab = QWidget()
        rl = QVBoxLayout(rev_tab)
        rl.addWidget(QLabel("待复核 · 卡片快判（右箭头=喜欢/保留，左箭头=不喜欢，↓跳过，空格打开文件，Ctrl+Z 撤销）"))
        self.card = QFrame()
        self.card.setProperty("cssClass", "panel-card")
        self.card_lay = QVBoxLayout(self.card)
        self.card_lay.setContentsMargins(18, 18, 18, 18)
        rl.addWidget(self.card, 1)
        self.main_tabs.addTab(rev_tab, "① 待复核·快判")

        self.tab_like = self._make_table(["番号", "名称", "命中度", "理由", "标签/演员"])
        self.tab_del = self._make_table(["番号", "名称", "命中度", "理由", "标签/演员"])
        self.tab_done = self._make_table(["番号", "名称", "打标", "大小"])
        self._list_stack: dict = {}      # key -> QStackedWidget（0表格 / 1卡片）
        self._list_cardlay: dict = {}    # key -> 卡片容器 layout
        for key, table, name in [("like", self.tab_like, "② 可能喜欢"),
                                 ("del", self.tab_del, "③ 建议删除"),
                                 ("done", self.tab_done, "④ 已打标")]:
            container = QWidget()
            cl = QVBoxLayout(container)
            cl.setContentsMargins(0, 0, 0, 0)
            stack = QStackedWidget()
            stack.addWidget(table)
            card_scroll = QScrollArea()
            card_scroll.setWidgetResizable(True)
            card_scroll.setFrameShape(QFrame.NoFrame)
            cc = QWidget()
            ccl = QVBoxLayout(cc)
            ccl.setSpacing(8)
            card_scroll.setWidget(cc)
            stack.addWidget(card_scroll)
            cl.addWidget(stack, 1)
            self._list_stack[key] = stack
            self._list_cardlay[key] = ccl
            self.main_tabs.addTab(container, name)
        root.addWidget(self.main_tabs, 1)

        g = QGroupBox("日志")
        gl = QVBoxLayout(g)
        self.log = LogPanel()
        self.log.setMaximumHeight(90)
        gl.addWidget(self.log)
        root.addWidget(g)

        self._clear_card()
        self._render_counts(None)

    def _make_table(self, headers: list) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        h = t.horizontalHeader()
        # 全部 Interactive + 固定初始宽：拖任意一列只改该列，不带动其它列。
        # 不用 Stretch/stretchLastSection（伸缩列会导致"调一列其余跟着动"）。
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # 番号
        h.setDefaultSectionSize(130)
        h.setMinimumSectionSize(80)
        h.setStretchLastSection(False)
        if t.columnCount() > 1:
            t.setColumnWidth(1, 420)     # 名称列给较宽初始（可再手动拖）
        t.verticalHeader().setVisible(False)
        t.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSortingEnabled(True)
        t.itemDoubleClicked.connect(self._open_row_file)
        return t

    # ── 分析 ──
    def _start(self) -> None:
        if self.analyzer and self.analyzer.isRunning():
            return
        self.btn_analyze.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        self.analyzer = AnalyzeWorker(
            self.cfg, self.labels,
            exclude_keywords=self.cfg.exclude_path_keywords,
            th_keep=self.cfg.score_th_keep / 100, th_delete=self.cfg.score_th_delete / 100)
        self.analyzer.log.connect(self.log.log)
        self.analyzer.progress.connect(self.progress.setValue)
        self.analyzer.finished.connect(self._on_analyzed)
        self.analyzer.start()

    def _on_analyzed(self, data: dict) -> None:
        self.btn_analyze.setEnabled(True)
        self.progress.setValue(100 if "results" in data else 0)
        if "error" in data:
            QMessageBox.warning(self, "错误", str(data["error"]))
            return
        self.items = data["items"]
        self.results = data["results"]
        self.log.log_success(f"分析完成：{len(self.items)} 部。")
        self._rebuild_deck()
        self._render_counts(data.get("counts"))
        self._fill_tables()

    # ── 打标闭环 ──
    def _toggle_order(self) -> None:
        """在「命中度排序」和「随机」之间切换，持久化到 smart.db。"""
        self._order_mode = ORDER_RANDOM if self._order_mode == ORDER_SCORE else ORDER_SCORE
        self.deck_state.set_order(self._order_mode)
        self._sync_order_ui()
        self.log.log_success(f"队列顺序已切换为「{'随机' if self._order_mode == ORDER_RANDOM else '命中度'}」。")
        if self.results:
            self._rebuild_deck()

    def _restore_skipped(self) -> None:
        """把跳过的(搁置)作品全部恢复进队列。"""
        if not self._skipped:
            QMessageBox.information(self, "没有跳过的", "当前没有处于「跳过/搁置」状态的作品。")
            return
        n = len(self._skipped)
        self._skipped.clear()
        self.deck_state.clear_skipped()
        if self.results:
            self._rebuild_deck()
        self.log.log_success(f"已恢复 {n} 部跳过的作品到队列。")

    def _sync_order_ui(self) -> None:
        if not hasattr(self, "_order_btn"):
            return
        random = self._order_mode == ORDER_RANDOM
        self._order_btn.setText("🔀 随机顺序" if random else "📊 命中度顺序")
        self._order_btn.setToolTip("点击切换队列展示顺序。随机可避免反复打开时按同一顺序处理。")
        self._order_label.setText(
            "当前：随机（每次打开/重排都重新洗牌）" if random
            else "当前：命中度升序（先处理不太可能喜欢的）"
        )

    def _rebuild_deck(self, anchor_id: str | None = None) -> None:
        """卡片队列 = 未打标且未搁置(跳过)的「待复核」作品。

        anchor_id: 打标后希望定位到的目标作品 id。若在队列里就定位到它，
        否则退回开头（队列已变，原目标可能已被重排移除/打标掉）。
        解决"打标回开头 / 跳过的又冒出来"：重排不再强制 pos=0，
        而是锚定到当前这张之后的下一张，让打标自然前进。
        """
        seen_labeled = set(self.labels.keys())
        deck = [
            r for r in self.results
            if r.decision == scoring.DEC_REVIEW
            and not r.is_seen
            and r.item.id not in seen_labeled
            and r.item.id not in self._skipped   # 跳过(搁置)的不再出现
        ]
        if self._order_mode == ORDER_RANDOM:
            import random as _r
            _r.shuffle(deck)   # 随机洗牌；配合 skip 持久化，避免反复按同一顺序处理
        else:
            deck.sort(key=lambda r: r.s_content)
        self.deck_ids = [r.item.id for r in deck]
        # 定位锚点：优先当前这一张之后的下一张(anchor_id)，否则回退到队列开头。
        if anchor_id and anchor_id in self.deck_ids:
            self.pos = self.deck_ids.index(anchor_id)
        else:
            self.pos = 0
        self._show_current()

    def _current_index(self) -> int:
        if not self.deck_ids:
            return -1
        return min(self.pos, len(self.deck_ids) - 1)

    def _show_current(self) -> None:
        idx = self._current_index()
        if idx < 0:
            self._clear_card("🎉 待复核队列已清空！可在右侧表格查看自动分类结果。")
            return
        sid = self.deck_ids[idx]
        r = next(x for x in self.results if x.item.id == sid)
        self._render_card(r, idx, len(self.deck_ids))

    def _label(self, action: str) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        item_id = self.deck_ids[idx]
        item = next(x for x in self.items if x.id == item_id)
        old = self.labels.get(item_id)
        if action == "keep":
            new = scoring.LABLE_KEEP
        elif action == "delete":
            new = scoring.LABEL_DELETE
        else:  # skip -> 搁置(跳过)：落库，暂不再出现；可随时「恢复跳过的」
            self._skipped.add(item_id)
            self.deck_state.add_skipped(item_id)
            self._last_action = (item_id, "_skip")
            self.log.log_warning(f"跳过(搁置) {item.name[:30]} —— 已记录，不再按原顺序出现；可点「恢复跳过的」再浏览。")
            # 直接从当前队列移除，定位到下一张；若清空则重排
            self.deck_ids.pop(idx)
            if not self.deck_ids:
                self._rebuild_deck()
            else:
                self.pos = min(self.pos, len(self.deck_ids) - 1)
                self._show_current()
            return
        # 记录"当前这张之后的第一张"作为重排后的定位锚点，
        # 避免打标后重排把 pos 归零导致"回到开头 / 跳过的又冒出来"。
        self._label_anchor = self.deck_ids[idx + 1] if idx + 1 < len(self.deck_ids) else None
        self.store.set(item_id, new)
        self.labels[item_id] = new
        self._last_action = (item_id, old)
        self.log.log_success(f"打标 {item.name[:30]} → {new}")
        self._refresh_after_change()

    def _undo(self) -> None:
        if not self._last_action:
            return
        item_id, old = self._last_action
        # 撤销"跳过(搁置)"：把该作品从 skip 集合移除并恢复进队列
        if old == "_skip":
            self._skipped.discard(item_id)
            self.deck_state.remove_skipped(item_id)
            self._label_anchor = item_id
            self._last_action = None
            self.log.log_warning("已撤销上一条「跳过」，作品恢复到队列。")
            self._refresh_after_change()
            return
        if old is None:
            self.store.remove(item_id)
            self.labels.pop(item_id, None)
        else:
            self.store.set(item_id, old)
            self.labels[item_id] = old
        # 撤销后回到被撤销的那部（它回到待复核队列，重排后定位到它）
        self._label_anchor = item_id
        self._last_action = None
        self.log.log_warning("已撤销上一条打标")
        self._refresh_after_change()

    def _refresh_after_change(self) -> None:
        """标签变化后后台重排。连按时不阻塞界面，只标记待重排。"""
        if self.reranker and self.reranker.isRunning():
            self._rerank_pending = True
            return
        self._rerank_pending = False
        self.reranker = RerankWorker(self.items, self.labels,
                                     th_keep=self.cfg.score_th_keep / 100,
                                     th_delete=self.cfg.score_th_delete / 100)
        self.reranker.finished.connect(self._on_reranked)
        self.reranker.start()

    def _on_reranked(self, data: dict) -> None:
        self.results = data["results"]
        self._rebuild_deck(anchor_id=self._label_anchor)
        self._label_anchor = None
        self._render_counts(data.get("counts"))
        self._fill_tables()
        # 若打标期间又产生了新标签，补一次重排
        if self._rerank_pending:
            self._rerank_pending = False
            self._refresh_after_change()

    # ── AI 语义打分（对边界作品做内容相似度打分并缓存）──
    @staticmethod
    def _profile_line(items) -> str:
        """从"已看/收藏"正样本构建画像主线（喂给 AI 打分用）。"""
        from utils.scoring import sample_weight
        pos = [it for it in items if sample_weight(it) > 0]
        if not pos:
            return "（暂无已看样本，仅按元数据保守判断）"
        from collections import Counter
        actors = Counter(a for it in pos for a in it.actors if a)
        studios = Counter(s for it in pos for s in it.studios if s)
        genres = Counter(g for it in pos for g in it.genres if g)
        tags = Counter(t for it in pos for t in it.tags if t)
        parts = []
        if actors:
            parts.append("Top演员:" + "、".join(a for a, _ in actors.most_common(6)))
        if studios:
            parts.append("Top厂商:" + "、".join(s for s, _ in studios.most_common(5)))
        if genres:
            parts.append("Top类型:" + "、".join(g for g, _ in genres.most_common(6)))
        if tags:
            parts.append("Top标签:" + "、".join(t for t, _ in tags.most_common(8)))
        unc = sum(1 for it in pos if it.is_uncensored)
        parts.append(f"无码偏好:{unc / len(pos):.0%}")
        return "；".join(parts)

    def _ai_score_all(self) -> None:
        if not self.cfg.ai_enabled:
            QMessageBox.information(self, "未启用 AI",
                                    "请在「设置」→「本地 AI 二次校验」启用本地 LLM（如 Ollama）。")
            return
        if not self.results:
            QMessageBox.information(self, "无候选", "请先点击「🚀 开始分析」加载全库评分。")
            return
        if getattr(self, "_score_worker", None) and self._score_worker.isRunning():
            return
        # 候选 = 未看 + 待复核 + 有元数据（非 featureless/junk）
        cand = [
            r.item for r in self.results
            if not r.is_seen and r.item.id not in self.labels
            and not r.is_junk and not r.featureless
            and r.decision == scoring.DEC_REVIEW
        ]
        if not cand:
            QMessageBox.information(self, "无待打分项", "当前没有待复核作品可做语义打分。")
            return
        # 按桶分组，避免 prompt 里混桶造成画像混乱
        from collections import defaultdict
        by_bucket = defaultdict(list)
        for it in cand:
            by_bucket[it.bucket].append(it)
        bucket_cn = {"jav": "常规番号 JAV", "uncensored": "无码/FC2/PPV",
                     "hentai": "里番H动画", "other": "其它"}.get
        line = self._profile_line(self.items)
        self.btn_ai_score.setEnabled(False)
        # 每个桶最多 60 条，总计最多 200 条；其它桶放最后，避免混桶画像
        self._score_pending = []
        buckets_ordered = sorted(by_bucket.keys(), key=lambda b: (b == "other", b))
        for b in buckets_ordered:
            for it in by_bucket[b][:60]:
                self._score_pending.append({
                    "item_id": it.id, "num": it.num or "", "name": it.name,
                    "actors": list(it.actors[:4]), "studios": list(it.studios[:3]),
                    "genres": list(it.genres[:4]), "tags": list(it.tags[:5]),
                    "bucket": b, "bucket_cn": bucket_cn(b) or "其它",
                })
        self._score_pending = self._score_pending[:200]
        self.log.log_success(f"启动 AI 语义打分：{len(self._score_pending)} 条（分桶分批，连不上自动退回纯规则）")
        self._run_score_batch(line)

    def _run_score_batch(self, profile_line: str) -> None:
        from workers.ai_score import AIScoreWorker
        batch = self._score_pending[:25]
        self._score_pending = self._score_pending[25:]
        self._score_worker = AIScoreWorker(
            batch, profile_line, batch[0]["bucket_cn"] if batch else "其它",
            DATA_DIR / "smart.db",
            self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model)
        self._score_worker.log.connect(self.log.log)
        self._score_worker.finished.connect(
            lambda d: self._on_score_batch_done(d, profile_line))
        self._score_worker.start()

    def _on_score_batch_done(self, data: dict, profile_line: str) -> None:
        if data.get("error"):
            self.btn_ai_score.setEnabled(True)
            self.log.log_warning(f"AI 打分中断（{data['error'][:60]}），已打分的保留，评分退回纯规则。")
            self._refresh_after_change()
            return
        if self._score_pending:
            self._run_score_batch(profile_line)
            return
        self.btn_ai_score.setEnabled(True)
        self.log.log_success(f"AI 语义打分完成：{data.get('scored', 0)} 条已缓存。触发一次重排以融合进评分…")
        self._refresh_after_change()
        QMessageBox.information(self, "AI 语义打分完成",
                                f"已对 {data.get('scored', 0)} 条作品做内容相似度打分并缓存。\n"
                                "下次「开始分析」或打标重排会自动与规则命中度融合。\n"
                                "连不上本地 LLM 时评分为纯规则，不受影响。")

    # ── AI 二次复核 ──
    def _build_candidates(self, context: str) -> list:
        cand = []
        for r in self.results:
            it = r.item
            if r.is_seen or it.id in self.labels:
                continue
            if context == "like":
                if r.decision != scoring.DEC_LIKE:
                    continue
            else:
                if r.decision != scoring.DEC_DELETE or r.is_junk:
                    continue
            cand.append({
                "num": it.num or "", "name": it.name,
                "hit": f"{r.s_content:.0%}", "reason": r.reason,
                "actors": list(it.actors[:4]), "studios": list(it.studios[:4]),
                "genres": list(it.genres[:4]),
            })
        return cand[:25]

    def _ai_recheck(self, context: str) -> None:
        if not self.cfg.ai_enabled:
            QMessageBox.information(
                self, "未启用 AI",
                "请在「设置」→「本地 AI 二次校验」勾选启用并配置本地 LLM（如 Ollama）。\n"
                "未启用 AI 时，规则结果照常可用。")
            return
        if not self.results:
            QMessageBox.information(self, "无候选", "请先点击「🚀 开始分析」加载全库评分。")
            return
        if getattr(self, "_ai_worker", None) and self._ai_worker.isRunning():
            return
        cand = self._build_candidates(context)
        if not cand:
            QMessageBox.information(
                self, "无候选",
                f"当前没有「{'可能喜欢' if context == 'like' else '建议删除'}」候选可复核。")
            return
        tag = "可能喜欢" if context == "like" else "建议删除"
        self.log.log_success(f"启动 AI 复核：{len(cand)} 条（{tag}）")
        if context == "like":
            self.btn_ai_like.setEnabled(False)
        else:
            self.btn_ai_del.setEnabled(False)
        from workers.ai import AIRecheckWorker
        self._ai_worker = AIRecheckWorker(
            cand, self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model,
            context=context)
        self._ai_worker.result.connect(lambda s: self.log.log(str(s)[:300]))
        self._ai_worker.log.connect(self.log.log)
        self._ai_worker.finished.connect(lambda d: self._on_ai_done(d, context))
        self._ai_worker.start()

    def _on_ai_done(self, data: dict, context: str) -> None:
        if context == "like":
            self.btn_ai_like.setEnabled(True)
        else:
            self.btn_ai_del.setEnabled(True)
        if data.get("error"):
            self.log.log_error(f"AI 复核失败: {data['error']}")
            QMessageBox.warning(self, "AI 复核失败", str(data["error"]))
            return
        QMessageBox.information(
            self, "AI 复核完成",
            f"已复核 {data.get('ok', 0)}/{data.get('total', 0)} 条，判定详情见日志。")

    # ── AI 批量预判待复核 ──
    def _ai_review_and_mark(self) -> None:
        if not self.cfg.ai_enabled:
            QMessageBox.information(self, "未启用 AI",
                                    "请在「设置」→「本地 AI 二次校验」启用本地 LLM 后使用。")
            return
        if not self.results:
            QMessageBox.information(self, "无候选", "请先点击「🚀 开始分析」。")
            return
        cand = [
            r for r in self.results
            if not r.is_seen and r.item.id not in self.labels
            and not r.is_junk and not r.featureless
            and r.decision == scoring.DEC_REVIEW
        ]
        if not cand:
            QMessageBox.information(self, "无待复核", "当前没有待复核作品。")
            return
        if getattr(self, "_ai_review_worker", None) and self._ai_review_worker.isRunning():
            return
        # 分批（每批 25），串行跑
        self._ai_review_num2id = {r.item.num or "": r.item.id for r in cand}
        self._ai_review_pool = [
            [{
                "num": r.item.num or "", "name": r.item.name,
                "hit": f"{r.s_content:.0%}", "reason": r.reason or "",
                "actors": list(r.item.actors[:4]), "studios": list(r.item.studios[:4]),
                "genres": list(r.item.genres[:4]),
            } for r in cand[i:i + 25]]
            for i in range(0, len(cand), 25)]
        # 只处理一个有意义的量（防止几十批串行太久）：一次最多 8 批(200部)
        self._ai_review_pool = self._ai_review_pool[:8]
        self._ai_review_marks = []
        self._ai_review_idx = 0
        self.btn_ai_review.setEnabled(False)
        self.log.log_success(f"AI 批量预判待复核：共 {len(cand)} 部，将处理前 {min(len(cand), len(self._ai_review_pool) * 25)} 部。")
        self._run_review_batch()

    def _run_review_batch(self) -> None:
        from workers.ai import AIRecheckWorker
        batch = self._ai_review_pool[self._ai_review_idx]
        self._ai_review_worker = AIRecheckWorker(
            batch, self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model,
            context="review")
        self._ai_review_worker.log.connect(self.log.log)
        self._ai_review_worker.result.connect(self._collect_review_verdicts)
        self._ai_review_worker.finished.connect(self._on_review_batch_done)
        self._ai_review_worker.start()

    def _collect_review_verdicts(self, text: str) -> None:
        import re as _re
        for line in (_re.split(r"[\r\n]+", str(text)) if text else []):
            m = _re.match(r"\s*([A-Za-z0-9\-]+)\s*[:：]\s*(KEEP|DELETE|UNSURE)", line.strip().upper())
            if m and m.group(2) == "DELETE":
                num = line.strip().split(":")[0].strip().split("：")[0].strip()
                item_id = self._ai_review_num2id.get(num)
                if item_id and item_id not in self._ai_review_marks:
                    self._ai_review_marks.append(item_id)

    def _on_review_batch_done(self, data: dict) -> None:
        if data.get("error"):
            self.btn_ai_review.setEnabled(True)
            QMessageBox.warning(self, "AI 预判中断", data["error"])
            return
        self._ai_review_idx += 1
        if self._ai_review_idx < len(self._ai_review_pool):
            self._run_review_batch()
            return
        # 全部批次完成：写入删除标签并重排
        for item_id in self._ai_review_marks:
            self.store.set(item_id, scoring.LABEL_DELETE)
        self.btn_ai_review.setEnabled(True)
        n = len(self._ai_review_marks)
        self.log.log_warning(f"AI 预判完成：{n} 部标记为建议删除（已进删除暂存，需到删除页二次确认）。")
        QMessageBox.information(
            self, "AI 批量预判完成",
            f"已处理 {self._ai_review_idx} 批。\nAI 判定 {n} 部待复核作品为可删，"
            "已写入删除暂存（到「删除暂存」页二次确认后再删）。\n其余保留，可继续人工快判。")
        self._refresh_after_change()

    # ── 表格 ──
    @staticmethod
    def _feat_summary(it):
        a = ", ".join(str(x) for x in it.actors[:3]) if it.actors else ""
        s = ", ".join(str(x) for x in it.studios[:2]) if it.studios else ""
        g = ", ".join(str(x) for x in it.genres[:2]) if it.genres else ""
        t = ", ".join(str(x) for x in it.tags[:5]) if it.tags else ""
        if not (a or s or g or t):
            return "无任何元数据（仅标题，无演员/厂商/类型/标签——只能凭标题/大小判断）"
        parts = []
        if a:
            parts.append(f"演员:{a}")
        if s:
            parts.append(f"厂商:{s}")
        if g:
            parts.append(f"类型:{g}")
        if t:
            parts.append(f"标签:{t}")
        return " ｜ ".join(parts)

    def _fill_tables(self) -> None:
        like_rows, del_rows, done_rows = [], [], []

        for r in self.results:
            it = r.item
            mb = f"{it.size_mb} MB" if it.size_mb else ""
            if r.item.id in self.labels:
                done_rows.append((it.num or "", it.name, self.labels[r.item.id], mb, it.path))
            elif r.decision == scoring.DEC_LIKE and not r.is_seen:
                like_rows.append((it.num or "", it.name, f"{r.s_content:.0%}",
                                  r.reason, self._feat_summary(it), it.path))
            elif r.decision == scoring.DEC_DELETE and not r.is_seen and not r.is_junk:
                # 排除 Jellyfin 附属文件(theme/缩略图)，它们不是作品，不该进删除建议
                del_rows.append((it.num or "", it.name, f"{r.s_content:.0%}",
                                 r.reason, self._feat_summary(it), it.path))
        self._set_table(self.tab_like, like_rows)
        self._set_table(self.tab_del, del_rows)
        self._set_table(self.tab_done, done_rows)
        # 存行供卡片视图用
        self._tab_rows = {"like": like_rows, "del": del_rows, "done": done_rows}
        # 路径 → 封面 映射（卡片视图缩略图用）
        self._tab_covers = {it.path: it.cover for it in self.items if it.cover}

    def _set_table(self, table: QTableWidget, rows: list) -> None:
        # 必须先关排序再填充：排序开启时逐行 setItem 会因首列排序移行，导致错位/空白
        table.setSortingEnabled(False)
        table.setRowCount(0)
        ncols = table.columnCount()
        for row in rows:
            r = table.rowCount()
            table.insertRow(r)
            for c in range(ncols):
                v = row[c] if c < len(row) else ""
                s = str(v) if str(v) else "—"
                if _NUM_RE.match(s):
                    # 数值列(命中度/大小)：显示原文、按数值排序
                    item = NumItem(s)
                    try:
                        item.setData(Qt.UserRole, float(s.split()[0]))
                    except (TypeError, ValueError):
                        item.setData(Qt.UserRole, 0.0)
                else:
                    item = QTableWidgetItem(s)
                item.setData(ROLE_PATH, row[-1])     # 文件路径（自定义角色，不占 UserRole）
                item.setToolTip(str(row[-1]))
                table.setItem(r, c, item)
        table.setSortingEnabled(True)

    # ── 表格/卡片视图切换（列表 tab）──
    def _current_list_key(self):
        idx = self.main_tabs.currentIndex()
        if idx == 0:
            return None
        return ("like", "del", "done")[idx - 1]

    def _toggle_view(self) -> None:
        key = self._current_list_key()
        if not key:
            return
        stack = self._list_stack[key]
        if stack.currentIndex() == 0:
            stack.setCurrentIndex(1)
            self.btn_view.setText("☰ 表格视图")
            self._rebuild_cards(key)
        else:
            stack.setCurrentIndex(0)
            self.btn_view.setText("☷ 卡片视图")

    def _rebuild_cards(self, key: str | None = None) -> None:
        key = key or self._current_list_key()
        if not key:
            return
        lay = self._list_cardlay[key]
        self._clear_layout(lay)
        rows = (getattr(self, "_tab_rows", {}) or {}).get(key, []) or []
        if not rows:
            lbl = QLabel("当前分类无数据。")
            lbl.setProperty("cssClass", "text-muted")
            lay.addWidget(lbl)
        else:
            for row in rows:
                lay.addWidget(self._make_result_card(row))
        lay.addStretch()

    def _make_result_card(self, row: tuple) -> QFrame:
        card = QFrame()
        card.setProperty("cssClass", "panel-card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        num, name = row[0], row[1]
        meta2 = row[2] if len(row) > 2 else ""
        meta3 = row[3] if len(row) > 3 else ""
        path = row[-1] if len(row) >= 5 else ""
        top = QHBoxLayout()
        top.setSpacing(10)
        cover = CoverThumb(size=64)
        cover.set_cover((getattr(self, "_tab_covers", {}) or {}).get(path))
        top.addWidget(cover, 0, Qt.AlignTop)
        title = QLabel(f"{num or '—'}　{name or ''}")
        title.setWordWrap(True)
        top.addWidget(title, 1)
        tag = QLabel(str(meta2))
        top.addWidget(tag)
        lay.addLayout(top)
        detail = QLabel(str(meta3))
        detail.setWordWrap(True)
        lay.addWidget(detail)
        card.setToolTip("双击打开文件所在位置" if path else "")
        if path:

            def _open(ev, p=path):
                if ev.button() == Qt.LeftButton and os.path.exists(p):
                    subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
            card.mouseDoubleClickEvent = _open
        return card

    def _open_row_file(self, item: QTableWidgetItem) -> None:
        path = item.data(ROLE_PATH)
        if path and os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')

    # ── 渲染 ──
    @staticmethod
    def _clear_layout(lay) -> None:
        """递归清空一个布局，连嵌套子布局里的 widget 也一起删干净。

        卡片叠加的根因：旧代码只 takeAt(0) 后对 it.widget() 删 widget，
        但内容被放进嵌套子布局(body/detail)时，it.widget() 对子布局返回 None，
        子布局里的旧 QLabel 永远不会被删除，于是新旧卡片层层叠在一起。
        """
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
                continue
            sub = it.layout()
            if sub is not None:
                # 递归清空子布局内的所有 widget
                ReviewPage._clear_layout(sub)

    def _clear_card(self, text: str = "点击「开始分析」加载全库评分…") -> None:
        self._clear_layout(self.card_lay)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        self.card_lay.addWidget(lbl)

    def _render_card(self, r, idx: int, total: int) -> None:
        self._clear_layout(self.card_lay)
        it = r.item
        muted = theme.color("text_muted")
        txt = theme.color("text_primary")

        pos = QLabel(f"待复核 第 {idx + 1} / {total} 部 · 剩余 {total - idx - 1}")
        pos.setStyleSheet(f"font-size:12px; color:{muted};")
        self.card_lay.addWidget(pos)

        # 左侧封面 + 右侧信息
        body = QHBoxLayout()
        body.setSpacing(14)
        cover = CoverThumb(size=140)
        cover.set_cover(it.cover)
        body.addWidget(cover, 0, Qt.AlignTop)
        detail = QVBoxLayout()
        detail.setSpacing(6)

        name = QLabel(it.name)
        name.setStyleSheet(f"font-size:16px; font-weight:bold; color:{txt};")
        name.setWordWrap(True)
        detail.addWidget(name)

        meta = (
            f"番号: {it.num or '—'}　|　年份: {it.year or '—'}　|　大小: {it.size_mb if it.size_mb else '—'} MB\n"
            f"分类: {it.bucket}　|　无码: {'是' if it.is_uncensored else '否'}　|　"
            f"CD分卷: {'是' if it.is_cd else '否'}"
        )
        m = QLabel(meta)
        m.setStyleSheet(f"font-size:12px; color:{muted};")
        m.setWordWrap(True)
        detail.addWidget(m)

        for label, vals in [("演员", it.actors), ("厂商", it.studios),
                            ("类型", it.genres), ("标签", it.tags)]:
            if vals:
                v = QLabel(f"{label}: {', '.join(str(x) for x in vals[:8])}")
                v.setWordWrap(True)
                detail.addWidget(v)

        if r.featureless:
            bar_label = QLabel("该条没有任何可用元数据（无法预测），只能人工判断")
            bar_label.setStyleSheet(f"color:{theme.color('warning')};")
            detail.addWidget(bar_label)
        else:
            pct = int(r.s_content * 100)
            bar_label = QLabel(f"命中度（内容跟你喜欢的有多像）: {pct}%")
            bar_label.setStyleSheet(f"color:{txt};")
            detail.addWidget(bar_label)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(pct)
            detail.addWidget(bar)

        col = theme.color(DEC_COLOR_KEY.get(r.decision, "text_secondary"))
        reason = QLabel(f"依据：{r.reason or '无'}")
        reason.setStyleSheet(f"font-size:12px; color:{theme.color('text_muted')};")
        reason.setWordWrap(True)
        detail.addWidget(reason)

        # 系统建议提示（更新为纯鼠标友好文案）
        sug = QLabel("系统建议: 待复核 — 你来定（点下方按钮 或 用 →/← 键）")
        sug.setStyleSheet(f"font-size:14px; font-weight:bold; color:{col};")
        detail.addWidget(sug)

        body.addLayout(detail, 1)
        self.card_lay.addLayout(body)

        # 纯鼠标操作按钮排（解决"只能快捷键"的痛点，避免强迫键盘操作）
        btns = QHBoxLayout()
        btns.setSpacing(10)
        btn_like = QPushButton("👍 喜欢/保留 (→)")
        btn_like.setProperty("cssClass", "success")
        btn_like.setMinimumHeight(38)
        btn_like.clicked.connect(lambda: self._label("keep"))
        btns.addWidget(btn_like, 1)

        btn_del = QPushButton("👎 不喜欢/建议删 (←)")
        btn_del.setProperty("cssClass", "danger")
        btn_del.setMinimumHeight(38)
        btn_del.clicked.connect(lambda: self._label("delete"))
        btns.addWidget(btn_del, 1)

        btn_skip = QPushButton("⏭ 跳过待定 (↓)")
        btn_skip.setMinimumHeight(38)
        btn_skip.clicked.connect(lambda: self._label("skip"))
        btns.addWidget(btn_skip, 1)

        btn_undo = QPushButton("↩ 撤销 (Ctrl+Z)")
        btn_undo.setMinimumHeight(38)
        btn_undo.clicked.connect(self._undo)
        btns.addWidget(btn_undo, 0)

        self.card_lay.addLayout(btns)

        play = QPushButton("▶ 直接播放/预览 (Enter)")
        play.clicked.connect(self._play_current)
        self.card_lay.addWidget(play)

    def _play_current(self) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        sid = self.deck_ids[idx]
        item = next(x for x in self.items if x.id == sid)
        if item.path and os.path.exists(item.path):
            os.startfile(item.path)  # 用系统默认播放器打开

    def _render_counts(self, counts: dict | None) -> None:
        if counts:
            self._last_counts = counts
        else:
            self.counts_label.setText(
                f"已人工打标: {len(self.labels)} ｜ 待复核: {len(self.deck_ids)}"
                f" ｜ 跳过(搁置): {len(self._skipped)}")
            return
        success = theme.color("success")
        warning = theme.color("warning")
        danger = theme.color("danger")
        accent = theme.color("accent")
        s = (
            f"<b>已保留(看过/收藏):</b> <span style='color:{success}'> {counts.get('keep', 0)}</span>"
            f"　<b>可能喜欢(未看):</b> <span style='color:{success}'> {counts.get('like', 0)}</span>"
            f"　<b>建议删除:</b> <span style='color:{danger}'> {counts.get('delete', 0)}</span>"
            f"　<b>待复核:</b> <span style='color:{warning}'> {counts.get('review', 0)}</span>"
            f"　<b>已人工打标:</b> <span style='color:{accent}'> {len(self.labels)}</span>"
            f"　<b>卡片队列:</b> {len(self.deck_ids)}"
            f"　<b>跳过(搁置):</b> {len(self._skipped)}"
        )
        self.counts_label.setText(s)

    def apply_theme(self) -> None:
        """主题切换后重渲染动态色部分。"""
        if self.results:
            self._render_counts(self._last_counts)
            self._show_current()

    # ── 键盘 ──
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Right:
            self._label("keep")
            return
        if key == Qt.Key_Left:
            self._label("delete")
            return
        if key == Qt.Key_Down:
            self._label("skip")
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._play_current()
            return
        if key == Qt.Key_Space and not (event.modifiers() & Qt.ControlModifier):
            self._open_current_folder()
            return
        if key == Qt.Key_Z and (event.modifiers() & Qt.ControlModifier):
            self._undo()
            return
        super().keyPressEvent(event)

    def _open_current_folder(self) -> None:
        idx = self._current_index()
        if idx < 0:
            return
        sid = self.deck_ids[idx]
        item = next(x for x in self.items if x.id == sid)
        if item.path and os.path.exists(item.path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(item.path)}"')

    def shutdown(self) -> None:
        for w in (self.analyzer, self.reranker, getattr(self, "_ai_worker", None),
                  getattr(self, "_ai_review_worker", None),
                  getattr(self, "_score_worker", None)):
            if w and w.isRunning():
                w.wait(3000)
