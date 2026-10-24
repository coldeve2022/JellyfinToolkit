"""Filename groups select sampling effort; they never supply visual evidence."""
from __future__ import annotations

import re


def triage_plan(path, cfg, requested, *, explicit=False) -> dict:
    """Partition one complete sampling plan, preserving both temporal endpoints."""
    count = int(requested)
    if count < 1:
        raise ValueError('抽帧数量必须为正数')
    get = cfg.get if isinstance(cfg, dict) else lambda key, default: getattr(cfg, key, default)
    name = str(path).replace('\\', '/').rsplit('/', 1)[-1]
    prefixes = get('censor_triage_prefixes', ['FC2']) or []
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    group = ''
    for prefix in prefixes:
        prefix = str(prefix).strip()
        # Unicode letters/digits block matching; underscores and punctuation
        # are separators, as are spaces and brackets in common filenames.
        token = bool(prefix and re.search(r'(?<![^\W_])' + re.escape(prefix) + r'(?![^\W_])', name, re.I))
        # Common compact spellings still describe the same filename group.
        # Require a complete numeric identifier so FC20/FC2word stay excluded.
        compact = (prefix.casefold() == 'fc2' and re.search(
            r'(?<![^\W_])fc2[\s_-]*(?:ppv[\s_-]*)?\d{6,9}(?![^\W_])', name, re.I))
        if token or compact:
            group = prefix
            break
    initial_count = max(1, min(count, int(get('censor_triage_samples', 30))))
    enabled = bool(group and get('censor_triage_enabled', True) and not explicit and initial_count < count)
    initial = (sorted({round(i * (count - 1) / max(1, initial_count - 1))
                       for i in range(initial_count)}) if enabled else list(range(count)))
    selected = set(initial)
    remaining = [i for i in range(count) if i not in selected]
    return {'enabled': enabled, 'group': group, 'initial_indices': initial,
            'order': initial + remaining, 'planned_samples': count,
            'initial_samples': len(initial), 'phase': 'initial' if enabled else 'full',
            'refined': False, 'low_risk': False}
