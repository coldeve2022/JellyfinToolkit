"""检索编排、缓存、传输的用例 —— 全部离线，用假源替代真实站点。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases import ReleaseOptions, ReleaseService  # noqa: E402
from censor.releases.base import ReleaseSource  # noqa: E402
from censor.releases.models import (  # noqa: E402
    Availability, MagnetLink, ReleaseCandidate, ReleaseQuery, ReleaseReport,
)
from censor.releases.store import ReleaseStore  # noqa: E402
from censor.releases.transport import (  # noqa: E402
    HttpTransport, TransportBlocked, TransportConfig, TransportError,
    TransportNotFound, detect_proxy,
)


# ── 假检索源 ────────────────────────────────────────────────

class FakeSource(ReleaseSource):
    name = "fake"
    title = "假源"
    version = "1"

    def __init__(self, transport=None, cfg=None, *, results=None, error=None):
        super().__init__(transport, cfg)
        self._results = results or []
        self._error = error
        self.calls = 0

    def search(self, query, *, should_stop=lambda: False, log=lambda _m: None, limit=4):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._results)


def _cand(uncensored=True, number="ABC-123", confidence=0.9) -> ReleaseCandidate:
    return ReleaseCandidate(
        source="fake", number=number, title=f"{number} title",
        page_url="https://example.invalid/x", maker="日本コンテンツ流通" if uncensored else "ひよこ",
        uncensored=uncensored, uncensored_reason="测试",
        uncensored_confidence=confidence if uncensored else 0.0,
        magnets=(MagnetLink(uri="magnet:?xt=urn:btih:" + "a" * 40, size_mb=8000),),
    )


class _DummyTransport:
    """假源不真的发请求，但基类要求有传输层才算可用。"""

    description = "假传输"

    def get(self, url, **_kwargs):  # pragma: no cover - 不该被调用
        raise AssertionError("假源不应发起网络请求")


def _make_source(*, name="fake", **kwargs) -> FakeSource:
    source = FakeSource(_DummyTransport(), None, **kwargs)
    source.name = name
    return source


def _service(tmp_path, source: FakeSource, *, name="fake") -> ReleaseService:
    return ReleaseService(
        {"release_sources": [name]},
        data_dir=tmp_path,
        factory=lambda _name: source,
    )


# ── 编排 ────────────────────────────────────────────────────

def test_lookup_confirmed(tmp_path):
    svc = _service(tmp_path, _make_source(results=[_cand()]))
    report = svc.lookup("ABC-123")
    assert report.availability is Availability.CONFIRMED
    assert report.has_download
    assert report.best is not None and report.best.magnets


def test_lookup_none_when_only_censored_entries(tmp_path):
    svc = _service(tmp_path, _make_source(results=[_cand(uncensored=False)]))
    report = svc.lookup("ABC-123")
    assert report.availability is Availability.NONE
    assert not report.has_download


def test_lookup_likely_for_soft_evidence(tmp_path):
    svc = _service(tmp_path, _make_source(results=[_cand(confidence=0.55)]))
    assert svc.lookup("ABC-123").availability is Availability.LIKELY


def test_lookup_unknown_when_blocked(tmp_path):
    svc = _service(tmp_path, _make_source(error=TransportBlocked("Cloudflare")))
    report = svc.lookup("ABC-123")
    assert report.availability is Availability.UNKNOWN
    assert "拦住" in report.note or "拦截" in report.note
    assert "fake" in report.sources


def test_lookup_unknown_without_number(tmp_path):
    svc = _service(tmp_path, FakeSource())
    report = svc.lookup("")
    assert report.availability is Availability.UNKNOWN
    assert "番号" in report.note


def test_failed_source_does_not_hide_other_sources(tmp_path):
    """一个站失败，另一个站的结果仍然要算数。"""
    good = _make_source(name="good", results=[_cand()])
    bad = _make_source(name="bad", error=RuntimeError("boom"))
    svc = ReleaseService(
        {"release_sources": ["bad", "good"]}, data_dir=tmp_path,
        factory=lambda name: bad if name == "bad" else good,
    )
    report = svc.lookup("ABC-123")
    assert report.availability is Availability.CONFIRMED
    assert "失败" in report.sources["bad"]
    assert "命中" in report.sources["good"]


def test_cache_prevents_second_request(tmp_path):
    source = _make_source(results=[_cand()])
    svc = _service(tmp_path, source)
    svc.lookup("ABC-123")
    assert source.calls == 1
    report = svc.lookup("ABC-123")
    assert source.calls == 1, "缓存命中不应再请求站点"
    assert report.elapsed == 0.0


def test_force_ignores_cache(tmp_path):
    source = _make_source(results=[_cand()])
    svc = _service(tmp_path, source)
    svc.lookup("ABC-123")
    svc.lookup("ABC-123", options=ReleaseOptions(force=True))
    assert source.calls == 2


def test_expired_cache_is_refetched(tmp_path):
    store = ReleaseStore(tmp_path / "censor.db")
    old = ReleaseReport(number="ABC123", availability=Availability.NONE,
                        queried_at=time.time() - 30 * 86400)
    store.put(old)
    source = _make_source(results=[_cand()])
    svc = ReleaseService({"release_sources": ["fake"]}, store=store,
                         factory=lambda _n: source)
    report = svc.lookup("ABC-123", options=ReleaseOptions(ttl_days=1))
    assert source.calls == 1
    assert report.availability is Availability.CONFIRMED


def test_lookup_many_reports_progress(tmp_path):
    svc = _service(tmp_path, _make_source(results=[_cand()]))
    seen = []
    reports = list(svc.lookup_many(
        ["ABC-123", "DEF-456"],
        progress=lambda done, total, _report: seen.append((done, total))))
    assert len(reports) == 2
    assert seen[-1] == (2, 2)


# ── 缓存 ────────────────────────────────────────────────────

def test_store_roundtrip_preserves_magnets(tmp_path):
    store = ReleaseStore(tmp_path / "censor.db")
    report = ReleaseReport(number="ABC123", availability=Availability.CONFIRMED,
                           candidates=(_cand(),), sources={"fake": "命中 1 条"},
                           queried_at=time.time())
    store.put(report)
    loaded = store.get("abc-123")
    assert loaded is not None
    assert loaded.availability is Availability.CONFIRMED
    assert loaded.candidates[0].magnets[0].size_mb == 8000
    assert loaded.candidates[0].uncensored_reason == "测试"


def test_store_stats_and_queue(tmp_path):
    store = ReleaseStore(tmp_path / "censor.db")
    store.put(ReleaseReport(number="A1", availability=Availability.CONFIRMED))
    store.put(ReleaseReport(number="A2", availability=Availability.UNKNOWN))
    stats = store.stats()
    assert stats.total == 2 and stats.confirmed == 1 and stats.unknown == 1

    store.enqueue(["A1", "A3"], priority=0, note="x")
    assert set(store.pending(10)) == {"A1", "A3"}
    store.done("A1")
    assert store.pending(10) == ["A3"]
    assert store.queue_stats().get("done") == 1


def test_store_signature_mismatch_invalidates(tmp_path):
    store = ReleaseStore(tmp_path / "censor.db")
    store.put(ReleaseReport(number="A1", availability=Availability.NONE))
    assert store.signature_matches("A1", "")          # 未写签名时视为匹配
    assert not store.signature_matches("A1", "v2")


# ── 传输 ────────────────────────────────────────────────────

def test_cloudflare_page_is_blocked_not_empty():
    body = b"<html><title>Just a moment...</title></html>"
    assert HttpTransport._looks_blocked(body)


def test_explicit_proxy_wins_over_detection():
    assert detect_proxy("http://127.0.0.1:9999") == "http://127.0.0.1:9999"


@pytest.fixture(autouse=True)
def _allow_offline_switch(monkeypatch):
    """这些用例只验证传输层逻辑（`_once` 被打桩），需要关掉全局离线开关。"""
    monkeypatch.delenv("JELLYFIN_TOOLKIT_OFFLINE", raising=False)


def test_offline_mode_blocks_network(monkeypatch):
    from censor.releases.transport import offline

    monkeypatch.setenv("JELLYFIN_TOOLKIT_OFFLINE", "1")
    assert offline()
    transport = HttpTransport(TransportConfig(proxy="", timeout=1, retries=0, min_interval=0))
    with pytest.raises(TransportError, match="离线模式"):
        transport.get("https://example.invalid/x")


def test_404_is_not_retried():
    calls = {"n": 0}

    class T(HttpTransport):
        def _once(self, url, referer, extra=None):  # noqa: ARG002
            calls["n"] += 1
            raise TransportNotFound("HTTP 404")

    transport = T(TransportConfig(proxy="", timeout=1, retries=3, min_interval=0))
    with pytest.raises(TransportNotFound):
        transport.get("https://example.invalid/x")
    assert calls["n"] == 1, "404 重试没有意义，只会拖慢整批检索"


def test_transient_error_is_retried_then_succeeds():
    calls = {"n": 0}

    class T(HttpTransport):
        def _once(self, url, referer, extra=None):  # noqa: ARG002
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransportError("temporary")
            return b"<html>ok</html>"

    transport = T(TransportConfig(proxy="", timeout=1, retries=2, min_interval=0))
    assert "ok" in transport.get("https://example.invalid/x")
    assert calls["n"] == 2


def test_dead_route_falls_through_to_next_and_pins_it():
    seen = []

    class T(HttpTransport):
        def _once(self, url, referer, extra=None):  # noqa: ARG002
            seen.append(self.config.proxy)
            if self.config.proxy == "http://bad.invalid":
                raise TransportError("connect failed")
            return b"<html>ok</html>"

    transport = T(TransportConfig(proxy="http://bad.invalid",
                                  fallbacks=("http://good.invalid", ""),
                                  timeout=1, retries=0, min_interval=0))
    assert "ok" in transport.get("https://example.invalid/x")
    assert transport.config.proxy == "http://good.invalid"
    # 第二条请求直接用钉住的好线路，不再试坏线路
    transport.get("https://example.invalid/y")
    assert seen.count("http://bad.invalid") == 1


def test_source_reports_unavailable_without_transport():
    source = FakeSource()
    ok, why = source.available()
    assert not ok and "传输层" in why
    # JavDb 的 require_transport 才会因为没传输层报错
    from censor.releases.javdb import JavDbSource

    with pytest.raises(RuntimeError):
        JavDbSource(None).require_transport()


def test_query_key_uses_number_then_title():
    assert ReleaseQuery(number="abc-123").key == "ABC123"
    assert ReleaseQuery(title_hint="PIYO 170").key == "PIYO170"
    assert not ReleaseQuery().usable
