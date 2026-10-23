from urllib.parse import urlsplit

import pytest

from censor.releases.models import ReleaseQuery
from censor.releases.supjav import SupjavSource
from censor.releases.transport import TransportBlocked, TransportError


def search_page():
    return '''<html><body class="search search-results"><main>
    <article><h2><a href="https://supjav.com/zh/100.html">DEMO-123 站内候选</a></h2></article>
    <article><h2><a href="https://supjav.com/ja/100.html">DEMO-123 同文章</a></h2></article>
    <article><h2><a href="https://supjav.com/101.html">XDEMO-123 不同作品</a></h2></article>
    </main></body></html>'''


def detail_page(title='【无码破解】DEMO-123 合成条目', category='无码破解'):
    return f'''<html><body class="single-post"><main><article>
    <h1 class="entry-title">{title}</h1><div class="entry-meta">
    <a rel="category tag" href="https://supjav.com/zh/category/reducing-mosaic">{category}</a>
    </div></article></main><aside><h2>OTHER-999 Uncensored</h2></aside></body></html>'''


class Transport:
    def __init__(self, detail=None, blocked=False):
        self.calls = []
        self.detail = detail if detail is not None else detail_page()
        self.blocked = blocked
    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.blocked:
            raise TransportBlocked('synthetic challenge')
        return search_page() if '?s=' in url else self.detail


def test_direct_search_reads_matching_detail_without_using_index(monkeypatch):
    transport = Transport()
    monkeypatch.setattr('censor.releases.supjav.lookup_site', lambda *a, **kw:
        pytest.fail('successful direct lookup must not depend on an index'))
    source = SupjavSource(transport, {'release_supjav_mode': 'direct'})
    found = source.search(ReleaseQuery(number='DEMO-123'))
    assert len(found) == 1 and found[0].uncensored_confidence >= .8
    assert found[0].number == 'DEMO-123'
    assert len(transport.calls) == 2
    assert all(urlsplit(url).hostname == 'supjav.com' for url in transport.calls)
    assert '站内' in found[0].uncensored_reason


def test_wrong_detail_identifier_is_not_accepted():
    source = SupjavSource(Transport(detail_page('OTHER-999 Uncensored')), {'release_supjav_mode': 'direct'})
    with pytest.raises(TransportError):
        source.search(ReleaseQuery(number='DEMO-123'))


def test_sidebar_category_does_not_turn_plain_primary_post_uncensored():
    page = detail_page('DEMO-123 合成普通条目', '普通作品').replace('</aside>',
        '<a rel="category tag" href="/category/uncensored">Uncensored</a></aside>')
    found = SupjavSource(Transport(page), {'release_supjav_mode': 'direct'}).search(ReleaseQuery(number='DEMO-123'))
    assert len(found) == 1 and not found[0].uncensored


def test_real_theme_navigation_is_not_post_metadata():
    page = '''<html><body class="single-post"><header><a href="/category/uncensored">Uncensored</a></header>
    <div class="main"><div class="video-wrap"><div class="archive-title"><h1>DEMO-123 合成普通条目</h1></div></div>
    <div class="content"><div class="post-meta clearfix"><div class="cats"><p class="cat">
    <a href="/category/censored">Censored</a></p></div></div></div></div></body></html>'''
    found = SupjavSource(Transport(page), {'release_supjav_mode': 'direct'}).search(ReleaseQuery(number='DEMO-123'))
    assert found and not found[0].uncensored
    assert found[0].categories == ('Censored',)


def test_direct_challenge_is_never_reported_as_empty_success():
    with pytest.raises(TransportError):
        SupjavSource(Transport(blocked=True), {'release_supjav_mode': 'direct'}).search(ReleaseQuery(number='DEMO-123'))


def test_auto_mode_keeps_unverified_index_as_partial_soft_lead(monkeypatch):
    from censor.releases.index_search import IndexHit
    monkeypatch.setattr('censor.releases.supjav.lookup_site', lambda *a, **kw:
        ([IndexHit('https://supjav.com/100.html', 'DEMO-123 Uncensored')], {'bing': '命中 1 条'}))
    source = SupjavSource(Transport(blocked=True), {'release_supjav_mode': 'auto'})
    found = source.search(ReleaseQuery(number='DEMO-123'))
    assert found and found[0].uncensored_confidence < .8
    assert source.incomplete_reason and '站内' in source.incomplete_reason


def test_valid_direct_no_results_does_not_need_index(monkeypatch):
    class Empty(Transport):
        def get(self, url, **kw):
            return '<html><body class="search search-no-results"><main><h1>Nothing Found</h1></main></body></html>'
    monkeypatch.setattr('censor.releases.supjav.lookup_site', lambda *a, **kw: pytest.fail('no fallback needed'))
    source = SupjavSource(Empty(), {'release_supjav_mode': 'direct'})
    assert source.search(ReleaseQuery(number='DEMO-123')) == []
    assert not source.incomplete_reason


def test_empty_or_unknown_markup_is_a_parse_failure():
    class Empty(Transport):
        def get(self, url, **kw):
            return '<html><body>upstream error</body></html>'
    with pytest.raises(TransportError):
        SupjavSource(Empty(), {'release_supjav_mode': 'direct'}).search(ReleaseQuery(number='DEMO-123'))


def test_cancel_does_not_start_any_site_or_index_request(monkeypatch):
    transport = Transport()
    monkeypatch.setattr('censor.releases.supjav.lookup_site', lambda *a, **kw: pytest.fail('cancelled'))
    source = SupjavSource(transport, {'release_supjav_mode': 'direct'})
    source.search(ReleaseQuery(number='DEMO-123'), should_stop=lambda: True)
    assert transport.calls == []
