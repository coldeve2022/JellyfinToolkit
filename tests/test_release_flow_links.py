"""跨模块链路的守则：检查页挑人 → 找无码核查 → 送去替换。

用户报的三个问题都在这条链上：

1. **FC2 番号写法不统一**：Supjav 搜 ``FC2-PPV-1631136`` 返回 0 条，
   只搜 ``1631136`` 才有结果；而 JavDB 用整串才准。所以"搜索词"必须按站点区分，
   "复核词"仍要严格 —— 两者混用就会要么搜不到、要么误命中。
2. **只能全量查**：检查页已经有"喜欢/结论"筛选，但缺一条"把我挑的送去查"的通道。
   现在 `receive_requests()` 只限定检索范围，不复制筛选逻辑。
3. **替换入口放错页**：业务流程是先确认有无码再替换。检查页只负责送去核查，
   替换入口在「找无码版」页，且只传数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases.base import (  # noqa: E402
    FC2_DIGITS_SITES, contains_number, match_token, search_keyword)


# ── 1. FC2 搜索词与复核词 ─────────────────────────────────

@pytest.mark.parametrize("number", [
    "FC2-PPV-1631136", "FC2PPV-1631136", "FC2PPV 1631136", "fc2-1631136",
])
def test_fc2_supjav_uses_digits(number):
    assert search_keyword(number, site="supjav") == "1631136"


def test_fc2_other_sites_keep_full_number():
    assert search_keyword("FC2-PPV-1631136", site="javdb") == "FC2-PPV-1631136"
    assert "supjav" in FC2_DIGITS_SITES
    assert "javdb" not in FC2_DIGITS_SITES


def test_ordinary_number_keeps_its_hyphen():
    """搜索词不能去掉横线：站点搜索框认 IPX-123，不认 IPX123。"""
    assert search_keyword("IPX-123", site="supjav") == "IPX-123"
    assert search_keyword("IPX-123", site="javdb") == "IPX-123"


def test_match_token_stays_strict_for_fc2():
    """复核用数字，但必须带边界：不能命中更长的一串数字。"""
    assert match_token("FC2-PPV-1631136") == "1631136"
    assert contains_number("FC2PPV 1631136 無修正流出", "FC2-PPV-1631136")
    assert contains_number("FC2-PPV-1631136", "FC2-PPV-1631136")
    assert not contains_number("FC2-PPV-16311360", "FC2-PPV-1631136")
    assert not contains_number("16311360", "FC2-PPV-1631136")


def test_match_token_for_ordinary_number_is_unchanged():
    assert contains_number("IPX-123 無修正", "IPX-123")
    assert not contains_number("IPX-1234", "IPX-123")


# ── 2. 检查页送来的范围限定 ───────────────────────────────

@pytest.fixture
def page(qapp, monkeypatch):
    from config import ToolkitConfig
    from ui.pages.censor_replace import CensorReplacePage, NumberRow

    cfg = ToolkitConfig.load()
    cfg.release_auto_lookup = False
    widget = CensorReplacePage(cfg)
    # 不让构造期/接收期的刷新真的去读库
    monkeypatch.setattr(widget, "refresh", lambda: None)
    widget._rows = {
        "IPX123": NumberRow(number="IPX-123", paths=[r"C:\lib\IPX-123.mp4"]),
        "ABC1": NumberRow(number="ABC-1", paths=[r"C:\lib\ABC-1.mp4"]),
    }
    yield widget


def test_receive_requests_limits_scope_without_copying_filters(page, monkeypatch):
    from censor.models import VideoVerdict

    def fake_get_many(paths):
        return {"C:\\lib\\IPX-123.mp4": VideoVerdict(path="C:\\lib\\IPX-123.mp4", number="IPX-123")}

    monkeypatch.setattr(page._censor.store, "get_many", fake_get_many)
    page.receive_requests(["C:\\lib\\IPX-123.mp4"])

    assert page._focus_numbers == {"IPX123"}
    assert page.btn_focus_clear.isHidden() is False, "有范围限定时应显示「显示全部」"
    targets = page.lookup_targets(force=True)
    assert targets == ["IPX-123"], targets


def test_clear_focus_restores_full_scope(page):
    page._focus_numbers = {"IPX123"}
    page._clear_focus()
    assert page._focus_numbers == set()
    assert sorted(page.lookup_targets(force=True)) == ["ABC-1", "IPX-123"]


def test_send_selected_to_versions_only_emits_data(page, monkeypatch):
    """这个按钮只传路径，不调用任何替换逻辑 —— 替换归版本管理模块。"""
    from ui.services import events

    seen: list[list[str]] = []
    handler = lambda paths: seen.append(list(paths))  # noqa: E731
    events.media_upgrade_requested.connect(handler)
    try:
        page._fill_table()          # 表格要先有行，选中才有意义
        page._fill_table()          # 表格要先有行，选中才有意义
        page.table.selectRow(0)
        page._send_selected_to_versions()
    finally:
        events.media_upgrade_requested.disconnect(handler)

    assert seen, "应当发出替换请求"
    assert all(str(p).endswith(".mp4") for p in seen[0])
    assert "交给「视频版本管理」" in page.lbl_summary.text()
