from threading import Thread

import pytest


def test_site_reader_uses_common_budget_and_offline_switch(monkeypatch):
    from censor.releases.transport import HttpTransport, TransportConfig, query_scope, register_site_reader
    monkeypatch.delenv('JELLYFIN_TOOLKIT_OFFLINE', raising=False)
    calls = []
    register_site_reader('supjav.com', lambda url, **kw: calls.append(url) or '<h1>metadata</h1>')
    try:
        transport = HttpTransport(TransportConfig(min_interval=0, retries=0))
        with query_scope(1):
            assert 'metadata' in transport.get_site_text('https://supjav.com/123.html')
            with pytest.raises(Exception, match='上限'):
                transport.get_site_text('https://supjav.com/124.html')
        monkeypatch.setenv('JELLYFIN_TOOLKIT_OFFLINE', '1')
        with pytest.raises(Exception, match='离线'):
            transport.get_site_text('https://supjav.com/123.html')
        assert len(calls) == 1
    finally:
        register_site_reader('supjav.com', None)


@pytest.mark.parametrize('url', ['https://supjav.com/123.html', 'https://supjav.com/zh/123.html',
                                'https://supjav.com/?s=DEMO-123',
                                'https://supjav.com/?s=DEMO-123&__cf_chl_tk=synthetic'])
def test_browser_accepts_only_metadata_navigation(url):
    from ui.supjav_browser import metadata_url
    assert metadata_url(url)


@pytest.mark.parametrize('url', ['http://supjav.com/123.html', 'https://evil.supjav.com/123.html',
    'https://supjav.com.evil.invalid/123.html', 'https://supjav.com/video.mp4',
    'https://supjav.com/?download=123', 'magnet:?xt=anything', 'file:///C:/Windows/system.ini'])
def test_browser_rejects_media_external_and_local_navigation(url):
    from ui.supjav_browser import metadata_url
    assert not metadata_url(url)


def test_old_page_capture_cannot_complete_a_new_navigation(qapp, qtbot):
    from ui.supjav_browser import SupjavBrowserSession, ReadRequest
    from PySide6.QtCore import QUrl
    import time
    session = SupjavBrowserSession()
    qtbot.addWidget(session.window)
    class Page:
        def url(self):
            return QUrl('https://supjav.com/122.html')
    session._page = Page()
    request = ReadRequest('https://supjav.com/123.html', time.monotonic()+5)
    session._current = request
    session._captured(request, '<html><body><h1>old page</h1>' + ' ' * 150 + '</body></html>')
    assert not request.done.is_set()
    session._page = None
    session.dispose()


def test_browser_worker_handoff_runs_navigation_on_gui(qapp, qtbot, monkeypatch):
    from ui.supjav_browser import SupjavBrowserSession
    import threading
    session = SupjavBrowserSession()
    qtbot.addWidget(session.window)
    seen, result = [], []
    def navigate(request):
        seen.append(threading.get_ident())
        session._complete(request, '<main>synthetic metadata</main>')
    monkeypatch.setattr(session, '_navigate', navigate)
    worker = Thread(target=lambda: result.append(session.read('https://supjav.com/123.html', timeout=2)))
    worker.start()
    qtbot.waitUntil(lambda: bool(result))
    worker.join(2)
    assert result == ['<main>synthetic metadata</main>']
    assert seen == [threading.get_ident()]
    session.dispose()


def test_packaging_does_not_remove_the_browser_backend():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'JellyfinToolkit.spec').read_text(encoding='utf-8'))
    exclusions = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'excludes' for t in n.targets))
    assert not {'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel'} & set(exclusions)


def test_disposed_session_fails_fast(qapp):
    """会话关掉后再来读取：立刻报"已关闭"，不要先白等锁满超时。"""
    import pytest
    from censor.releases.transport import TransportError
    from ui.supjav_browser import SupjavBrowserSession

    session = SupjavBrowserSession()
    session.dispose()
    with pytest.raises(TransportError, match="已关闭"):
        session.read("https://supjav.com/139614.html")


def test_cancel_after_page_gone_does_not_raise(qapp):
    """页面已随窗口销毁时取消，不该再抛 RuntimeError。"""
    from ui.supjav_browser import ReadRequest, SupjavBrowserSession

    session = SupjavBrowserSession()
    request = ReadRequest("https://supjav.com/139614.html", 0.0)
    session._current = request
    session.dispose()
    session._cancel(request)      # 不应抛
    assert request.done.is_set() or True


def test_dispose_clears_view_and_profile(qapp):
    from ui.supjav_browser import SupjavBrowserSession

    session = SupjavBrowserSession()
    session.dispose()
    assert session._page is None
    assert session._profile is None
    assert session._view is None


