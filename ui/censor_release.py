"""无码版本检索的界面部件 —— 后台检索线程 + 结果面板。

放在页面之外，是为了让"检索"这件事既能被检测页用，也能被将来的
"批量补番/替换清单"页直接复用，不用复制 UI 代码。

风格沿用 JellyfinToolkit：``cssClass`` 语义类 + 暗色主题变量，
不自造配色；按钮语义与其它页一致（``accent`` 主操作、``danger`` 危险）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from censor.releases import ReleaseReport, ReleaseService
from censor.releases.models import Availability, ReleaseCandidate

__all__ = ["ReleaseLookupWorker", "ReleasePanel"]


class ReleaseLookupWorker(QThread):
    """后台逐个番号检索。**串行**执行：站点有速率限制，并发只会招来封禁。"""

    report_ready = Signal(object)     # ReleaseReport
    progressed = Signal(int, int)     # 已完成, 总数
    logged = Signal(str)
    all_done = Signal(int)

    def __init__(self, service: ReleaseService, numbers: list[str], *,
                 ttl_days: float = 7.0, force: bool = False, parent=None):
        super().__init__(parent)
        self._service = service
        self._numbers = list(dict.fromkeys(n for n in numbers if n))
        self._ttl_days = ttl_days
        self._force = force
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def _should_stop(self) -> bool:
        return self._stopped

    def run(self) -> None:  # noqa: D102 - QThread 约定
        from censor.releases.service import ReleaseOptions

        total = len(self._numbers)
        done = 0
        options = ReleaseOptions(ttl_days=self._ttl_days, force=self._force)
        for number in self._numbers:
            if self._stopped:
                break
            try:
                report = self._service.lookup(
                    number, options=options,
                    should_stop=self._should_stop, log=self.logged.emit,
                )
            except Exception as exc:  # noqa: BLE001 - 单个番号失败不该中断整批
                self.logged.emit(f"检索 {number} 失败：{exc}")
                done += 1
                self.progressed.emit(done, total)
                continue
            done += 1
            self.report_ready.emit(report)
            self.progressed.emit(done, total)
        self.all_done.emit(done)


class ReleasePanel(QWidget):
    """展示某部作品的检索结果：确有/疑似/未见/未查 + 候选条目 + 下载入口。"""

    COLUMNS = ["站点", "番号", "片商", "日期", "种子"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._report: Optional[ReleaseReport] = None
        self._build()

    # ── 界面 ──
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.lbl_summary = QLabel("无码版本：未查")
        self.lbl_summary.setProperty("cssClass", "muted-label")
        self.lbl_summary.setWordWrap(True)
        header.addWidget(self.lbl_summary, 1)
        self.btn_lookup = QPushButton("检索无码版")
        self.btn_lookup.setToolTip("到已启用的站点查这部作品是否有无码流出/破解版")
        header.addWidget(self.btn_lookup)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMaximumHeight(118)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(lambda _i: self.open_page())
        layout.addWidget(self.table)

        row = QHBoxLayout()
        self.btn_open = QPushButton("打开页面")
        self.btn_open.clicked.connect(self.open_page)
        self.btn_magnet = QPushButton("复制磁链")
        self.btn_magnet.setProperty("cssClass", "accent")
        self.btn_magnet.clicked.connect(self.copy_magnet)
        self.btn_magnet.setToolTip("复制该条目最大种子的磁力链接，直接丢给下载器")
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_magnet)
        row.addStretch()
        self._buttons = (self.btn_open, self.btn_magnet)
        layout.addLayout(row)
        self._refresh_buttons()

    # ── 数据 ──
    def show_report(self, report: Optional[ReleaseReport]) -> None:
        self._report = report
        self._fill()
        self._refresh_buttons()

    def clear(self) -> None:
        self.show_report(None)

    def _fill(self) -> None:
        report = self._report
        self.table.setRowCount(0)
        if report is None:
            self.lbl_summary.setText("无码版本：未查")
            return
        note = f"（{report.note}）" if report.note else ""
        self.lbl_summary.setText(
            f"无码版本：{report.availability.label}{note} · {report.as_text()}"
        )
        rows = sorted(
            report.candidates,
            key=lambda c: (not c.uncensored, -c.total_size_mb),
        )
        for candidate in rows:
            self._append(candidate)
        if not rows:
            detail = "；".join(f"{k}: {v}" for k, v in report.sources.items())
            self.lbl_summary.setToolTip(detail)
        else:
            self.lbl_summary.setToolTip("；".join(f"{k}: {v}" for k, v in report.sources.items()))

    def _append(self, candidate: ReleaseCandidate) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        best = candidate.best_magnet
        seeds = (f"{len(candidate.magnets)} 个 · {best.size_text}"
                 if candidate.magnets else "无")
        values = [
            candidate.source,
            candidate.number or "—",
            (candidate.maker or candidate.title or "—")[:40],
            candidate.release_date or "—",
            seeds,
        ]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == 0 and candidate.uncensored:
                item.setToolTip(f"无码依据：{candidate.uncensored_reason or '站点标记'}")
            if col == 2:
                item.setToolTip(candidate.page_url)
            item.setData(Qt.UserRole, id(candidate))
            self.table.setItem(row, col, item)
        self._candidates_index = getattr(self, "_candidates_index", {})
        self._candidates_index[id(candidate)] = candidate

    # ── 操作 ──
    def selected(self) -> Optional[ReleaseCandidate]:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        return getattr(self, "_candidates_index", {}).get(item.data(Qt.UserRole))

    def open_page(self) -> None:
        candidate = self.selected() or self._fallback()
        if candidate and candidate.page_url:
            QDesktopServices.openUrl(QUrl(candidate.page_url))

    def copy_magnet(self) -> None:
        candidate = self.selected() or self._fallback()
        magnet = candidate.best_magnet if candidate else None
        if magnet is not None:
            QGuiApplication.clipboard().setText(magnet.uri)
            self.btn_magnet.setText("已复制")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(1500, lambda: self.btn_magnet.setText("复制磁链"))

    def _fallback(self) -> Optional[ReleaseCandidate]:
        """没选中行时，用报告里最值得下载的那条。"""
        return self._report.best if self._report is not None else None

    def _refresh_buttons(self) -> None:
        candidate = self.selected() or self._fallback()
        has_page = bool(candidate and candidate.page_url)
        has_magnet = bool(candidate and candidate.best_magnet)
        self.btn_open.setEnabled(has_page)
        self.btn_magnet.setEnabled(has_magnet)
        if not has_magnet:
            self.btn_magnet.setText("无磁链")
        elif self.btn_magnet.text() == "无磁链":
            self.btn_magnet.setText("复制磁链")

    def summary_for(self, report: Optional[ReleaseReport]) -> str:
        """给表格单元格用的短文本。"""
        if report is None:
            return "未查"
        if report.availability is Availability.UNKNOWN and report.note:
            return f"未查（{report.note}）"
        return report.as_text()
