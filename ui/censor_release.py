"""无码版本检索的界面部件 —— 后台检索线程 + 结果面板。

放在页面之外，是为了让"检索"这件事既能被检测页用，也能被将来的
"批量补番/替换清单"页直接复用，不用复制 UI 代码。

风格沿用 JellyfinToolkit：``cssClass`` 语义类 + 暗色主题变量，
不自造配色；按钮语义与其它页一致（``accent`` 主操作、``danger`` 危险）。
"""

from __future__ import annotations

from typing import Optional
from copy import copy, deepcopy

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
    task_status = Signal(object)

    def __init__(self, service: ReleaseService, numbers: list[str], *,
                 ttl_days: float = 7.0, force: bool = False, parent=None):
        super().__init__(parent)
        self._service = copy(service)
        self._service.cfg = deepcopy(service.cfg)
        if service._transport is not None and service._factory is None:
            self._service._transport = None
        self._service._source_sessions = {}
        self._numbers = list(dict.fromkeys(n for n in numbers if n))
        self._ttl_days = ttl_days
        self._force = force
        self._stopped = False
        self.stats = dict(ok=0, fail=0, cancelled=False)

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
                self.stats["fail"] += 1
                self.progressed.emit(done, total)
                continue
            done += 1
            from censor.models import ProcessingStatus
            if report.status is ProcessingStatus.CANCELLED:
                self.stats["cancelled"] = True
            elif report.status is not ProcessingStatus.COMPLETED:
                self.stats["fail"] += 1
            else:
                self.stats["ok"] += 1
            self.report_ready.emit(report)
            self.progressed.emit(done, total)
            self.task_status.emit(dict(progress=int(done * 100 / max(1, total)), phase="检索作品",
                detail=f"{done}/{total} · 完成 {self.stats['ok']} · 未完成 {self.stats['fail']} · {number}"))
        self.stats["cancelled"] = self.stats["cancelled"] or self._stopped
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
        self.btn_lookup = QPushButton("查询这条")
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
        self.btn_open = QPushButton("打开链接")
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
        self._candidates_index = {}
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
                 if best else "无可确认版本的磁链")
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
        magnet = candidate.best_magnet if candidate and candidate.uncensored else None
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
        has_magnet = bool(candidate and candidate.uncensored and candidate.best_magnet)
        self.btn_open.setEnabled(has_page)
        self.btn_magnet.setEnabled(has_magnet)
        if not has_magnet:
            self.btn_magnet.setText("复制磁链")
        elif self.btn_magnet.text() == "复制磁链":
            self.btn_magnet.setText("复制磁链")

    def summary_for(self, report: Optional[ReleaseReport]) -> str:
        """给表格单元格用的短文本。"""
        if report is None:
            return "未查"
        if report.availability is Availability.UNKNOWN and report.note:
            return f"未查（{report.note}）"
        return report.as_text()


class ConnectionCheck(QThread):
    """设置页的「测试连接」—— 一次把四件事都试一遍，逐条告诉你卡在哪。

    为什么不只在失败时弹个错：这个功能涉及**三层**（本地代理 → 目标站点 →
    Cloudflare 旁路），任何一层断了表现都是"查不到"，只给一句失败等于没说。
    所以这里固定输出四行，每行都带"下一步该干什么"。
    """

    line = Signal(str)
    passed = Signal(int, int)      # 通过条数, 总条数

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = {
            "release_proxy": getattr(cfg, "release_proxy", "") or "",
            "release_cf_bypass_url": getattr(cfg, "release_cf_bypass_url", "") or "",
            "release_sources": ["javdb", "supjav"],
            "release_supjav_engines": list(
                getattr(cfg, "release_supjav_engines", ["bing", "duckduckgo"]) or []),
            "release_supjav_mode": getattr(cfg, 'release_supjav_mode', 'auto'),
            # 测试要快：不重试、短超时、少请求
            "release_timeout": 15.0,
            "release_retries": 0,
            "release_min_interval": 0.0,
            "release_max_requests": 8,
            "release_detail_limit": 1,
        }
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:  # noqa: D102 - QThread 约定
        from censor.releases.javdb import JavDbSource
        from censor.releases.models import ReleaseQuery
        from censor.releases.supjav import SupjavSource
        from censor.releases.transport import (
            TransportBlocked, TransportError, build_transport, offline, probe_bypass,
        )

        checks: list[bool] = []

        def emit(ok: bool, text: str, hint: str = "") -> None:
            checks.append(ok)
            self.line.emit(f"{'✓' if ok else '✗'} {text}")
            if hint:
                self.line.emit(f"    → {hint}")

        if offline():
            emit(False, "离线模式已开启（JELLYFIN_TOOLKIT_OFFLINE=1）",
                 "去掉这个环境变量后重启，测试才有意义")
            self.passed.emit(0, 1)
            return

        endpoint = self._cfg["release_cf_bypass_url"].strip()
        bypass_ok = False
        if endpoint:
            bypass_ok, why = probe_bypass(endpoint)
            emit(bypass_ok, f"Cloudflare 旁路服务：{why}",
                 "" if bypass_ok else "启动 FlareSolverr（默认端口 8191）后重试")

        transport = build_transport(self._cfg)

        # 1) 到站点的线路
        try:
            transport.get("https://javdb.com/")
            emit(True, f"网络线路：{transport.description}")
            reachable = True
        except TransportBlocked as exc:
            reachable = False
            emit(False, f"网络线路：被拦截（{exc}）", "检查代理是否开启了规则分流")
        except TransportError as exc:
            reachable = False
            emit(False, f"网络线路：{exc}",
                 "在「检索代理」里填一个能访问该站点的地址（如 http://127.0.0.1:7897）")

        # 2) JavDB 实际检索
        if reachable:
            try:
                found = JavDbSource(transport, self._cfg).search(
                    ReleaseQuery(number="PIYO-170"), limit=1)
                emit(bool(found), f"JavDB 检索：命中 {len(found)} 条（PIYO-170）",
                     "" if found else "页面结构可能变了，或该番号在站点上不存在")
            except Exception as exc:  # noqa: BLE001 - 测试就该把原因显示出来
                emit(False, f"JavDB 检索失败：{exc}", "站点改版或网络不稳定")

        # Supjav has an independent route/session and must be tested even if JavDB fails.
        if not self._stopped:
            source = SupjavSource(transport, self._cfg)
            try:
                found = source.search(ReleaseQuery(number="NCRA-026"), limit=2)
            except Exception as exc:  # noqa: BLE001 - 测试就把原因显示出来
                emit(False, f"Supjav 检索失败：{exc}",
                     "在检索设置连接 Supjav 浏览器会话并手动完成验证；也可配置 FlareSolverr")
            else:
                detail = "；".join(f"{k}: {v}" for k, v in source.status_detail.items())
                if found:
                    verified = sum('站内' in c.uncensored_reason for c in found)
                    emit(True, f"Supjav：命中 {len(found)} 条，其中站内核验 {verified} 条", detail)
                else:
                    emit(False, "Supjav：没有命中",
                         (detail + "；" if detail else "")
                         + "索引里没有这个番号，不代表站上没有，只代表没被索引到")

        ok_count = sum(1 for value in checks if value)
        self.passed.emit(ok_count, len(checks))
