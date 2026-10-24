"""版本判定：Supjav 标注就是依据，不要什么都推给人工复核。

用户报的核心问题："大量视频要我手动确认，而这个确认压根不难"。

根因是 `classify_tag` 把 ``mosaic`` 一律算"有码"，于是
``[Reducing Mosaic]YSN-638`` 被判成有码，详情页 0.9 的依据被丢掉，
只剩标题的 0.6 → 结论"疑似（需核对）"。

这里锁住三档语义：
- 无码/流出/破解 → 确有
- 减码/薄码（Reducing Mosaic）→ **也算有得换**（0.80，达"确有"阈值）
- 中文字幕/4K/60FPS 这类前缀 → 与版本无关，不能拿来判
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases.service import decide_availability  # noqa: E402
from censor.releases.models import Availability, ReleaseCandidate  # noqa: E402
from censor.releases.supjav import SupjavSource  # noqa: E402
from censor.tag_signals import version_level  # noqa: E402


@pytest.mark.parametrize("text,level", [
    ("[Reducing Mosaic]YSN-638 Breast Milk", "soft"),
    ("Uncensored JAV", "strong"),
    ("無修正流出 YSN-638", "strong"),
    ("Uncensored Leak", "strong"),
    ("Decensored", "strong"),
    ("Censored", "censored"),
    ("[Chn Sub] YSN-638", "neutral"),
    ("[4K] YSN-638", "neutral"),
    ("[60FPS] ABC-123", "neutral"),
    ("[English Sub] ABC-123", "neutral"),
    ("YSN-638 Breast Milk", "neutral"),
])
def test_version_level(text, level):
    assert version_level(text)[0] == level


def test_reducing_mosaic_is_usable_not_censored():
    """减码版必须算"有得换"，否则用户就要一条条人工点。"""
    usable, confidence, reason = SupjavSource._judge("[Reducing Mosaic]YSN-638 Breast Milk")
    assert usable is True
    assert confidence >= 0.8, "低于 HARD_EVIDENCE 就会退回'疑似'"
    assert "减码" in reason


def test_uncensored_marker_is_hard_evidence():
    usable, confidence, _ = SupjavSource._judge("Uncensored Leak YSN-638")
    assert usable and confidence >= 0.85


def test_censored_marker_is_not_usable():
    assert SupjavSource._judge("Censored YSN-638")[0] is False


@pytest.mark.parametrize("text", ["[Chn Sub] YSN-638", "[4K] YSN-638", "[60FPS] YSN-638"])
def test_neutral_prefixes_never_mean_uncensored(text):
    """中文字幕/4K/帧率是常见前缀，不能当版本标记。"""
    assert SupjavSource._judge(text)[0] is False


def test_reducing_mosaic_reaches_confirmed():
    """端到端：减码版候选应把结论顶到"确有"，不再停在"疑似"。"""
    candidate = ReleaseCandidate(
        source="supjav", number="YSN-638",
        title="[Reducing Mosaic]YSN-638 Breast Milk",
        page_url="https://supjav.com/333078.html",
        uncensored=True, uncensored_confidence=0.85,
        uncensored_reason="站内分类：Reducing Mosaic（减码版）")
    assert decide_availability([candidate]) is Availability.CONFIRMED
