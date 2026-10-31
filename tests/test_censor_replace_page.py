"""「找无码版」页的用例 —— 阶段二的独立工作台。

守的契约：
1. 只认**人工确认有码**的记录（数据源 `confirmed_censored`），算法猜的不进；
2. 按番号聚合：同一部片的多个副本只占一行；
3. 检索分"未查"与"全部重查"两种，前者跳过已有结果的番号；
4. 结果回填到行上，候选面板与文件清单跟着联动。
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
    Availability, MagnetLink, ReleaseCandidate, ReleaseReport, normalize_number,
)


class _Signal:
    """假 worker 的信号替身。"""

    def connect(self, *_args, **_kwargs):
        return None


class FakeWorker:
    report_ready = progressed = logged = all_done = finished = _Signal()

    def __init__(self, service, numbers, **kwargs):
        FakeWorker.calls.append(list(numbers))

    calls: list = []

    def start(self):
        return None

    def isRunning(self):  # noqa: N802 - Qt 约定
        return False


def _confirmed(path: str, number: str, state=CensorState.CENSORED) -> VideoVerdict:
    item = VideoVerdict(path=path, number=number)
    item.verdict = fuse([
        Evidence(EvidenceSource.VISUAL, state, 0.9, "画面命中"),
        Evidence(EvidenceSource.MANUAL, state, 1.0, "人工确认"),
    ])
    return item


def _report(number: str, availability: Availability) -> ReleaseReport:
    candidate = ReleaseCandidate(
        source="supjav", number=number, title=f"[Uncensored Leak] {number}",
        page_url="https://supjav.com/1.html",
        uncensored=availability is not Availability.NONE,
        uncensored_reason="站点标题含无码/破解标记",
        uncensored_confidence=0.85 if availability is not Availability.NONE else 0.0,
        magnets=(MagnetLink(uri="magnet:?xt=urn:btih:" + "c" * 40, name=f'{number} Uncensored', size_mb=4096),)
        if availability is not Availability.NONE else (),
    )
    return ReleaseReport(number=number, availability=availability,
                         candidates=(candidate,), sources={"supjav": "命中 1 条"})


@pytest.fixture()
def cfg():
    from config import ToolkitConfig

    return ToolkitConfig.load()


@pytest.fixture
def page(qapp, qtbot, cfg, monkeypatch):
    from ui.pages.censor_replace import CensorReplacePage

    records = [
        _confirmed(r"C:\lib\A-123.mp4", "A-123"),
        _confirmed(r"C:\lib\A-123-1080p.mp4", "A-123"),
        _confirmed(r"C:\lib\B-456.mp4", "B-456"),
        _confirmed(r"C:\lib\no_number.mp4", ""),
    ]
    widget = CensorReplacePage(cfg)
    monkeypatch.setattr(
        widget._censor, "store",
        type("S", (), {"confirmed_censored": staticmethod(lambda: records),
                       "db_path": widget._censor.store.db_path})(),
        raising=False)
    monkeypatch.setattr(
        widget._release, "cached",
        lambda number: _report("A123", Availability.CONFIRMED)
        if normalize_number(number) == "A123" else None)
    monkeypatch.setattr(widget._release, "fresh", lambda number: _report("A123", Availability.CONFIRMED)
                        if normalize_number(number) == "A123" else None)
    monkeypatch.setattr(widget._release, 'fresh_many', lambda numbers: {'A123': _report('A123', Availability.CONFIRMED)})
    widget._release.store.put(_report("A123", Availability.CONFIRMED))
    widget.refresh()
    qtbot.waitUntil(lambda: widget.table.rowCount() == 3, timeout=15000)
    yield widget
    qtbot.waitUntil(lambda: widget._refresh_worker is None, timeout=15000)
    widget.shutdown()
    from ui.services import TaskManager
    qtbot.waitUntil(lambda: not TaskManager.instance().running(widget._refresh_owner), timeout=5000)
    qtbot.waitUntil(lambda: not TaskManager.instance().running(widget), timeout=5000)


def _rows(page):
    return {page.table.item(r, 0).text(): {
        "files": page.table.item(r, 1).text(),
        "result": page.table.item(r, 2).text(),
        "seeds": page.table.item(r, 4).text(),
    } for r in range(page.table.rowCount())}


def test_columns_are_number_oriented(page):
    from ui.pages.censor_replace import _COLUMNS

    assert _COLUMNS == ["番号", "片数", "检索结果", "候选", "最大种子", "最近检索", "喜欢", "播放状态"]


def test_groups_paths_by_number(page):
    rows = _rows(page)
    assert "A-123" in rows and rows["A-123"]["files"] == "2", rows
    assert rows["B-456"]["files"] == "1"


def test_unqueried_rows_say_never_checked(page):
    rows = _rows(page)
    assert rows["A-123"]["result"].startswith("确有")
    assert rows["B-456"]["result"] == "未查"


def test_uncached_number_is_reported_in_summary(page):
    text = page.lbl_summary.text()
    assert "3 部" in text, text
    assert "待查 1" in text, text
    # "没有番号的片子无法检索"从主行移到了悬停提示（主行只留 5 个数）
    assert "没有番号" in page.lbl_summary.toolTip(), "无番号的片子要说明无法检索"


def test_selecting_row_shows_candidates_and_files(page):
    for r in range(page.table.rowCount()):
        if page.table.item(r, 0).text() == "A-123":
            page.table.selectRow(r)
            break
    page._on_row_selected()
    assert "确有" in page.panel.lbl_summary.text()
    assert page.files.rowCount() == 2


def test_lookup_new_only_queries_unchecked(page, monkeypatch, qtbot):
    FakeWorker.calls = []
    monkeypatch.setattr("ui.pages.censor_replace.ReleaseLookupWorker", FakeWorker)
    page._lookup(force=False)
    qtbot.waitUntil(lambda: bool(FakeWorker.calls), timeout=15000)
    assert FakeWorker.calls == [["B-456"]], FakeWorker.calls


def test_lookup_all_requeries_every_number(page, monkeypatch):
    FakeWorker.calls = []
    monkeypatch.setattr("ui.pages.censor_replace.ReleaseLookupWorker", FakeWorker)
    page._lookup(force=True)
    assert FakeWorker.calls and set(FakeWorker.calls[0]) == {"A-123", "B-456"}, FakeWorker.calls


def test_report_updates_row_and_panel(page):
    for r in range(page.table.rowCount()):
        if page.table.item(r, 0).text() == "B-456":
            page.table.selectRow(r)
            break
    page._on_row_selected()

    page._on_report(_report("B-456", Availability.CONFIRMED))
    rows = _rows(page)
    assert rows["B-456"]["result"].startswith("确有")
    assert rows["B-456"]["seeds"] != "—"
    assert "确有" in page.panel.lbl_summary.text()


def test_empty_list_is_handled(page, monkeypatch, qtbot):
    monkeypatch.setattr(page._censor, "store",
                        type("S", (), {"confirmed_censored": staticmethod(lambda: []),
                                       "db_path": page._censor.store.db_path})())
    page.refresh()
    qtbot.waitUntil(lambda: page.table.rowCount() == 0, timeout=15000)
    assert page.table.rowCount() == 0
    assert "0 部" in page.lbl_summary.text()
