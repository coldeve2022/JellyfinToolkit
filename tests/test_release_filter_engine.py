"""筛选规则引擎：自由组合条件。

用户要"完整的筛选、能充分自定义"，所以筛选从硬编码的几段 if 改成了
字段 + 运算符 + 值的组合。这里守引擎本身：运算符语义、AND/OR、单位换算、
以及"没有条件时一律通过"（否则一打开清单就空了）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.release_filter import FilterRule, describe, evaluate  # noqa: E402
from censor.releases.models import (  # noqa: E402
    Availability, MagnetLink, ReleaseCandidate, ReleaseReport)


class Row:
    """最小可用的行替身：引擎只依赖这些属性。"""

    def __init__(self, number="ABC-123", paths=None, report=None, searchable=True):
        self.number = number
        self.paths = paths if paths is not None else [r"C:/lib/ABC-123.mp4"]
        self.report = report
        self.searchable = searchable

    @property
    def state_key(self):
        return "todo" if self.report is None else self.report.stage


def _magnet(size_mb=0, link=""):
    return MagnetLink(uri=link or "magnet:?xt=urn:btih:" + "a" * 40,
                      name="x", size_mb=size_mb)


def _report(stage=Availability.CONFIRMED, *, candidates=(), queried_at=None,
            sources=None, elapsed=1.0, cache_hit=False):
    return ReleaseReport(number="ABC-123", availability=stage,
                         candidates=tuple(candidates),
                         sources=sources or {}, elapsed=elapsed,
                         queried_at=queried_at if queried_at is not None else time.time(),
                         cache_hit=cache_hit)


def _candidate(source="supjav", uncensored=True, magnet=None, page="https://x/1.html"):
    return ReleaseCandidate(source=source, number="ABC-123", title="t", page_url=page,
                            uncensored=uncensored, uncensored_confidence=0.9,
                            magnets=(magnet,) if magnet else ())


# ── 基础 ───────────────────────────────────────────────────

def test_no_rules_allows_everything():
    assert evaluate([], "and", Row()) is True
    assert evaluate([], "or", Row()) is True


def test_unknown_field_is_ignored():
    assert evaluate([FilterRule("nope", "eq", "x")], "and", Row()) is True


def test_text_operators():
    row = Row(number="YSN-638")
    assert evaluate([FilterRule("number", "contains", "ysn")], "and", row)
    assert evaluate([FilterRule("number", "startswith", "YSN")], "and", row)
    assert not evaluate([FilterRule("number", "contains", "dandy")], "and", row)
    assert evaluate([FilterRule("number", "not_contains", "dandy")], "and", row)
    assert evaluate([FilterRule("number", "eq", "ysn-638")], "and", row)


def test_number_operators_on_files_and_candidates():
    row = Row(paths=[r"C:/a.mp4", r"C:/b.mp4"],
              report=_report(candidates=[_candidate(), _candidate()]))
    assert evaluate([FilterRule("files", "eq", "2")], "and", row)
    assert evaluate([FilterRule("files", "gte", "2")], "and", row)
    assert not evaluate([FilterRule("files", "gt", "2")], "and", row)
    assert evaluate([FilterRule("candidates", "eq", "2")], "and", row)
    assert evaluate([FilterRule("candidates", "lte", "1")], "and", row) is False


def test_bool_operators():
    row = Row(report=_report(candidates=[_candidate(uncensored=True)]))
    assert evaluate([FilterRule("has_uncensored", "is_true")], "and", row)
    assert not evaluate([FilterRule("has_uncensored", "is_false")], "and", row)


def test_tag_filters_use_filenames():
    row = Row(paths=[r"C:/lib/ABC-123-4K-60FPS-C.mp4"])
    assert evaluate([FilterRule("tag_4k", "is_true")], "and", row)
    assert evaluate([FilterRule("tag_60fps", "is_true")], "and", row)
    assert evaluate([FilterRule("tag_cnsub", "is_true")], "and", row)
    assert not evaluate([FilterRule("tag_leak", "is_true")], "and", row)


def test_magnet_size_accepts_gb_suffix():
    """填 "10gb" 应该等价于 10×1024³ 字节 —— 没人愿意自己乘。"""
    row = Row(report=_report(candidates=[_candidate(magnet=_magnet(size_mb=12 * 1024))]))
    assert evaluate([FilterRule("magnet_size", "gte", "10gb")], "and", row)
    assert not evaluate([FilterRule("magnet_size", "gte", "20gb")], "and", row)
    assert evaluate([FilterRule("magnets", "gte", "1")], "and", row)


def test_date_filters():
    old = Row(report=_report(queried_at=time.time() - 30 * 86400))
    fresh = Row(report=_report(queried_at=time.time() - 86400))
    assert evaluate([FilterRule("queried", "within_days", "7")], "and", fresh)
    assert not evaluate([FilterRule("queried", "within_days", "7")], "and", old)
    assert evaluate([FilterRule("queried", "older_days", "7")], "and", old)
    assert evaluate([FilterRule("queried", "never")], "and", Row(report=None))


def test_enum_filters_for_state_and_jellyfin():
    row = Row(report=_report())
    assert evaluate([FilterRule("state", "eq", "confirmed")], "and", row)
    assert evaluate([FilterRule("state", "ne", "todo")], "and", row)

    ctx = {"review": lambda path: {"is_favorite": True, "played": False}}
    assert evaluate([FilterRule("favorite", "eq", "favorite")], "and", row, ctx)
    assert evaluate([FilterRule("played", "eq", "unplayed")], "and", row, ctx)


# ── 组合 ───────────────────────────────────────────────────

def test_and_or_logic():
    row = Row(number="YSN-638", report=_report(candidates=[_candidate()]))
    rules = [FilterRule("number", "contains", "ysn"),
             FilterRule("candidates", "gte", "5")]
    assert evaluate(rules, "and", row) is False
    assert evaluate(rules, "or", row) is True


def test_describe_is_readable():
    assert "番号" in describe(FilterRule("number", "contains", "YSN"))
    assert "已喜欢" in describe(FilterRule("favorite", "eq", "favorite"))
    assert describe(FilterRule("tag_4k", "is_true")).endswith("是")
