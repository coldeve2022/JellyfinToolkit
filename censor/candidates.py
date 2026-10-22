"""Content and time deduplication shared by visual plugins and service merging."""

from __future__ import annotations

from typing import Iterable
from censor.models import FrameCandidate


def select_candidates(
    candidates: Iterable[FrameCandidate],
    *,
    gap_seconds: float,
    minimum_score: float = 0.0,
    top: int | None = None,
) -> list[FrameCandidate]:
    selected: list[FrameCandidate] = []
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.timestamp)):
        if candidate.score < minimum_score or (top is not None and len(selected) >= top):
            continue
        if all(
            (not candidate.frame_hash or candidate.frame_hash != c.frame_hash)
            and abs(candidate.timestamp - c.timestamp) >= gap_seconds
            and candidate.timestamp != c.timestamp
            for c in selected
        ):
            selected.append(candidate)
    return selected
