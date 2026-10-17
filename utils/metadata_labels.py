"""Normalize Jellyfin/Emby tag and genre labels without changing label punctuation."""
from __future__ import annotations

import re

_LABEL_SEPARATOR = re.compile(r'[|｜]')


def normalize_metadata_labels(value) -> list[str]:
    """Flatten pipe-delimited strings/list entries, trim and deduplicate in source order.

    Applies only to Tags/Genres. Commas, slashes, colons and case are meaningful
    parts of an individual label and are preserved. Input containers are unchanged.
    """
    entries = value if isinstance(value, (list, tuple)) else [value]
    labels = []
    seen = set()
    for entry in entries:
        if isinstance(entry, dict):
            entry = entry.get('Name')
        if not isinstance(entry, str):
            continue
        for token in _LABEL_SEPARATOR.split(entry):
            label = token.strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
    return labels
