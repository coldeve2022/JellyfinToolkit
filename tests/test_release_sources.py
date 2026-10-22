"""无码版本检索的用例 —— 全部离线，用真实页面裁出来的夹具锁住解析。

夹具来自 2026-10 真实抓取（PIYO-170，一部有码作品），只保留解析需要的结构。
站点改版时这些用例会先红，比等到用户报"查不到"要早得多。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases.base import same_number  # noqa: E402
from censor.releases.javbus import JavBusSource  # noqa: E402
from censor.releases.javdb import JavDbSource  # noqa: E402
from censor.releases.models import (  # noqa: E402
    Availability, MagnetLink, ReleaseCandidate, ReleaseQuery, normalize_number,
)
from censor.releases.supjav import SupjavSource  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class FakeTransport:
    """按 URL 片段返回夹具；记录被请求过的地址，便于断言"没有乱翻页"。"""

    def __init__(self, pages: dict[str, str], *, fail: Exception | None = None):
        self.pages = pages
        self.fail = fail
        self.calls: list[str] = []

    @property
    def description(self) -> str:
        return "假传输"

    def get(self, url: str, *, referer: str = "", headers=None) -> str:  # noqa: ARG002
        self.calls.append(url)
        if self.fail is not None:
            raise self.fail
        for key, body in self.pages.items():
            if key in url:
                return body
        raise AssertionError(f"未预期的请求: {url}")


# ── 番号归一 ────────────────────────────────────────────────

def test_normalize_number_ignores_separators():
    assert normalize_number("piyo-170") == normalize_number("PIYO_170") == "PIYO170"


def test_same_number_rejects_prefix_collision():
    assert same_number("PIYO-170", "piyo 170")
    assert not same_number("PIYO-17", "PIYO-170")
    assert not same_number("", "")


# ── JavDB ──────────────────────────────────────────────────

def test_javdb_search_filters_other_numbers():
    source = JavDbSource(FakeTransport({}))
    query = ReleaseQuery(number="PIYO-170")
    rows = source._parse_search(fixture("javdb_search_piyo170.html"), query)
    assert rows, "搜索页应当解析出条目"
    assert all(same_number(row["number"], "PIYO-170") for row in rows), rows
    assert rows[0]["href"].startswith("/v/")


def test_javdb_detail_parses_fields_and_magnets():
    source = JavDbSource(FakeTransport({}))
    row = {"href": "/v/k4Gk6z", "number": "PIYO-170", "title": "PIYO-170"}
    candidate = source._parse_detail(fixture("javdb_detail_piyo170.html"), row,
                                     ReleaseQuery(number="PIYO-170"))
    assert candidate.number == "PIYO-170", "番号里的多余空格必须被清掉"
    assert candidate.maker == "ひよこ"
    assert "單體作品" in candidate.categories
    assert candidate.duration_minutes == 210
    assert candidate.magnets, "详情页应当解析出磁链"
    assert all(m.uri.startswith("magnet:?xt=") for m in candidate.magnets)
    assert max(m.size_mb for m in candidate.magnets) > 1000      # 9.54GB
    assert candidate.uncensored is False, "有码厂牌 + 无无码类别 → 不算无码"


def test_javdb_uncensored_maker_is_confirmed():
    """片商换成流出专营（日本コンテンツ流通）→ 必须判成无码条目。"""
    html = fixture("javdb_detail_piyo170.html").replace("ひよこ", "日本コンテンツ流通")
    source = JavDbSource(FakeTransport({}))
    candidate = source._parse_detail(html, {"href": "/v/x", "number": "NCRA-026"},
                                     ReleaseQuery(number="NCRA-026"))
    assert candidate.uncensored
    assert "无码厂牌" in candidate.uncensored_reason
    assert candidate.uncensored_confidence >= 0.8


def test_javdb_uncensored_category_is_detected():
    html = fixture("javdb_detail_piyo170.html").replace("單體作品", "無碼流出")
    source = JavDbSource(FakeTransport({}))
    candidate = source._parse_detail(html, {"href": "/v/x", "number": "STARS-016"},
                                     ReleaseQuery(number="STARS-016"))
    assert candidate.uncensored
    assert "类别" in candidate.uncensored_reason


def test_javdb_search_walks_detail_pages():
    transport = FakeTransport({
        "/search": fixture("javdb_search_piyo170.html"),
        "/v/": fixture("javdb_detail_piyo170.html"),
    })
    source = JavDbSource(transport)
    found = source.search(ReleaseQuery(number="PIYO-170"), limit=2)
    assert found
    assert all(c.source == "javdb" for c in found)
    assert len(transport.calls) <= 3, "只应读搜索页 + 少量详情页"


# ── JavBus ─────────────────────────────────────────────────

def test_javbus_detail_parses_and_reads_magnets():
    transport = FakeTransport({
        "/PIYO-170": fixture("javbus_detail_piyo170.html"),
        "uncledatoolsbyajax": fixture("javbus_magnets.html"),
    })
    source = JavBusSource(transport)
    found = source.search(ReleaseQuery(number="PIYO-170"))
    assert len(found) == 1
    candidate = found[0]
    assert candidate.number == "PIYO-170"
    assert candidate.maker == "ひよこ"
    assert candidate.duration_minutes == 216
    assert candidate.uncensored is False
    assert candidate.magnets, "ajax 接口里的磁链应当被解析出来"
    assert max(m.size_mb for m in candidate.magnets) > 1000


def test_javbus_uc_flag_means_uncensored():
    html = fixture("javbus_detail_piyo170.html").replace("var uc = 0;", "var uc = 1;")
    transport = FakeTransport({"/PIYO-170": html, "uncledatoolsbyajax": "<html></html>"})
    found = JavBusSource(transport).search(ReleaseQuery(number="PIYO-170"))
    assert found and found[0].uncensored
    assert "uc=1" in found[0].uncensored_reason


# ── Supjav ─────────────────────────────────────────────────

SUPJAV_PAGE = """
<html><body>
<a href="https://supjav.com/123456.html" title="x">
  NCRA-026 【Decensored】Full version
</a>
<a href="https://supjav.com/123457.html">OTHER-001 ordinary title</a>
</body></html>
"""


def test_supjav_marks_decensored_as_soft_evidence():
    source = SupjavSource(FakeTransport({"supjav": SUPJAV_PAGE}))
    found = source.search(ReleaseQuery(number="NCRA-026"))
    assert len(found) == 1
    assert found[0].uncensored
    assert found[0].uncensored_confidence < 0.8, "标题线索只能算软依据"


def test_supjav_skips_unrelated_results():
    source = SupjavSource(FakeTransport({"supjav": SUPJAV_PAGE}))
    assert source.search(ReleaseQuery(number="ZZZZ-999")) == []


# ── 结论判定 ───────────────────────────────────────────────

def _candidate(uncensored: bool, confidence: float = 0.9) -> ReleaseCandidate:
    return ReleaseCandidate(source="t", number="ABC-123", uncensored=uncensored,
                            uncensored_confidence=confidence if uncensored else 0.0)


def test_decide_availability_table():
    from censor.releases.service import decide_availability as decide

    assert decide([]) is Availability.UNKNOWN
    assert decide([_candidate(False)]) is Availability.NONE
    assert decide([_candidate(False), _candidate(True, 0.9)]) is Availability.CONFIRMED
    assert decide([_candidate(True, 0.55)]) is Availability.LIKELY
    assert decide([_candidate(True, 0.55), _candidate(True, 0.9)]) is Availability.CONFIRMED


def test_magnet_size_text_and_best():
    candidate = ReleaseCandidate(
        source="t",
        magnets=(MagnetLink(uri="magnet:?xt=a", size_mb=500),
                 MagnetLink(uri="magnet:?xt=b", size_mb=9000)),
    )
    assert candidate.best_magnet.uri == "magnet:?xt=b"
    assert candidate.best_magnet.size_text.endswith("GB")
    assert candidate.total_size_mb == 9000
