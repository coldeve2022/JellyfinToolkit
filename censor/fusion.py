"""证据融合 —— 把一堆弱信号拧成一个结论，并且说清楚为什么。

裁定顺序（**这就是本项目的"检码宪法"**，改之前先想清楚）

1. **人工确认**永远最高。你点过的结论不会被任何算法推翻。
2. **画面检出马赛克** → 有码。这是唯一能证明"文件里确实还有码"的证据，
   优先级压过一切元数据 —— 因为用户真正不信任的正是元数据。
3. **番号属于天生无码厂牌**（FC2 / HEYZO / 1pondo …）→ 无码。
   这是作品级事实，不依赖任何刮削标签。
4. 只有 **NFO / Jellyfin 标签** 说无码 → 标成"无码（待确认）"，
   ``needs_review=True``。**这正是历史误判的来源，所以不给高置信度。**
5. 剩下全部 → 未定，进人工复核队列。

为什么 4 不直接给结论：刮削标签描述的是"这部作品"，
而用户要判的是"这个文件"。文件被 Lada 破解过、或被换成流出版之后，
标签往往还停在"有码"，反过来也有标签写着"无码"但文件其实还是有码的。
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

    visual = _pick(items, EvidenceSource.VISUAL)
    series = _pick(items, EvidenceSource.SERIES)
    work = _pick(items, EvidenceSource.WORK_DB)
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
        verdict.needs_review = False
        conflict = (series is not None and series.state is CensorState.UNCENSORED) or tag_uncensored
        verdict.reason = visual.detail or "画面检出马赛克"
        if conflict:
            verdict.needs_review = True
            verdict.reason += "（与番号/标签的无码标记冲突，建议核对）"
        return verdict

    # 3. 天生无码厂牌
    if series is not None and series.state is CensorState.UNCENSORED \
            and series.confidence >= pol.series_confirm:
        verdict.state = CensorState.UNCENSORED
        verdict.confidence = series.confidence
        verdict.needs_review = False
        verdict.reason = series.detail or "番号属于天生无码厂牌"
        return verdict

    # 4. 作品库：这个番号在库上本身就是无码版本
    if work is not None and work.state is CensorState.UNCENSORED \
            and work.confidence >= pol.visual_confirm:
        verdict.state = CensorState.UNCENSORED
        verdict.confidence = work.confidence
        # 作品级事实不等价于"这个文件"，所以仍要人扫一眼
        verdict.needs_review = True
        verdict.reason = work.detail or "作品库显示该番号无码发行"
        return verdict

    # 5. 只有标签说无码 —— 压低置信度并明确要求复核
    if tag_uncensored and not tag_censored:
        visual_quiet = visual is not None and visual.state is CensorState.UNKNOWN
        verdict.state = CensorState.UNCENSORED
        verdict.confidence = (pol.tag_supported_confidence if visual_quiet
                              else pol.tag_only_confidence)
        verdict.needs_review = True
        verdict.reason = "仅标签/文件名显示无码，画面未提供确证"
        return verdict

    # 6. 未定
    verdict.state = CensorState.UNKNOWN
    verdict.confidence = 0.0
    verdict.needs_review = pol.unknown_needs_review
    if visual is not None and visual.state is CensorState.UNKNOWN and visual.detail:
        verdict.reason = visual.detail
    if tag_censored:
        verdict.reason = (verdict.reason + "；" if verdict.reason else "") + "标签显示有码"
    if not verdict.reason:
        verdict.reason = "证据不足"
    return verdict


def explain(verdict: Verdict) -> list[tuple[str, str]]:
    """把结论拆成「来源 → 说明」列表，直接喂给界面表格。"""
    rows = []
    for ev in verdict.evidence:
        if ev.confidence <= 0 and ev.state is CensorState.UNKNOWN:
            rows.append((ev.source.value, ev.detail or "无结论"))
            continue
        rows.append((ev.source.value,
                     f"{ev.state.label} {ev.confidence:.2f} {ev.detail}".strip()))
    return rows
