"""无码版本检索的界面接线用例 —— 不联网，直接喂报告。

守的是这几件事：
1. 检索属于**阶段二**：没确认有码之前「无码版」列显示「—」，不假装查过；
2. 只有人工标记「有码」的片才进「② 找替换」，算法判的"疑似有码"不进去；
3. 报告按番号落到所有同番号的行上（同一部片的多份副本共用一条结果）；
4. 面板能正确显示候选与按钮状态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor import CensorState, VideoVerdict  # noqa: E402
from censor.fusion import fuse  # noqa: E402
from censor.models import Evidence, EvidenceSource  # noqa: E402
from censor.releases.models import (  # noqa: E402
    Availability, MagnetLink, ReleaseCandidate, ReleaseReport,
)


def _verdict(path: str, number: str, state=CensorState.CENSORED, *,
             manual: bool = False) -> VideoVerdict:
    evidence = [Evidence(EvidenceSource.VISUAL, state, 0.9, "画面命中")]
    if manual:
        evidence.append(Evidence(EvidenceSource.MANUAL, state, 1.0, "人工确认"))
    result = VideoVerdict(path=path, number=number)
    result.verdict = fuse(evidence)
    return result


def _report(number: str, availability: Availability, *, magnet=True) -> ReleaseReport:
    candidate = ReleaseCandidate(
        source="javdb", number=number, title=f"{number} 無碼流出",
        page_url="https://javdb.com/v/xxx", maker="日本コンテンツ流通",
        uncensored=availability is not Availability.NONE,
        uncensored_reason="片商为无码厂牌",
        uncensored_confidence=0.9 if availability is not Availability.NONE else 0.0,
        magnets=(MagnetLink(uri="magnet:?xt=urn:btih:" + "b" * 40, size_mb=9000),)
        if magnet else (),
    )
    return ReleaseReport(number=number, availability=availability,
                         candidates=(candidate,), sources={"javdb": "命中 1 条"})


@pytest.fixture()
def cfg():
    from config import ToolkitConfig

    return ToolkitConfig.load()


@pytest.fixture
def page(qapp, cfg):
    from ui.pages.censorship import CensorshipPage

    cfg.release_auto_lookup = False      # 用例自己喂报告，别去联网
    widget = CensorshipPage(cfg)
    yield widget
    widget.shutdown()


def test_table_has_release_column(page):
    from ui.pages.censorship import _COLUMNS

    assert page.table.columnCount() == len(_COLUMNS)
    assert _COLUMNS[-1] == "无码版"


def test_release_cell_is_blank_before_confirmation(page):
    """阶段一还没打标记，无码版列就是「—」—— 检索根本没轮到它。"""
    page._on_item(_verdict(r"C:\tmp\ABC-123.mp4", "ABC-123"))
    page._on_item(_verdict(r"C:\tmp\CONF-1.mp4", "CONF-1", manual=True))
    page._on_item(_verdict(r"C:\tmp\nonumber.mp4", "", manual=True))
    cells = {page.table.item(r, 0).data(0): page.table.item(r, 7).text()
             for r in range(page.table.rowCount())}
    assert cells["ABC-123.mp4"] == "—", "算法判的有码不算确认，不该显示未查"
    assert cells["CONF-1.mp4"] == "未查", "确认有码后才该轮到检索"
    assert cells["nonumber.mp4"] == "无番号"


def test_report_updates_all_rows_with_same_number(page):
    page._on_item(_verdict(r"C:\tmp\a\ABC-123.mp4", "ABC-123", manual=True))
    page._on_item(_verdict(r"C:\tmp\b\ABC-123-1080p.mp4", "ABC-123", manual=True))
    page._on_release_report(_report("ABC-123", Availability.CONFIRMED))
    texts = {page.table.item(r, 7).text() for r in range(page.table.rowCount())}
    assert texts and all("确有" in text for text in texts), texts


def test_replace_view_holds_only_confirmed_censored(page):
    """「② 找替换」只装已确认有码的片 —— 不管检索结果如何。"""
    from censor.review_route import WorkflowView

    page._on_item(_verdict(r"C:\tmp\A-1.mp4", "A-1", manual=True))
    page._on_item(_verdict(r"C:\tmp\B-2.mp4", "B-2", manual=True,
                           state=CensorState.UNCENSORED))
    page._on_item(_verdict(r"C:\tmp\C-3.mp4", "C-3"))
    page._on_release_report(_report("A-1", Availability.NONE))

    page.combo_filter.setCurrentIndex(
        page.combo_filter.findData(WorkflowView.REPLACE.value))
    visible = {page.table.item(r, 0).data(0) for r in range(page.table.rowCount())
               if not page.table.isRowHidden(r)}
    assert visible == {"A-1.mp4"}, "未确认的 C-3 与已确认无码的 B-2 都不该进来"


def test_panel_shows_candidates_and_enables_download(page):
    page._on_item(_verdict(r"C:\tmp\ABC-123.mp4", "ABC-123", manual=True))
    page.table.selectRow(0)
    page._on_row_selected()
    page._on_release_report(_report("ABC-123", Availability.CONFIRMED))

    assert "确有" in page.release_panel.lbl_summary.text()
    assert page.release_panel.table.rowCount() == 1
    assert page.release_panel.btn_open.isEnabled()
    assert page.release_panel.btn_magnet.isEnabled()


def test_panel_without_magnet_disables_copy(page):
    page._on_item(_verdict(r"C:\tmp\ABC-123.mp4", "ABC-123"))
    page.table.selectRow(0)
    page._on_row_selected()
    page._on_release_report(_report("ABC-123", Availability.CONFIRMED, magnet=False))

    assert not page.release_panel.btn_magnet.isEnabled()
    assert page.release_panel.btn_open.isEnabled()


def test_unknown_report_carries_reason_in_cell(page):
    report = ReleaseReport(number="ABC123", availability=Availability.UNKNOWN,
                           note="站点被拦截，未能查证")
    page._on_item(_verdict(r"C:\tmp\ABC-123.mp4", "ABC-123", manual=True))
    page._on_release_report(report)
    cell = page.table.item(0, 7).text()
    assert "未查" in cell and "拦截" in cell, cell


def test_uncensored_video_is_not_auto_queued(page):
    """只查「有码」的片 —— 无码片没有替换意义，别浪费请求。"""
    page._on_item(_verdict(r"C:\tmp\ABC-123.mp4", "ABC-123", CensorState.UNCENSORED))
    # 直接检查排队逻辑（不跑计时器）
    pending_before = list(page._lookup_pending)
    for result in page._results.values():
        page._maybe_queue_lookup(result)
    assert page._lookup_pending == pending_before


def test_connection_test_button_reports_offline(page, monkeypatch):
    """「测试连接」必须能跑通且不联网 —— 离线模式下要说清楚为什么测不了。"""
    from PySide6.QtCore import QEventLoop, QTimer

    monkeypatch.setenv("JELLYFIN_TOOLKIT_OFFLINE", "1")
    loop = QEventLoop()
    page._on_connection_tested_orig = page._on_connection_tested
    page._on_connection_tested = lambda ok, total: (
        page._on_connection_tested_orig(ok, total), QTimer.singleShot(50, loop.quit))

    page._test_release_connection()
    QTimer.singleShot(8000, loop.quit)
    loop.exec()

    text = page.txt_release_test.toPlainText()
    assert "离线模式" in text
    assert "测试结束" in text
    assert page.btn_test_release.isEnabled()
