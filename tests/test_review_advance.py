"""「下一条」的定位：必须从当前位置往后走，不能总从头开始。

这个功能坏过两次：
1. 跳转逻辑又按视图筛了一遍（表格已筛过）→ 视图停在「暂不处理」时永远找不到下一条；
2. 只用**可见行**定位当前位置 —— 标记完当前条目就不再是"待复核"，会被筛掉、
   从可见行里消失，"我在哪"查不到 → `index()` 落空 → 从头开始 →
   表现就是"不管从哪条开始，下一条永远是第一条"。

第 2 条正是用户后来报的现象。所以这里把定位逻辑抽成纯函数并锁死。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.review_route import next_review_target  # noqa: E402

PATHS = [f"X:/{name}.mp4" for name in "ABCDE"]


def _always(_path):
    return True


def test_next_from_middle():
    got = next_review_target(PATHS, set(PATHS), "X:/B.mp4", _always)
    assert got == "X:/C.mp4"


def test_current_row_just_got_hidden_still_continues_forward():
    """标记后当前行被筛掉（不在 visible 里）—— 仍要从它的位置往后走。"""
    visible = {"X:/A.mp4", "X:/C.mp4", "X:/D.mp4", "X:/E.mp4"}   # B 刚被隐藏
    got = next_review_target(PATHS, visible, "X:/B.mp4", _always)
    assert got == "X:/C.mp4", "不能因为当前行不可见就从头开始"


def test_last_row_wraps_to_first():
    got = next_review_target(PATHS, set(PATHS), "X:/E.mp4", _always)
    assert got == "X:/A.mp4"


def test_skips_hidden_rows_while_walking_forward():
    visible = {"X:/A.mp4", "X:/B.mp4", "X:/E.mp4"}   # C、D 被筛掉
    got = next_review_target(PATHS, visible, "X:/B.mp4", _always)
    assert got == "X:/E.mp4"


def test_skips_items_that_are_no_longer_candidates():
    got = next_review_target(PATHS, set(PATHS), "X:/A.mp4",
                             lambda p: p != "X:/B.mp4")
    assert got == "X:/C.mp4"


def test_no_candidate_returns_empty():
    assert next_review_target(PATHS, set(PATHS), "X:/A.mp4", lambda p: False) == ""


def test_unknown_position_falls_back_to_scanning_all():
    """当前位置不在表格里（比如刚被移除）—— 从全表找第一个可用的。"""
    got = next_review_target(PATHS, set(PATHS), "X:/gone.mp4", _always)
    assert got == "X:/A.mp4"


def test_only_current_row_left_returns_empty():
    visible = {"X:/C.mp4"}
    assert next_review_target(PATHS, visible, "X:/C.mp4", _always) == ""
