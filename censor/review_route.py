"""Review workload categories; low risk is deliberately not proof of absence."""
from pathlib import Path
from censor.models import CensorState, EvidenceSource, ProcessingStatus
from censor.tag_signals import classify_tag


def category(result):
    if result.verdict.of(EvidenceSource.MANUAL) is not None:
        return 'manual'
    if result.status is not ProcessingStatus.COMPLETED:
        return 'failed'
    visuals = [e for e in result.verdict.evidence if e.source is EvidenceSource.VISUAL]
    if result.state is not CensorState.UNKNOWN:
        return 'review'
    if (result.state is CensorState.UNKNOWN and visuals and not result.needs_review
            and all(e.state is CensorState.UNKNOWN and e.review_required is False for e in visuals)):
        return 'low_risk'
    if any(candidate.score > 0 for candidate in result.candidates):
        return 'review'
    uncensored, censored = classify_tag(Path(result.path).stem)
    if uncensored and not censored:
        return 'deferred'
    return 'review'


def needs_review(result):
    return category(result) in ('review', 'failed')
