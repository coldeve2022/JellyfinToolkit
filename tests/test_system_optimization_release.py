"""Behavior regressions from the system audit; fake only external HTTP/sources."""
import time

import pytest

from censor.models import ProcessingStatus
from censor.releases.base import ReleaseSource
from censor.releases.models import Availability, ReleaseCandidate
from censor.releases.service import ReleaseOptions, ReleaseService
from censor.releases.transport import HttpTransport, TransportConfig, TransportError


class Source(ReleaseSource):
    name = "fake"

    def __init__(self, transport=None, *, fail=False, uncensored=True):
        super().__init__(transport or object())
        self.fail, self.uncensored = fail, uncensored
        self.calls = 0
        self.limits = []

    def search(self, query, *, limit=4, **kwargs):
        self.calls += 1
        self.limits.append(limit)
        if self.fail:
            raise TransportError("temporary timeout")
        if isinstance(self.transport, HttpTransport):
            self.transport.get("https://synthetic.invalid/" + query.number)
        return [ReleaseCandidate(self.name, number=query.number, uncensored=self.uncensored,
                                 uncensored_confidence=0.9 if self.uncensored else 0.)]


def service(tmp_path, source, **cfg):
    return ReleaseService({"release_sources": ["fake"], **cfg}, data_dir=tmp_path,
                          factory=lambda _: source)


def test_independent_queries_do_not_exhaust_session_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("JELLYFIN_TOOLKIT_OFFLINE", raising=False)
    transport = HttpTransport(TransportConfig(max_requests=2, retries=0, min_interval=0))
    monkeypatch.setattr(transport, "_once", lambda *args: b"ok")
    svc = service(tmp_path, Source(transport))
    reports = [svc.lookup(f"DEMO-{i}") for i in range(45)]
    assert all(r.availability is Availability.CONFIRMED for r in reports)


def test_cancel_preserves_successful_cache_and_pending_queue(tmp_path):
    svc = service(tmp_path, Source())
    svc.lookup("ABC-123")
    svc.queue(["ABC-123"])
    result = svc.lookup("ABC-123", options=ReleaseOptions(force=True), should_stop=lambda: True)
    assert svc.cached("ABC-123").availability is Availability.CONFIRMED
    assert svc.pending() == ["ABC123"]
    assert result.status is ProcessingStatus.CANCELLED
    assert "取消" in result.note


def test_transient_failure_is_not_a_reusable_final_report(tmp_path):
    source = Source(fail=True)
    svc = service(tmp_path, source)
    first = svc.lookup("ABC-123")
    source.fail = False
    second = svc.lookup("ABC-123")
    assert second.availability is Availability.CONFIRMED
    assert first.status is ProcessingStatus.FAILED
    assert source.calls == 2


def test_failed_force_keeps_previous_success_but_records_attempt(tmp_path):
    source = Source()
    svc = service(tmp_path, source)
    svc.lookup("ABC-123")
    source.fail = True
    result = svc.lookup("ABC-123", options=ReleaseOptions(force=True))
    assert result.status is ProcessingStatus.FAILED
    assert svc.cached("ABC-123").availability is Availability.CONFIRMED
    assert svc.store.latest_attempt("ABC-123").status is ProcessingStatus.FAILED


def test_disabled_service_does_not_execute_source(tmp_path):
    source = Source()
    result = service(tmp_path, source, release_enabled=False).lookup("ABC-123")
    assert source.calls == 0
    assert "禁用" in result.note


def test_detail_limit_reaches_source(tmp_path):
    source = Source()
    service(tmp_path, source, release_detail_limit=2).lookup("ABC-123")
    assert source.limits == [2]


def test_partial_negative_is_not_a_completed_absence_claim(tmp_path):
    good, bad = Source(uncensored=False), Source(fail=True)
    good.name, bad.name = "good", "bad"
    svc = ReleaseService({"release_sources": ["good", "bad"]}, data_dir=tmp_path,
                         factory=lambda name: good if name == "good" else bad)
    result = svc.lookup("ABC-123")
    assert result.status is ProcessingStatus.PARTIAL
    assert result.availability is Availability.UNKNOWN
    assert result.candidates and "部分" in result.note


def test_source_construction_failure_is_visible(tmp_path):
    def factory(name):
        raise ValueError("synthetic initialization failure")
    svc = ReleaseService({"release_sources": ["broken"]}, data_dir=tmp_path, factory=factory)
    result = svc.lookup("ABC-123")
    assert "broken" in result.sources
    assert "初始化" in result.sources["broken"]
    assert result.status is ProcessingStatus.FAILED


def test_fresh_rejects_expired_and_changed_sources(tmp_path):
    svc = service(tmp_path, Source())
    report = svc.lookup("ABC-123")
    assert svc.fresh("ABC-123") is not None
    report.queried_at = time.time() - 8 * 86400
    svc.store.put(report, svc.signature())
    assert svc.fresh("ABC-123") is None


def test_request_budget_counts_retries(tmp_path, monkeypatch):
    monkeypatch.delenv("JELLYFIN_TOOLKIT_OFFLINE", raising=False)
    transport = HttpTransport(TransportConfig(max_requests=1, retries=2, min_interval=0))
    attempts = []
    def once(*args):
        attempts.append(1)
        raise TransportError("temporary")
    monkeypatch.setattr(transport, "_once", once)
    monkeypatch.setattr("censor.releases.transport.time.sleep", lambda _: None)
    result = service(tmp_path, Source(transport)).lookup("ABC-123")
    assert len(attempts) == 1
    assert result.availability is Availability.UNKNOWN


def test_title_only_is_soft_and_negation_is_not_positive():
    from censor.releases.javdb import JavDbSource
    from censor.releases.models import ReleaseQuery
    from censor.releases.supjav import SupjavSource
    parser = JavDbSource()
    candidate = parser._parse_detail("<strong>番號:</strong>ABC-123</div>",
        {"href": "/v/demo", "number": "ABC-123", "title": "ABC-123 Uncensored"},
        ReleaseQuery("ABC-123"))
    assert candidate.uncensored and candidate.uncensored_confidence < .8
    assert not parser._judge_uncensored("ABC-123", "", (), "Not Uncensored")[0]
    assert not SupjavSource._judge("Not Uncensored ABC-123")[0]


def test_returned_javbus_page_must_match_query():
    from pathlib import Path
    from censor.releases.javbus import JavBusSource
    from censor.releases.models import ReleaseQuery
    html = (Path(__file__).parent / "fixtures/javbus_detail_piyo170.html").read_text(encoding="utf-8")
    class Transport:
        def get(self, *args, **kwargs):
            return html
    parser = JavBusSource(Transport())
    assert parser.search(ReleaseQuery("ABC-123")) == []


@pytest.mark.parametrize("title", ["XNCRA-026", "NCRA-0261", "NCRA-026A"])
def test_supjav_number_boundaries(title):
    from censor.releases.supjav import SupjavSource
    assert not SupjavSource._number_matches(title, title, "NCRA026")


def test_no_default_download_from_censored_candidates():
    from censor.releases.models import MagnetLink, ReleaseReport
    candidate = ReleaseCandidate("fake", number="ABC-123", uncensored=False,
                                 magnets=(MagnetLink("magnet:?xt=a", size_mb=9000),))
    assert ReleaseReport(candidates=(candidate,), availability=Availability.NONE).best is None


def test_mixed_magnets_prefer_explicit_matching_version():
    from censor.releases.models import MagnetLink
    candidate = ReleaseCandidate("fake", number="ABC-123", uncensored=True, magnets=(
        MagnetLink("magnet:?xt=original", name="ABC-123", size_mb=9000),
        MagnetLink("magnet:?xt=uncensored", name="ABC-123 Uncensored", size_mb=4000)))
    assert candidate.best_magnet.uri == "magnet:?xt=uncensored"
