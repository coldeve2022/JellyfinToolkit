"""库概览仪表盘 - 可筛选/排序的分布表格 + 占比条形图 + 指标卡。
样式走 cssClass，随主题切换。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QGroupBox, QFrame, QMessageBox, QComboBox,
)

from config import ToolkitConfig
from ui.widgets import (
    LogPanel, TableBarDelegate, make_number_item, DonutChart, VBarChart,
)
from workers.ingest import IngestWorker

BUCKET_LABEL = {
    "jav": "常规番号 JAV",
    "uncensored": "无码 / FC2 / PPV",
    "hentai": "里番 / H动画",
    "trailer": "预告",
    "other": "其它(无番号/未知)",
}

_FILTERS = ["全部", "分类桶", "年份", "Top 厂商", "Top 演员",
            "收藏偏好演员", "待预测池", "重复"]


class InsightPage(QWidget):
    """库概览页面。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: IngestWorker | None = None
        self._summary: dict | None = None
        self._all_rows: list = []          # (维度, 名称, 数量, max)
        self._setup_ui()

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        title = QLabel("库概览")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = QLabel(
            "只读读取媒体库（Jellyfin / Emby 均可），统计全库作品、行为信号与分布。\n"
            "数据来源在设置页选择：库文件直读，或走服务器 API。\n"
            "如需排除某些目录（例如动漫库），可在设置页的「清理排除目录关键词」里填写。"
            "本页不会修改任何数据。"
        )
        hint.setProperty("cssClass", "subtitle")
        layout.addWidget(hint)

        act = QGroupBox("数据采集")
        ag = QVBoxLayout(act)
        row = QHBoxLayout()
        self.db_label = QLabel(self._source_text())
        self.db_label.setProperty("cssClass", "path-display")
        row.addWidget(self.db_label, 1)
        self.btn_scan = QPushButton("🔍 扫描库")
        self.btn_scan.setProperty("cssClass", "accent")
        self.btn_scan.clicked.connect(self._start_scan)
        row.addWidget(self.btn_scan)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)
        row.addWidget(self.btn_stop)
        ag.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        ag.addWidget(self.progress)
        layout.addWidget(act)

        self.metric_row = QHBoxLayout()
        layout.addLayout(self.metric_row)

        chart_group = QGroupBox("分类统计")
        cg = QHBoxLayout(chart_group)
        cg.setContentsMargins(16, 14, 16, 14)
        cg.setSpacing(20)
        # 左：分类桶环形图
        donut_box = QVBoxLayout()
        donut_t = QLabel("分类桶占比")
        donut_t.setProperty("cssClass", "text-muted")
        donut_box.addWidget(donut_t)
        self.donut = DonutChart(center_text="待预测池\n0")
        self.donut.setMinimumHeight(190)
        donut_box.addWidget(self.donut, 1)
        cg.addLayout(donut_box, 1)
        # 右：年份趋势柱状图
        bar_box = QVBoxLayout()
        bar_t = QLabel("年份发行分布")
        bar_t.setProperty("cssClass", "text-muted")
        bar_box.addWidget(bar_t)
        self.year_bar = VBarChart(max_bars=14)
        self.year_bar.setMinimumHeight(190)
        bar_box.addWidget(self.year_bar, 1)
        cg.addLayout(bar_box, 2)
        layout.addWidget(chart_group)

        dist_group = QGroupBox("全库分布")
        dg = QVBoxLayout(dist_group)
        filt_row = QHBoxLayout()
        filt_row.addWidget(QLabel("筛选维度:"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(_FILTERS)
        self.filter_combo.currentTextChanged.connect(self._apply_filter)
        filt_row.addWidget(self.filter_combo)
        filt_row.addStretch()
        dg.addLayout(filt_row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["维度", "名称", "数量", "占比"])
        # 全 Interactive + 固定初始宽：拖任意列只改该列（去掉 Stretch 联动）
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Interactive)
        h.setDefaultSectionSize(120)
        h.setMinimumSectionSize(70)
        h.setStretchLastSection(False)
        self.table.setColumnWidth(1, 360)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.setItemDelegateForColumn(3, TableBarDelegate(self.table))
        dg.addWidget(self.table)
        layout.addWidget(dist_group)

        log_group = QGroupBox("扫描日志")
        lg = QVBoxLayout(log_group)
        self.log = LogPanel()
        self.log.setMaximumHeight(140)
        lg.addWidget(self.log)
        layout.addWidget(log_group)

        layout.addStretch()
        scroll.setWidget(content)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        self._render_empty()

    # ── 扫描 ──
    def _source_text(self) -> str:
        """当前生效的数据来源（库文件 / 服务器 API）；没配好时说明原因而不是留空。"""
        from utils import library_source
        return "数据来源：" + library_source.describe(self.cfg)

    def _start_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self.db_label.setText(self._source_text())
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)
        self.log.clear()
        self._worker = IngestWorker(self.cfg,
                                    exclude_keywords=self.cfg.exclude_path_keywords)
        self._worker.log.connect(self.log.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _stop_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log.log_warning("正在停止...")

    def _on_done(self, result: dict) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(100 if "summary" in result else 0)
        if "error" in result:
            self.log.log_error(f"❌ {result['error']}")
            QMessageBox.warning(self, "错误", str(result["error"]))
            return
        self._summary = result["summary"]
        self._render_summary(result["summary"])
        self.log.log_success(
            f"✅ 统计完成：{result['summary']['total']} 部 | "
            f"收藏 {result['summary']['favorite']} | 看过 {result['summary']['played']}")

    # ── 指标卡 ──
    def _clear_metrics(self) -> None:
        while self.metric_row.count():
            it = self.metric_row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _metric_card(self, label: str, value: str) -> None:
        card = QFrame()
        card.setProperty("cssClass", "metric-card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        v = QLabel(value)
        v.setProperty("cssClass", "metric-value")
        v.setAlignment(Qt.AlignCenter)
        t = QLabel(label)
        t.setProperty("cssClass", "metric-label")
        t.setAlignment(Qt.AlignCenter)
        lay.addWidget(v)
        lay.addWidget(t)
        self.metric_row.addWidget(card, 1)

    def _render_empty(self) -> None:
        self._clear_metrics()
        self.filter_combo.setCurrentIndex(0)
        self._all_rows = []
        self.table.setRowCount(0)
        self.donut.set_data([])
        self.year_bar.set_data([])
        hint = QLabel("尚未扫描。点「🔍 扫描库」查看全库分布。")
        hint.setProperty("cssClass", "text-muted")
        self.metric_row.addWidget(hint)

    def _render_summary(self, s: dict) -> None:
        self._clear_metrics()
        for label, val in [("总数", s["total"]), ("收藏", s["favorite"]),
                           ("看过", s["played"]), ("播放过", s["ever_play"]),
                           ("未看(待预测)", s["unwatched"]), ("重复组", s["dup_groups"])]:
            self._metric_card(label, str(val))

        # ── 图表数据 ──
        bucket_seg = [
            ("JAV", s["by_bucket"].get("jav", 0), "accent"),
            ("无码", s["by_bucket"].get("uncensored", 0), "success"),
            ("里番", s["by_bucket"].get("hentai", 0), "warning"),
            ("预告", s["by_bucket"].get("trailer", 0), "danger"),
            ("其它", s["by_bucket"].get("other", 0), "accent_hover"),
        ]
        self.donut.set_data(bucket_seg)
        self.donut.set_center(f"待预测池\n{s['unwatched']}")
        yr = [(str(y), n, "accent") for y, n in s["years"] if y >= 2000]
        self.year_bar.set_data(yr)

        rows: list = []
        rows.append(("待预测池", "未看作品(从未播放)", s["unwatched"]))
        rows.append(("待预测池", "未看且较老(≤2022)", s["unwatched_old"]))
        for key, label in [("jav", BUCKET_LABEL["jav"]), ("uncensored", BUCKET_LABEL["uncensored"]),
                           ("hentai", BUCKET_LABEL["hentai"]), ("trailer", BUCKET_LABEL["trailer"]),
                           ("other", BUCKET_LABEL["other"])]:
            rows.append(("分类桶", label, s["by_bucket"].get(key, 0)))
        rows.append(("重复", "重复组(同番号≥2)", s["dup_groups"]))
        rows.append(("重复", "涉及路径数", s["dup_paths"]))
        rows.append(("重复", "其中 CD 分卷组(保留)", s["dup_cd_groups"]))
        rows.append(("重复", "疑似真重复组(可删差)", s["dup_true"]))
        for name, n in s["top_studios"]:
            rows.append(("Top 厂商", name, n))
        for name, n in s["top_actors"]:
            rows.append(("Top 演员", name, n))
        for name, n in s["top_fav_actors"]:
            rows.append(("收藏偏好演员", name, n))
        for y, n in s["years"]:
            if y < 2000:
                continue
            rows.append(("年份", str(y), n))
        self._all_rows = rows
        self._apply_filter()

    def _apply_filter(self, _=None) -> None:
        f = self.filter_combo.currentText()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        maxn = max((n for _c, _n, n in self._all_rows if isinstance(n, (int, float))), default=1)
        for cat, name, n in self._all_rows:
            if f != "全部" and cat != f:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            ci = QTableWidgetItem(cat)
            t1 = QTableWidgetItem(str(name))
            ni = make_number_item(str(n), n)
            pct = int(n / maxn * 100) if maxn else 0
            pi = make_number_item(f"{pct}%", pct)
            t1.setData(Qt.UserRole, name)
            for col, it in ((0, ci), (1, t1), (2, ni), (3, pi)):
                self.table.setItem(r, col, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2, Qt.DescendingOrder)

    def shutdown(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
