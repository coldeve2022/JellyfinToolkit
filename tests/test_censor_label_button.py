"""「标记『有码』/『无码』」按钮的可用性 —— 用户报"点了没反应"。

真实原因（不是按钮坏了）：
1. 按钮被布局压到 20px 高，文字挤成一团、看着像不能点；
2. ``_label`` 先把反馈写进 ``_stats_label``，紧接着 ``_refresh_stats()``
   又把它整段覆盖 —— 点了之后界面上什么都没变，自然觉得没反应；
3. 之前"没选中行就禁用按钮"，用户只能猜为什么点不动。

这组用例守这三条：按钮始终可点、点击一定留下可见反馈、没有可标记行时说清原因。
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


def _verdict(path: str, state=CensorState.CENSORED, *, manual=False) -> VideoVerdict:
    evidence = [Evidence(EvidenceSource.VISUAL, state, 0.9, "画面命中")]
    if manual:
        evidence.append(Evidence(EvidenceSource.MANUAL, state, 1.0, "人工确认"))
    item = VideoVerdict(path=path)
    item.verdict = fuse(evidence)
    return item


@pytest.fixture()
def cfg():
    from config import ToolkitConfig

    config = ToolkitConfig.load()
    config.release_auto_lookup = False
    return config


@pytest.fixture
def page(qapp, cfg):
    from ui.pages.censorship import CensorshipPage

    widget = CensorshipPage(cfg)
    widget._restored = True          # 不自动恢复历史，保持用例从空列表开始
    widget.show()
    qapp.processEvents()
    yield widget
    widget.shutdown()


def _stub_label(page, monkeypatch, state=CensorState.CENSORED):
    calls = []

    def fake(path, verdict_state, note="", *, base=None):
        calls.append((path, verdict_state))
        return _verdict(path, state, manual=True)

    monkeypatch.setattr(page._service, "label", fake)
    return calls


def test_buttons_are_tall_enough_and_always_enabled(page):
    for button in (page.btn_censored, page.btn_uncensored):
        assert button.isEnabled(), "按钮不该因为没选中就禁用"
        assert button.minimumHeight() >= 28, "20px 高会把文字挤成一团"


def test_click_leaves_visible_feedback(page, monkeypatch):
    calls = _stub_label(page, monkeypatch)
    page._on_item(_verdict(r"C:\lib\A-123.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()

    page.btn_censored.click()

    assert calls, "点击应当真的调用到标记接口"
    text = page.progress_title.text()
    assert "已标记「有码」" in text, text
    # 关键：刷新统计不能把这条反馈冲掉
    assert page._stats_label.text() != text
    assert "已标记" in page.progress_title.text()


def test_marking_moves_row_to_confirmed(page, monkeypatch):
    _stub_label(page, monkeypatch)
    page._on_item(_verdict(r"C:\lib\A-123.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()

    page.btn_censored.click()
    labels = [page.table.item(r, 3).text() for r in range(page.table.rowCount())]
    assert "已确认" in labels, labels


def test_click_without_selection_falls_back_to_first_visible(page, monkeypatch):
    """没选中任何行时，点击不该静默失败 —— 自动接管第一行。"""
    calls = _stub_label(page, monkeypatch)
    page._on_item(_verdict(r"C:\lib\B-456.mp4"))
    page.table.clearSelection()
    page._current = None
    page.table.setCurrentCell(-1, -1)
    page.update()

    page.btn_censored.click()
    assert calls, "应当自动选中第一行并标记"
    assert "已标记「有码」" in page.progress_title.text()


def test_click_with_empty_list_explains_why(page, monkeypatch):
    _stub_label(page, monkeypatch)
    page.table.setRowCount(0)
    page._results.clear()
    page._current = None

    page.btn_censored.click()
    text = page.progress_title.text()
    assert "没有可标记的行" in text, text
    assert "拖入视频" in text or "全部记录" in text


def test_label_failure_is_reported_not_swallowed(page, monkeypatch):
    """标记失败（文件读不到 / 已被替换）必须写到显眼位置。"""
    def boom(path, state, note="", *, base=None):
        raise OSError("文件不存在")

    monkeypatch.setattr(page._service, "label", boom)
    page._on_item(_verdict(r"C:\lib\C-789.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()

    page.btn_censored.click()
    text = page.progress_title.text()
    assert "标记失败" in text and "读不到文件" in text, text
