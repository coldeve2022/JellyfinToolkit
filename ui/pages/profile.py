"""喜好画像页 - 可视化你的口味：指标卡、无码偏好、Top 演员/类型/标签条形图，
年份/不喜欢表格；「为你推荐」一次展示 20 部高匹配未看作品（卡片流，双击打开文件）；
可导出 CSV。样式全部走 cssClass，随主题切换。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QFrame, QMessageBox, QGroupBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QTabWidget,
    QFileDialog, QTextEdit, QScrollArea,
)

from config import DATA_DIR, ToolkitConfig
from ui import theme
from ui.widgets import LogPanel, HBarChart
from utils import profile as profile_mod
from workers.scorer import AnalyzeWorker
from workers.ai import AIReportWorker, AITasteWorker

_CAPTION = {"actor": "演员", "studio": "厂商", "genre": "类型", "tag": "标签"}


class ProfilePage(QWidget):
    """喜好画像页。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.analyzer: AnalyzeWorker | None = None
        self.items: list = []
        self.profile: dict = {}
        self._rec_pool: list = []          # 推荐完整候选池（供「🎲 随机换一批」）
        self._ai_worker: AIReportWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)

        title = QLabel("喜好画像")
        title.setProperty("cssClass", "page-title")
        root.addWidget(title)

        hint = QLabel(
            "基于你已看过/收藏的作品，可视化你的真实口味，并给出画像驱动的未看推荐。\n"
            "画像随「快速打标」的人工反馈自动更新——你标得越多，画像越准。\n"
            "【画像强度】= 该特征在你已看/收藏作品里累计的行为权重（收藏=2、看完=1、有进度=0.6、播放过=0.3），"
            "只统计真正看过的作品，去掉凑数项。"
        )
        hint.setProperty("cssClass", "subtitle")
        root.addWidget(hint)

        act = QHBoxLayout()
        self.btn_analyze = QPushButton("🚀 生成我的喜好画像")
        self.btn_analyze.setProperty("cssClass", "accent")
        self.btn_analyze.clicked.connect(self._start)
        act.addWidget(self.btn_analyze)
        self.btn_export = QPushButton("💾 导出 CSV 报表")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export)
        act.addWidget(self.btn_export)
        self.btn_ai = QPushButton("🤖 AI 深度分析")
        self.btn_ai.setEnabled(False)
        self.btn_ai.clicked.connect(self._ai_report)
        act.addWidget(self.btn_ai)
        self.btn_ai_taste = QPushButton("🧠 AI 参与分类")
        self.btn_ai_taste.setEnabled(False)
        self.btn_ai_taste.setToolTip("让 AI 提炼喜欢/不喜欢的标签画像并写回评分规则，"
                                     "影响之后的分类（需在设置启用 AI）。")
        self.btn_ai_taste.clicked.connect(self._ai_classify)
        act.addWidget(self.btn_ai_taste)
        self.btn_ai_clear = QPushButton("清除 AI 画像")
        self.btn_ai_clear.setEnabled(False)
        self.btn_ai_clear.setToolTip("清除 AI 提炼的标签画像，评分恢复为纯规则（回滚）。")
        self.btn_ai_clear.clicked.connect(self._ai_clear)
        act.addWidget(self.btn_ai_clear)
        act.addStretch()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setValue(0)
        act.addWidget(self.progress)
        root.addLayout(act)

        # 指标卡
        self.metric_row = QHBoxLayout()
        root.addLayout(self.metric_row)

        # 无码偏好大条
        self.unc_card = self._panel("无码偏好占比（你喜欢的作品里无码占多少）")
        self.unc_chart = HBarChart()
        self.unc_card.layout().insertWidget(1, self.unc_chart)

        # 上半：Top 演员 / Top 标签 条形图（厂商压缩进 Tabs，标签比厂商更有参考价值）
        mid = QHBoxLayout()
        self.actor_card = self._panel("Top 演员")
        self.actor_chart = HBarChart()
        self.actor_card.layout().insertWidget(1, self.actor_chart)
        self.tag_card = self._panel("Top 标签")
        self.tag_chart = HBarChart()
        self.tag_card.layout().insertWidget(1, self.tag_chart)
        mid.addWidget(self.actor_card, 3)
        mid.addWidget(self.tag_card, 2)
        root.addLayout(mid)

        # Tabs：类型/厂商条形 + 表格类
        tabs = QTabWidget()
        self.genre_panel = self._panel("Top 类型 / 题材")
        self.genre_chart = HBarChart()
        self.genre_panel.layout().insertWidget(1, self.genre_chart)
        tabs.addTab(self.genre_panel, "Top 类型")
        self.studio_panel = self._panel("Top 厂商")
        self.studio_chart = HBarChart()
        self.studio_panel.layout().insertWidget(1, self.studio_chart)
        tabs.addTab(self.studio_panel, "Top 厂商")
        self.tab_tags = self._make_table(
            ["标签", "画像强度", "收藏", "看过"],
            tooltips=["该标签在你已看/收藏作品里出现的特征",
                      "行为权重累计：收藏=2、看完=1、有进度=0.6、播放过=0.3（只算真正看过的）",
                      "带此特征的收藏作品数", "带此特征的看完作品数"])
        self.tab_years = self._make_table(["年份", "喜欢作品数"])
        self.tab_neg = self._make_table(["不喜欢特征", "出现次数"])

        # 为你推荐 tab：一次展示 20 部高匹配未看（卡片流，可滚动，双击打开文件）+ 随机换一批
        self.rec_tab = QWidget()
        rt_lay = QVBoxLayout(self.rec_tab)
        rt_lay.setContentsMargins(0, 0, 0, 0)
        tool3 = QHBoxLayout()
        self.btn_rec_shuffle = QPushButton("🎲 随机换一批")
        self.btn_rec_shuffle.setToolTip("从全部高匹配未看候选中按命中加权随机重抽 20 部，供探索（不重复覆盖同一批）。")
        self.btn_rec_shuffle.clicked.connect(self._shuffle_recs)
        tool3.addWidget(self.btn_rec_shuffle)
        tool3.addStretch()
        rt_lay.addLayout(tool3)
        self.rec_scroll = QScrollArea()
        self.rec_scroll.setWidgetResizable(True)
        self.rec_scroll.setFrameShape(QFrame.NoFrame)
        recs_container = QWidget()
        self.recs_lay = QVBoxLayout(recs_container)
        self.recs_lay.setContentsMargins(4, 4, 4, 4)
        self.recs_lay.setSpacing(8)
        self.rec_scroll.setWidget(recs_container)
        rt_lay.addWidget(self.rec_scroll, 1)
        self.btn_rec_shuffle.setEnabled(False)

        for name, t in [("Top 标签", self.tab_tags), ("年份偏好", self.tab_years),
                        ("不喜欢特征", self.tab_neg)]:
            tabs.addTab(t, name)
        tabs.addTab(self.rec_tab, "为你推荐(未看 Top20)")
        self.ai_report = QTextEdit()
        self.ai_report.setReadOnly(True)
        tabs.addTab(self.ai_report, "AI 深度分析报告")
        tabs.setDocumentMode(True)
        root.addWidget(tabs, 1)

        g = QGroupBox("日志")
        gl = QVBoxLayout(g)
        self.log = LogPanel()
        self.log.setMaximumHeight(100)
        gl.addWidget(self.log)
        root.addWidget(g)

        self._empty()

    def _panel(self, title: str) -> QFrame:
        f = QFrame()
        f.setProperty("cssClass", "panel-card")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 12, 14, 12)
        t = QLabel(title)
        t.setProperty("cssClass", "panel-title")
        lay.addWidget(t)
        return f

    def _make_table(self, headers: list, tooltips: list | None = None) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        if tooltips:
            for i, tip in enumerate(tooltips):
                it = t.horizontalHeaderItem(i)
                if it and tip:
                    it.setToolTip(tip)
        h = t.horizontalHeader()
        # 全 Interactive + 固定初始宽：拖任意列只改该列，不带动其它列（去掉 Stretch 联动）
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # 名称/键列自适应
        h.setDefaultSectionSize(120)
        h.setMinimumSectionSize(70)
        h.setStretchLastSection(False)
        if t.columnCount() > 1:
            t.setColumnWidth(1, 320)     # 数值列给足宽（可拖）
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSortingEnabled(True)
        return t

    # ── 分析 ──
    def _start(self) -> None:
        if self.analyzer and self.analyzer.isRunning():
            return
        self.btn_analyze.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.progress.setValue(0)
        self.log.clear()
        self.analyzer = AnalyzeWorker(
            self.cfg, {},
            exclude_keywords=self.cfg.exclude_path_keywords,
            th_keep=self.cfg.score_th_keep / 100, th_delete=self.cfg.score_th_delete / 100)
        self.analyzer.log.connect(self.log.log)
        self.analyzer.progress.connect(self.progress.setValue)
        self.analyzer.finished.connect(self._on_done)
        self.analyzer.start()

    def _on_done(self, data: dict) -> None:
        self.btn_analyze.setEnabled(True)
        self.progress.setValue(100 if "items" in data else 0)
        if "error" in data:
            QMessageBox.warning(self, "错误", str(data["error"]))
            return
        self.items = data["items"]
        from utils.labels import LabelStore
        labels = LabelStore(DATA_DIR / "smart.db").all()
        self.profile = profile_mod.build_profile(self.items, labels, data["results"])
        self._render_profile()
        self.btn_export.setEnabled(True)
        self.btn_ai.setEnabled(True)
        self.btn_ai_taste.setEnabled(True)
        self._refresh_ai_clear()
        self.log.log_success(f"画像已生成：正样本 {self.profile['n_pos']} 部作品 · 覆盖演员 {self.profile['actors_covered']} 位")

    # ── AI 参与分类 ──
    def _refresh_ai_clear(self) -> None:
        from utils.ai_profile import AIProfileStore
        self.btn_ai_clear.setEnabled(bool(AIProfileStore(DATA_DIR / "smart.db").load()))

    def _ai_classify(self) -> None:
        if not self.profile:
            return
        if not self.cfg.ai_enabled:
            QMessageBox.information(
                self, "未启用 AI",
                "请在「设置」→「本地 AI 二次校验」勾选启用并配置本地 LLM（如 Ollama）。\n"
                "未启用时可用纯规则分类。")
            return
        if getattr(self, "_taste_worker", None) and self._taste_worker.isRunning():
            return
        self.btn_ai_taste.setEnabled(False)
        self.ai_report.setPlainText("正在让 AI 提炼喜欢/不喜欢标签画像…")
        self._taste_worker = AITasteWorker(
            self.profile, DATA_DIR / "smart.db",
            self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model)
        self._taste_worker.result.connect(self.ai_report.setPlainText)
        self._taste_worker.log.connect(self.log.log)
        self._taste_worker.finished.connect(self._on_ai_taste_done)
        self._taste_worker.start()

    def _on_ai_taste_done(self, data: dict) -> None:
        self.btn_ai_taste.setEnabled(True)
        if data.get("error"):
            self.log.log_error(f"AI 参与分类失败: {data['error']}")
            return
        self._refresh_ai_clear()
        self.log.log_success(
            f"AI 画像已写入评分规则：喜欢 {data.get('liked', 0)} 项 / 不喜欢 {data.get('disliked', 0)} 项。"
            "重新「生成画像」或打标即生效。")
        QMessageBox.information(
            self, "AI 已参与分类",
            "已写入评分规则（喜欢/不喜欢标签）。\n请点「🚀 生成我的喜好画像」或去「快速打标」触发一次重排，"
            "新的分类就会用上 AI 提炼的标签。可在「清除 AI 画像」回滚。")

    def _ai_clear(self) -> None:
        from utils.ai_profile import AIProfileStore
        AIProfileStore(DATA_DIR / "smart.db").clear()
        self.btn_ai_clear.setEnabled(False)
        self.log.log_warning("已清除 AI 画像，评分规则恢复为纯规则。")
        QMessageBox.information(self, "已清除", "AI 画像已清除，评分规则恢复为纯规则。")

    # ── AI 深度报告 ──
    def _ai_report(self) -> None:
        if not self.profile:
            return
        if not self.cfg.ai_enabled:
            QMessageBox.information(self, "未启用 AI",
                                    "请在「设置」→「本地 AI 二次校验」勾选启用并配置本地 LLM（如 Ollama）。\n"
                                    "未启用 AI 时，规则结果照常可用。")
            return
        self.btn_ai.setEnabled(False)
        self.ai_report.setPlainText("正在请求本地 LLM 分析画像…")
        self._ai_worker = AIReportWorker(
            self.profile, self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model)
        self._ai_worker.result.connect(self.ai_report.setPlainText)
        self._ai_worker.log.connect(self.log.log)
        self._ai_worker.finished.connect(lambda _: self.btn_ai.setEnabled(True))
        self._ai_worker.start()

    # ── 渲染 ──
    def _clear_metric_row(self) -> None:
        while self.metric_row.count():
            it = self.metric_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _empty(self) -> None:
        self._clear_metric_row()
        self.unc_chart.clear(); self.actor_chart.clear()
        self.tag_chart.clear(); self.studio_chart.clear(); self.genre_chart.clear()
        for t in (self.tab_tags, self.tab_years, self.tab_neg):
            t.setRowCount(0)
        while self.recs_lay.count():
            it = self.recs_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._rec_pool = []
        self.btn_rec_shuffle.setEnabled(False)
        hint = QLabel("画像还没生成。点击上方「🚀 生成我的喜好画像」查看你的口味。")
        hint.setProperty("cssClass", "text-muted")
        self.metric_row.addWidget(hint)

    def _render_profile(self) -> None:
        p = self.profile
        self._rec_pool = p.get("rec_candidates") or []
        self.btn_rec_shuffle.setEnabled(bool(self._rec_pool))
        self._clear_metric_row()
        for label, val in [("看过作品", p["n_pos"]), ("收藏", p["n_fav"]),
                           ("看完", p["n_played"]), ("覆盖演员", p["actors_covered"]),
                           ("覆盖厂商", p["studios_covered"]),
                           ("无码偏好", f"{p['unc_ratio']:.0%}")]:
            card = QFrame()
            card.setProperty("cssClass", "metric-card")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(12, 10, 12, 10)
            v = QLabel(str(val))
            v.setProperty("cssClass", "metric-value")
            v.setAlignment(Qt.AlignCenter)
            t = QLabel(label)
            t.setProperty("cssClass", "metric-label")
            t.setAlignment(Qt.AlignCenter)
            lay.addWidget(v)
            lay.addWidget(t)
            self.metric_row.addWidget(card, 1)

        # 无码偏好（按 100% 归一）
        self.unc_chart.clear()
        self.unc_chart.add("无码", p["unc_ratio"] * 100, 100,
                           f"无码 {p['n_unc']} / {p['n_pos']} 部")

        def fill_chart(chart, rows):
            chart.clear()
            maxw = max((r["weight"] for r in rows), default=0)
            for r in rows[:14]:
                chart.add(r["name"], r["weight"], maxw,
                          f"强度{r['weight']:.0f} · 收{r['fav']} 看{r['played']}")

        fill_chart(self.actor_chart, p["top_actors"])
        fill_chart(self.tag_chart, p["top_tags"])
        fill_chart(self.studio_chart, p["top_studios"])
        fill_chart(self.genre_chart, p["top_genres"])

        def fill_table(table, header_n, rows):
            # 必须先关排序再填充：排序开启时逐行 setItem 会因首列排序而移行，导致单元格错位/空白
            table.setSortingEnabled(False)
            table.setRowCount(0)
            for row in rows:
                r = table.rowCount()
                table.insertRow(r)
                for c in range(header_n):
                    v = str(row[c]) if c < len(row) else ""
                    table.setItem(r, c, QTableWidgetItem(v if v else "—"))  # 缺失显示占位，不空白
            table.setSortingEnabled(True)
        fill_table(self.tab_tags, 4, [[a["name"], f"{a['weight']:.0f}", a["fav"], a["played"]]
                                      for a in p["top_tags"]]
                                     or [["（库中缺标签数据，已用类型/演员参与判断）", "", "", ""]])
        fill_table(self.tab_years, 2, [[y, n] for y, n in p["year_pref"]])
        fill_table(self.tab_neg, 2, p["neg_features"] or [["（暂无明显内容类不喜欢特征）", ""]])
        self._fill_rec_cards()

    # ── 为你推荐 20 卡流 ──
    def _render_rec_cards(self, rows: list) -> None:
        while self.recs_lay.count():
            it = self.recs_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not rows:
            lbl = QLabel("暂无可推荐的未看作品（先生成喜好画像）。")
            lbl.setProperty("cssClass", "text-muted")
            self.recs_lay.addWidget(lbl)
            return
        for r in rows:
            self.recs_lay.addWidget(self._rec_card(r))
        self.recs_lay.addStretch()

    def _fill_rec_cards(self) -> None:
        self._render_rec_cards((self.profile or {}).get("recs") or [])

    def _shuffle_recs(self) -> None:
        import random
        pool = getattr(self, "_rec_pool", []) or []
        if len(pool) < 2:
            return
        wts = [max(float(r.get("hit") or 0), 0.02) for r in pool]
        chosen, seen = [], set()
        for _ in range(600):
            if len(chosen) >= 20:
                break
            r = random.choices(pool, weights=wts, k=1)[0]
            if r["num"] not in seen:
                seen.add(r["num"])
                chosen.append(dict(r, hit=f"{float(r['hit']):.0%}"))
        self._render_rec_cards(chosen)

    def _rec_card(self, r):
        card = QFrame()
        card.setProperty("cssClass", "panel-card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        top = QHBoxLayout()
        name = QLabel(str(r["name"] or r["num"]))
        name.setStyleSheet("font-size:14px; font-weight:bold;")
        name.setWordWrap(True)
        hit = QLabel(f"命中度 {r['hit']}")
        hit.setStyleSheet(f"font-size:13px; font-weight:bold; color:{theme.color('accent')};")
        top.addWidget(name, 1)
        top.addWidget(hit)
        lay.addLayout(top)
        meta = QLabel(f"番号: {r['num'] or '—'}　|　{r['reason'] or '—'}")
        meta.setWordWrap(True)
        lay.addWidget(meta)
        info = QLabel(
            f"演员: {'、'.join(r['actors']) or '—'}　|　厂商: {'、'.join(r['studios']) or '—'}"
            f"　|　类型: {'、'.join(r.get('genres') or []) or '—'}")
        info.setWordWrap(True)
        lay.addWidget(info)
        path = r.get("path")
        card.setToolTip("双击打开文件所在位置" if path else "无文件路径")
        if path:

            def _open(ev, p=path):
                if ev.button() == Qt.LeftButton and os.path.exists(p):
                    subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
            card.mouseDoubleClickEvent = _open
        return card

    def apply_theme(self) -> None:
        if self.profile:
            self._render_profile()

    # ── 导出 ──
    def _export(self) -> None:
        if not self.profile:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出喜好画像报表", "喜好画像报表.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            Path(path).write_text(profile_mod.profile_to_csv(self.profile),
                                  encoding="utf-8-sig")
            self.log.log_success(f"已导出: {path}")
        except OSError as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def shutdown(self) -> None:
        for w in (self.analyzer, self._ai_worker, getattr(self, "_taste_worker", None)):
            if w and w.isRunning():
                w.wait(3000)
