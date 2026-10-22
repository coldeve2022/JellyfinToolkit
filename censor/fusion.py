"""Fuse file-level evidence without treating metadata as pixel truth.

A matching manual confirmation has priority. Conflicting visual detectors need
review; strong visual evidence can support a rule-based positive result. Filename,
series, sidecar and server metadata remain clues and cannot prove absence.
Confidence values are internal evidence weights, not calibrated probabilities.
"""

from __future__ import annotations

from dataclasses import dataclass

from censor.models import CensorState, Evidence, EvidenceSource, Verdict

__all__ = ["FusionPolicy", "fuse", "explain"]


@dataclass(frozen=True)
class FusionPolicy:
    """可调阈值。UI/配置里改成"严格"或"宽松"只影响这几个数。"""

    #: 画面证据达到此置信度即可定性（防止弱命中直接改结论）
    visual_confirm: float = 0.60
    #: 番号系列证据达到此置信度即可定性
    series_confirm: float = 0.60
    #: 仅凭标签判定无码时的置信度（刻意压低）
    tag_only_confidence: float = 0.50
    #: 标签与画面一致的置信度
    tag_supported_confidence: float = 0.70
    #: 未定性时是否进人工复核队列
    unknown_needs_review: bool = True


def _pick(evidence: list[Evidence], source: EvidenceSource) -> Evidence | None:
    """同一来源可能有多条，取置信度最高的一条。"""
    hits = [ev for ev in evidence if ev.source is source]
    if not hits:
        return None
    return max(hits, key=lambda ev: ev.confidence)


def fuse(evidence: list[Evidence], policy: FusionPolicy | None = None) -> Verdict:
    """把证据列表融成一个 :class:`Verdict`。"""
    pol = policy or FusionPolicy()
    items = [ev for ev in evidence if ev is not None]
    verdict = Verdict(evidence=items)

    manual = _pick(items, EvidenceSource.MANUAL)
    if manual is not None and manual.state is not CensorState.UNKNOWN:
        verdict.state = manual.state
        verdict.confidence = 1.0
        verdict.needs_review = False
        verdict.reason = f"人工确认：{manual.state.label}"
        return verdict

    visuals = [ev for ev in items if ev.source is EvidenceSource.VISUAL]
    positive = [
        ev
        for ev in visuals
        if ev.state is CensorState.CENSORED and ev.confidence >= pol.visual_confirm
    ]
    negative = [
        ev
        for ev in visuals
        if ev.state is CensorState.UNCENSORED and ev.confidence >= pol.visual_confirm
    ]
    if positive and negative:
        verdict.needs_review = True
        verdict.reason = "视觉检测器结论冲突，需复核"
        return verdict
    visual = _pick(items, EvidenceSource.VISUAL)
    series = _pick(items, EvidenceSource.SERIES)
    tags = [ev for ev in items if ev.source in (EvidenceSource.NFO, EvidenceSource.SERVER_TAG)]

    visual_censored = (
        visual is not None
        and visual.state is CensorState.CENSORED
        and visual.confidence >= pol.visual_confirm
    )
    tag_uncensored = any(ev.state is CensorState.UNCENSORED for ev in tags)
    tag_censored = any(ev.state is CensorState.CENSORED for ev in tags)

    # 2. 画面有码 —— 压过一切
    if visual_censored:
        verdict.state = CensorState.CENSORED
        verdict.confidence = visual.confidence
        verdict.needs_review = True
        conflict = (
            (series is not None and series.state is CensorState.UNCENSORED)
            or tag_uncensored
            or any(
                ev.state is CensorState.UNCENSORED and ev.confidence >= pol.visual_confirm
                for ev in visuals
            )
        )
        verdict.reason = visual.detail or "画面检出马赛克"
        if conflict:
            verdict.needs_review = True
            verdict.reason += "（与番号/标签的无码标记冲突，建议核对）"
        return verdict

    # Metadata describes a work or label, never proof of this file's pixels.

    # 6. 未定
    verdict.state = CensorState.UNKNOWN
    verdict.confidence = 0.0
    verdict.needs_review = pol.unknown_needs_review
    if visuals and all(ev.state is CensorState.UNKNOWN and ev.review_required is False for ev in visuals):
        verdict.needs_review = False
    if visual is not None and visual.state is CensorState.UNKNOWN and visual.detail:
        verdict.reason = visual.detail
    if tag_censored:
        verdict.reason = (verdict.reason + "；" if verdict.reason else "") + "标签显示有码"
    if not verdict.reason:
        verdict.reason = "画面证据不足；元数据仅作为线索" if items else "证据不足"
    return verdict


def explain(verdict: Verdict) -> list[tuple[str, str]]:
    """把结论拆成「来源 → 说明」列表，直接喂给界面表格。"""
    rows = []
    for ev in verdict.evidence:
        if ev.confidence <= 0 and ev.state is CensorState.UNKNOWN:
            rows.append((ev.source.value, ev.detail or "无结论"))
            continue
        label = {
            CensorState.CENSORED: "像素化线索",
            CensorState.UNCENSORED: "未见像素化线索",
            CensorState.UNKNOWN: "待确认",
        }[ev.state]
        rows.append((ev.source.value, f"{label}：{ev.detail}"))
    return rows
