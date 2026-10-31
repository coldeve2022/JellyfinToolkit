"""One file-to-work projection and one predicate for the replacement workspace."""
from dataclasses import dataclass, field
from typing import Optional

from censor.release_filter import FIELDS, FilterRule, evaluate
from censor.releases.models import ReleaseReport, normalize_number
from utils.library_scope import normalize_scope_path


@dataclass
class NumberRow:
    number: str
    paths: list[str] = field(default_factory=list)
    report: Optional[ReleaseReport] = None
    records: list = field(default_factory=list)

    @property
    def key(self):
        return normalize_number(self.number) or ('path:' + normalize_scope_path(self.paths[0])
                                                if self.paths else '（无番号）')

    @property
    def searchable(self):
        return bool(normalize_number(self.number))

    @property
    def state_key(self):
        return 'todo' if self.report is None else self.report.stage

    @property
    def state_text(self):
        return self.report.as_text() if self.report else ('未查' if self.searchable else '无番号')

    @property
    def best_magnet_text(self):
        magnets = [magnet for candidate in (self.report.candidates if self.report else ())
                   for magnet in candidate.magnets]
        return max(magnets, key=lambda m: m.size_mb).size_text if magnets else '—'


@dataclass
class ReleaseQuery:
    words: tuple = ()
    quick: dict = field(default_factory=dict)
    columns: dict = field(default_factory=dict)
    rules: tuple = ()
    logic: str = 'and'
    only_keys: frozenset = frozenset()

    def matches(self, row, ctx=None, *, exclude_column=None):
        ctx = ctx or {}
        if self.only_keys and row.key not in self.only_keys:
            return False
        for key, value in self.quick.items():
            if value not in ('', 'all', None) and not evaluate([FilterRule(key, 'eq', value)], 'and', row, ctx):
                return False
        for key, values in self.columns.items():
            if key != exclude_column and key in FIELDS and str(FIELDS[key].getter(row, ctx)) not in values:
                return False
        if self.words:
            lookup = ctx.get('review', lambda path: {})
            text = [row.number, *row.paths]
            for path in row.paths:
                item = lookup(path) or {}
                text.extend([item.get('name', ''), *(item.get('tags') or []), *(item.get('genres') or [])])
            haystack = ' '.join(str(x or '') for x in text).casefold()
            if not all(word in haystack for word in self.words):
                return False
        return evaluate(self.rules, self.logic, row, ctx)
