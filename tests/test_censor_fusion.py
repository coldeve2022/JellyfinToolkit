"""融合规则的用例 —— 每条断言对应"检码宪法"里的一条。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.fusion import FusionPolicy, explain, fuse  # noqa: E402
from censor.models import CensorState, Evidence, EvidenceSource  # noqa: E402


def ev(source, state, conf, detail=""):
    return Evidence(source, state, conf, detail)


def test_manual_overrides_everything():
    verdict = fuse([
        ev(EvidenceSource.VISUAL, CensorState.CENSORED, 0.9, "画面命中"),
        ev(EvidenceSource.MANUAL, CensorState.UNCENSORED, 1.0, "我点过了"),
    ])
    assert verdict.state is CensorState.UNCENSORED
    assert verdict.confidence == 1.0
    assert not verdict.needs_review


def test_visual_beats_tag():
    """标签说无码、画面说有码 → 以画面为准，并提示冲突。"""
    verdict = fuse([
        ev(EvidenceSource.SERVER_TAG, CensorState.UNCENSORED, 0.45, "标签:无码"),
        ev(EvidenceSource.VISUAL, CensorState.CENSORED, 0.85, "画面命中"),
    ])
    assert verdict.state is CensorState.CENSORED
    assert verdict.needs_review
    assert "冲突" in verdict.reason


def test_series_rules_decide_uncensored():
    verdict = fuse([ev(EvidenceSource.SERIES, CensorState.UNCENSORED, 0.8, "FC2")])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.confidence == 0
    assert verdict.needs_review


def test_weak_visual_hit_does_not_decide():
    """视觉证据置信度低于阈值时不采纳，继续走后面的规则。"""
    verdict = fuse([
        ev(EvidenceSource.VISUAL, CensorState.CENSORED, 0.4, "弱命中"),
        ev(EvidenceSource.SERIES, CensorState.UNCENSORED, 0.8, "FC2"),
    ])
    assert verdict.state is CensorState.UNKNOWN


def test_tag_only_uncensored_is_flagged_for_review():
    """这正是用户不信任的那一路：只凭标签给低置信度 + 强制复核。"""
    verdict = fuse([ev(EvidenceSource.SERVER_TAG, CensorState.UNCENSORED, 0.45, "标签:无码")])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.confidence <= 0.5
    assert verdict.needs_review


def test_tag_plus_quiet_visual_raises_confidence():
    verdict = fuse([
        ev(EvidenceSource.NFO, CensorState.UNCENSORED, 0.5, "NFO:无码"),
        ev(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0.0, "未见网格"),
    ])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.confidence == 0
    assert verdict.needs_review


def test_nothing_means_unknown_and_review():
    verdict = fuse([ev(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0.0, "未见网格"),
                    ev(EvidenceSource.SERVER_TAG, CensorState.CENSORED, 0.4, "标签:有码")])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.needs_review
    assert "未见网格" in verdict.reason


def test_confidence_is_clamped_to_source_ceiling():
    """弱来源不许自称很确定。"""
    assert ev(EvidenceSource.FILENAME, CensorState.UNCENSORED, 0.99).confidence <= 0.45
    assert ev(EvidenceSource.MANUAL, CensorState.CENSORED, 0.10).confidence == 0.10


def test_empty_evidence():
    verdict = fuse([])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.confidence == 0.0


def test_policy_is_configurable():
    strict = FusionPolicy(visual_confirm=0.95)
    verdict = fuse([ev(EvidenceSource.VISUAL, CensorState.CENSORED, 0.85, "画面命中")], strict)
    assert verdict.state is CensorState.UNKNOWN


def test_explain_rows():
    verdict = fuse([ev(EvidenceSource.SERIES, CensorState.UNCENSORED, 0.8, "FC2")])
    rows = explain(verdict)
    assert rows and rows[0][0] == "series"


def test_conflicting_visual_plugins_require_review():
    verdict = fuse([
        ev(EvidenceSource.VISUAL, CensorState.CENSORED, .85, 'grid'),
        ev(EvidenceSource.VISUAL, CensorState.UNCENSORED, .95, 'other algorithm'),
    ])
    assert verdict.state is CensorState.UNKNOWN
    assert verdict.needs_review
    assert '冲突' in verdict.reason
