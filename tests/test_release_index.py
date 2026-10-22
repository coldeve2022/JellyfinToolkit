"""站点索引检索的用例 —— 用真实 Bing / DDG 页面裁出来的夹具锁解析。

这套东西的价值在于"不碰目标站也能查到"，所以夹具必须是**真实结果页**，
否则解析写错了也测不出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases.index_search import (  # noqa: E402
    BingEngine, DuckDuckGoEngine, IndexHit, get_engine, lookup_site,
)
from censor.releases.models import ReleaseQuery  # noqa: E402
from censor.releases.supjav import SupjavSource  # noqa: E402
from censor.releases.transport import TransportBlocked  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class FakeTransport:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    @property
    def description(self) -> str:
        return "假传输"

    def get(self, url: str, **_kwargs) -> str:
        self.calls.append(url)
        for key, body in self.pages.items():
            if key in url:
                return body
        raise AssertionError(f"未预期的请求: {url}")


# ── 引擎解析 ────────────────────────────────────────────────

def test_bing_returns_target_site_links():
    hits = BingEngine().parse(fixture("bing_supjav_ncra026.html"))
    assert hits, "Bing 夹具应当解析出结果"
    supjav = [hit for hit in hits if hit.host.endswith("supjav.com")]
    assert supjav, [hit.url for hit in hits]
    assert any("NCRA-026" in hit.title for hit in supjav)
    assert any("無修正流出" in hit.title for hit in supjav)


def test_bing_decodes_redirect_links():
    """Bing 的 /ck/a?...&u=a1<base64> 必须解回真实地址，否则域名过滤全落空。"""
    html = ('<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u='
            'a1aHR0cHM6Ly9zdXBqYXYuY29tLzEzOTYxNC5odG1s&ntb=1">【無修正流出】NCRA-026</a>'
            "</h2><p>desc</p></li>")
    hits = BingEngine().parse(html)
    assert hits and hits[0].url == "https://supjav.com/139614.html"


def test_duckduckgo_decodes_uddg_links():
    hits = DuckDuckGoEngine().parse(fixture("ddg_supjav_ncra026.html"))
    assert hits
    assert all(hit.url.startswith("http") for hit in hits)
    assert any("supjav.com" in hit.url for hit in hits)


def test_duckduckgo_throttle_is_not_empty_result():
    """限流页必须抛"被拦"，不能当成"没有结果"。"""
    with pytest.raises(TransportBlocked, match="限流"):
        DuckDuckGoEngine().parse(fixture("ddg_throttled.html"))


def test_engine_registry_lookup():
    assert get_engine("bing") is not None
    assert get_engine("DuckDuckGo") is not None
    assert get_engine("nope") is None


# ── 站点过滤与去重 ──────────────────────────────────────────

def test_lookup_site_filters_by_host_and_falls_through():
    """第一个引擎没有本站结果时，要自动换下一个引擎。"""
    transport = FakeTransport({
        "bing.com": "<html><li class=\"b_algo\"><h2><a href=\"https://other.com/x\">别的站</a></h2></li></html>",
        "duckduckgo.com": fixture("ddg_supjav_ncra026.html"),
    })
    hits, statuses = lookup_site(transport, "supjav.com", "NCRA-026")
    assert hits
    assert all(hit.host.endswith("supjav.com") for hit in hits)
    assert "bing" in statuses and "duckduckgo" in statuses


def test_lookup_site_dedupes_language_variants():
    raw = [
        IndexHit("https://supjav.com/ja/139614.html", "【無修正流出】NCRA-026 日本語"),
        IndexHit("https://supjav.com/139614.html", "【無修正流出】NCRA-026 English title longer"),
        IndexHit("https://supjav.com/139615.html", "NCRA-027 别的番号"),
    ]
    from censor.releases.index_search import _dedupe

    kept = _dedupe(raw)
    assert len(kept) == 2, [hit.url for hit in kept]


def test_lookup_site_reports_engine_failures_separately():
    class Boom(FakeTransport):
        def get(self, url, **_kwargs):
            raise TransportBlocked("被拦")

    hits, statuses = lookup_site(Boom({}), "supjav.com", "ABC-123")
    assert hits == []
    assert all("失败" in value for value in statuses.values()), statuses


# ── Supjav 判定 ─────────────────────────────────────────────

def test_supjav_marks_uncensored_leak_as_confirmed():
    transport = FakeTransport({"bing.com": fixture("bing_supjav_ncra026.html")})
    found = SupjavSource(transport, {"release_supjav_engines": ["bing"]}).search(
        ReleaseQuery(number="NCRA-026"))
    assert found, "应当命中 NCRA-026"
    top = found[0]
    assert top.uncensored
    assert top.uncensored_confidence >= 0.8, "站点标题写無修正流出 → 确有"
    assert "supjav.com" in top.page_url


def test_supjav_reducing_mosaic_is_soft_evidence():
    """[Reducing Mosaic] 是减码版，不能算"确有"，但也不能当没看见。"""
    from censor.releases.supjav import WEAK_CONFIDENCE, WEAK_HINTS

    ok, confidence, reason = SupjavSource._judge("[Reducing Mosaic] PIYO-170 标题")
    assert ok and confidence == WEAK_CONFIDENCE
    assert confidence < 0.8, "软依据不足以判确有"
    assert "减码" in reason or "薄码" in reason
    assert WEAK_HINTS


def test_supjav_plain_title_is_not_uncensored():
    ok, confidence, _ = SupjavSource._judge("PIYO-170 If The Sex Feels Good - SupJav")
    assert not ok and confidence == 0.0


def test_supjav_filters_numbers_that_merely_share_a_prefix():
    html = ('<li class="b_algo"><h2><a href="https://supjav.com/999999.html">'
            "【無修正流出】NCRA-0261 别的番号</a></h2></li>")
    transport = FakeTransport({"bing.com": html})
    found = SupjavSource(transport, {"release_supjav_engines": ["bing"]}).search(
        ReleaseQuery(number="NCRA-026"))
    assert found == [], "NCRA-026 不该命中 NCRA-0261"


def test_supjav_records_engine_status():
    transport = FakeTransport({"bing.com": fixture("bing_supjav_ncra026.html")})
    source = SupjavSource(transport, {"release_supjav_engines": ["bing"]})
    source.search(ReleaseQuery(number="NCRA-026"))
    assert source.status_detail and "bing" in source.status_detail
