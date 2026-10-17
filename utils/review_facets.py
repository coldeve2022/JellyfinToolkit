"""Read-only page facets, safe spelling equivalence and exact prospective counts."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import unicodedata

from utils.metadata_labels import normalize_metadata_labels


def label_key(value):
    # No semantic guesses, traditional/simplified conversion or punctuation loss.
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split()).casefold()


_TECH = re.compile(
    r'^(?:[248]k|vr|3d|[0-9]{3,4}p|[0-9]{3,4}x[0-9]{3,4}|h[ .-]?26[45]|x26[45]|hevc|avc|av1|'
    r'hdr(?:10\+?)?|sdr|dolby vision|dv|uhd|fhd|hd|sd|blu[ -]?ray|b[dr]rip|web[ .-]?(?:dl|rip)|'
    r'dvd(?:rip)?|[0-9]+[ .-]?bit|aac|flac|dts(?:[ .-]?hd)?|truehd|'
    r'字幕|中文字幕|中字|无字幕|高清|超清|原盘|重制版|复刻版|有码|无码|破解|薄码|薄马赛克)$', re.I)


@dataclass(frozen=True)
class FacetOption:
    key: tuple
    label: str
    count: int
    total: int
    group: str
    aliases: tuple


class FacetIndex:
    def __init__(self, items):
        self.postings = defaultdict(set)
        self.by_item = defaultdict(set)
        variants = defaultdict(Counter)
        for item in items:
            for kind, values in (('tag', item.tags), ('genre', item.genres)):
                for raw in normalize_metadata_labels(values):
                    key = (kind, label_key(raw))
                    self.by_item[item.id].add(key)
                    self.postings[key].add(item.id)
                    variants[key][raw] += 1
        self.aliases = {key: tuple(sorted(names)) for key, names in variants.items()}
        self.labels = {key: ' '.join(unicodedata.normalize('NFKC', sorted(names, key=lambda n: (-names[n], n))[0]).split())
                       for key, names in variants.items()}

    def match(self, ids, selected, mode='AND'):
        ids = set(ids)
        if not selected:
            return ids
        if mode == 'OR':
            union = set()
            for key in selected:
                union.update(self.postings.get(key, ()))
            return ids & union
        for key in selected:
            ids.intersection_update(self.postings.get(key, ()))
        return ids

    def options(self, base_ids, selected, mode='AND', single=False):
        base_ids, selected = set(base_ids), set(selected)
        totals, overlaps = Counter(), Counter()
        hits = self.match(base_ids, selected, mode)
        for item_id in base_ids:
            totals.update(self.by_item.get(item_id, ()))
        for item_id in hits:
            overlaps.update(self.by_item.get(item_id, ()))
        result = []
        for key in totals.keys() | selected:
            total = totals[key]
            count = (len(hits) if key in selected else total if single or not selected else
                     len(hits) + total - overlaps[key] if mode == 'OR' else overlaps[key])
            label = self.labels.get(key, key[1])
            group = 'genre' if key[0] == 'genre' else 'technical' if _TECH.fullmatch(key[1]) else 'content'
            result.append(FacetOption(key, label, count, total, group, self.aliases.get(key, (label,))))
        # Stable base frequency, never promote/reorder merely because a pill was selected.
        return sorted(result, key=lambda o: (-o.total, o.key))
