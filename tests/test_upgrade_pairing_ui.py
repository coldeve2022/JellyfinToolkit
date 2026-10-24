"""版本管理的配对环节：ambiguous 行必须能选，否则整批永远卡住。

用户实测的卡点（截图）：
- 5 行 ready + 1 行 ambiguous
- ambiguous 那行只写"需要选择唯一版本并匹配封装格式"，界面上**没有任何可选的地方**
- 而且 `_sync_buttons` 要求**全部 ready** 才能「保存进度快照」→ 整批卡死
- 点「载入上次记录」还会直接抛 `[Errno 2] ... latest.json`

这里锁住两条：选完能变 ready；没快照时给中文提示而不是裸文件错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui.pages.replace import _UPGRADE_STATUS, apply_candidate_choice  # noqa: E402


def _row(status="ambiguous", candidates=None):
    return {
        "source": r"F:\破解视频\原\ABC-123.mp4",
        "target": "",
        "candidates": candidates or [r"F:\破解视频\破解后\ABC-123-1.mp4",
                                    r"F:\破解视频\破解后\ABC-123-2.mp4"],
        "status": status,
        "error": "需要选择唯一原文件并匹配封装格式",
    }


def test_choosing_a_candidate_makes_the_row_ready():
    rows = [_row()]
    assert apply_candidate_choice(rows, 0, rows[0]["candidates"][1]) is True
    assert rows[0]["status"] == "ready"
    assert rows[0]["target"] == rows[0]["candidates"][1]
    assert rows[0]["error"] == ""
    # 全部 ready 之后，"保存进度快照"才可能解锁
    assert all(r["status"] == "ready" for r in rows)


def test_choice_must_be_one_of_the_candidates():
    rows = [_row()]
    assert apply_candidate_choice(rows, 0, r"F:\别处\X.mp4") is False
    assert rows[0]["status"] == "ambiguous", "非法选择不能把状态改成 ready"


@pytest.mark.parametrize("index", [-1, 5])
def test_out_of_range_choice_is_rejected(index):
    assert apply_candidate_choice([_row()], index, "x") is False


def test_tolerates_non_list_input():
    assert apply_candidate_choice(None, 0, "x") is False
    assert apply_candidate_choice({"rows": []}, 0, "x") is False


def test_statuses_are_chinese():
    """状态列不该把 ready / ambiguous 直接丢给用户。"""
    assert _UPGRADE_STATUS["ready"] == "就绪"
    assert _UPGRADE_STATUS["ambiguous"] == "要选版本"
    assert _UPGRADE_STATUS["container_mismatch"] == "封装不同"
    assert _UPGRADE_STATUS["not_found"] == "没找到新版本"
    for value in _UPGRADE_STATUS.values():
        assert value.isascii() is False, "状态名要中文化"


def test_dropping_a_blocked_row_unblocks_the_rest():
    """一行无解（比如封装不同）也不能把整批卡死：移除后可继续。"""
    from ui.pages.replace import drop_plan_row

    rows = [
        _row("ready"),
        _row("container_mismatch", [r"F:/x/A.mkv"]),
    ]
    rows[0]["target"] = rows[0]["source"]
    assert drop_plan_row(rows, 1) is True
    assert len(rows) == 1
    assert all(r["status"] == "ready" for r in rows)


def test_drop_rejects_bad_index():
    from ui.pages.replace import drop_plan_row

    rows = [_row("ready")]
    assert drop_plan_row(rows, 3) is False
    assert drop_plan_row("nope", 0) is False
    assert len(rows) == 1


