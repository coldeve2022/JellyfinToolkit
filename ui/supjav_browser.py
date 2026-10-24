"""A dedicated metadata-only WebEngine session; no exported browser credentials."""
from dataclasses import dataclass, field
from threading import Event, Lock
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QObject, Qt, Signal, Slot, QTimer, QUrl, QThread
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QMessageBox

from censor.releases.supjav_pages import article_url
from censor.releases.transport import (TransportError, TransportCancelled,
                                       register_site_reader, offline, HttpTransport)


def metadata_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('supjav.com', 'www.supjav.com') or parsed.username or parsed.port not in (None, 443):
        return False
    query = parse_qs(parsed.query)
    keys = {key for key in query if not key.startswith('__cf_chl_')}
    return bool((article_url(url) and not keys) or (parsed.path in ('/', '/zh/', '/ja/', '/en/')
                                    and keys == {'s'}))


def _debug(message: str) -> None:
    """排查用日志：设 ``SUPJAV_DEBUG=1`` 才输出到 stderr。"""
    if os.environ.get("SUPJAV_DEBUG"):
        print(message, file=sys.stderr, flush=True)


def _target_key(url: str):
    """比较"是不是同一个目标"，忽略 Cloudflare 追加的 __cf_chl_* 与参数顺序。"""
    parsed = urlsplit(url)
    query = {
        key: tuple(sorted(value))
        for key, value in parse_qs(parsed.query).items()
        if not key.startswith('__cf_chl_')
    }
    return (article_url(url) or parsed.path.rstrip('/'), tuple(sorted(query.items())))


def _same_target(effective: str, requested: str) -> bool:
    if article_url(effective) or article_url(requested):
        return article_url(effective) == article_url(requested)
    return _target_key(effective) == _target_key(requested)


@dataclass
class ReadRequest:
    url: str
    deadline: float
    done: Event = field(default_factory=Event)
    html: str = ''
    error: str = ''


class SupjavBrowserSession(QObject):
    requested = Signal(object)
    cancelled = Signal(object)

    def __init__(self):
        super().__init__(QApplication.instance())
        self.window = QDialog()
        self.window.setWindowTitle('Supjav 文字元数据会话')
        self.window.resize(900, 650)
        layout = QVBoxLayout(self.window)
        self.status = QLabel('独立浏览器会话；只读取文字元数据，图片、播放器、下载与新窗口已阻止。\n'
                            '出现 Cloudflare 时请手动完成验证；会话仅在本地保存。浏览器使用系统网络设置。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._layout, self._page, self._profile, self._view = layout, None, None, None
        self._current = None
        self._lock = Lock()
        self._disposed = False
        self._registered = False
        self.requested.connect(self._begin)
        self.cancelled.connect(self._cancel)
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._poll)
        QApplication.instance().aboutToQuit.connect(self.dispose)

    def _ensure_visible(self) -> None:
        """让窗口真的显示出来，但不抢焦点。

        为什么必须显示：Chromium 对不可见窗口会节流定时器与 rAF，
        Cloudflare 的挑战脚本靠这些跑完；窗口藏着的时候挑战永远过不去，
        表现就是"浏览器会话开了，但一直读不到页面"。
        """
        if self._disposed:
            return
        self._ensure_page()
        try:
            self.window.setAttribute(Qt.WA_ShowWithoutActivating, True)
            if not self.window.isVisible():
                self.window.show()
        except RuntimeError:
            pass

    def _ensure_page(self):
        if self._page is not None:
            return
        from PySide6.QtWebEngineCore import (QWebEngineProfile, QWebEnginePage,
            QWebEngineSettings, QWebEngineUrlRequestInterceptor, QWebEngineUrlRequestInfo)
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from config import DATA_DIR

        class Guard(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                url = info.requestUrl()
                host = url.host()
                kind = info.resourceType()
                types = QWebEngineUrlRequestInfo.ResourceType
                challenge = host == 'challenges.cloudflare.com'
                reason = ''
                if host not in ('supjav.com', 'www.supjav.com') and not challenge:
                    reason = '外域'
                elif kind in (types.ResourceTypeMedia, types.ResourceTypeImage) and not challenge:
                    reason = '媒体/图片'
                elif kind == types.ResourceTypeMainFrame and not metadata_url(url.toString()):
                    reason = '非元数据主框架'
                elif kind == types.ResourceTypeSubFrame and not challenge:
                    reason = '子框架'
                _debug(f"[guard] {kind} block={bool(reason)} {reason} {url.toString()[:90]}")
                if reason:
                    info.block(True)

        class Page(QWebEnginePage):
            def createWindow(self, kind):
                return None

            def acceptNavigationRequest(self, url, kind, is_main):
                allowed = (metadata_url(url.toString()) if is_main
                           else url.host() == 'challenges.cloudflare.com')
                _debug(f"[accept] main={is_main} allowed={allowed} url={url.toString()!r}")
                return allowed

        # 存储路径必须是纯 ASCII：QtWebEngine 的磁盘缓存/LevelDB 在非 ASCII 路径下
        # 可能初始化失败，表现为 loadFinished=False、URL 一直是空 —— 看起来像
        # "浏览器开了但什么都没加载"。程序数据目录可能就在中文路径里。
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or str(DATA_DIR)
        root = Path(base) / 'JellyfinToolkit' / 'supjav-browser'
        root.mkdir(parents=True, exist_ok=True)
        # 用默认 profile：命名 profile + 自定义 storage 在本机实测会导致
        # loadFinished=False、URL 始终为空（Chromium 的存储初始化失败），
        # 而默认 profile 持久化在 LOCALAPPDATA，路径是 ASCII，稳定。
        profile = QWebEngineProfile.defaultProfile()
        self._guard = Guard(profile)
        profile.setUrlRequestInterceptor(self._guard)
        profile.downloadRequested.connect(lambda download: download.cancel())
        page = Page(profile, self)
        page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True)
        page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        view = QWebEngineView(self.window)
        view.setPage(page)
        self._layout.addWidget(view, 1)
        self._profile, self._page, self._view = profile, page, view
        page.loadFinished.connect(self._loaded)

    def open(self):
        if offline():
            raise TransportError('离线模式下不能建立站点会话')
        self._ensure_page()
        register_site_reader('supjav.com', self.read)
        self._registered = True
        self.window.show()
        self.window.raise_()
        if self._current is None:
            self._page.load(QUrl('https://supjav.com/?s=SYNTHETIC-NOMATCH-748921'))

    # ── 页面缓存：同一番号二次检索不再过一次 Cloudflare ──
    def _cache_file(self, url):
        import hashlib
        from pathlib import Path
        from config import DATA_DIR

        folder = Path(DATA_DIR) / 'supjav-browser' / 'pages'
        return folder / (hashlib.sha1(url.encode('utf-8')).hexdigest() + '.json')

    def _cache_ttl(self) -> float:
        try:
            from config import ToolkitConfig

            days = float(getattr(ToolkitConfig.load(), 'release_cache_days', 7) or 7)
        except Exception:  # noqa: BLE001
            days = 7.0
        return max(0.0, days) * 86400.0

    def _cache_get(self, url):
        ttl = self._cache_ttl()
        if ttl <= 0:
            return None
        try:
            payload = json.loads(self._cache_file(url).read_text(encoding='utf-8'))
            if time.time() - float(payload['at']) > ttl:
                return None
            html = str(payload.get('html') or '')
            return html if html.strip() else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cache_put(self, url, html) -> None:
        if not html or not html.strip():
            return
        try:
            path = self._cache_file(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'url': url, 'at': time.time(), 'html': html}),
                            encoding='utf-8')
        except OSError:
            pass

    def read(self, url, *, timeout=45., should_stop=None):
        if not metadata_url(url):
            raise TransportError('只允许 Supjav 的搜索与文章元数据地址')
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        if self._disposed:
            # 快速失败：否则后台线程要先把锁等满 timeout 才知道会话没了
            raise TransportError('浏览器会话已关闭，请重新连接 Supjav 会话')
        if QThread.currentThread() == self.thread():
            raise TransportError('浏览器读取必须从后台任务发起')
        stop = should_stop or (lambda: False)
        request = ReadRequest(url, time.monotonic() + timeout)
        while not self._lock.acquire(timeout=.05):
            if stop() or time.monotonic() >= request.deadline:
                raise TransportCancelled('浏览器等待已取消') if stop() else TransportError('浏览器会话等待超时')
        try:
            if self._disposed:
                raise TransportError('浏览器会话已关闭，请重新连接')
            self.requested.emit(request)
            while not request.done.wait(.05):
                if stop() or time.monotonic() >= request.deadline:
                    request.done.set()
                    self.cancelled.emit(request)
                    if stop():
                        raise TransportCancelled('Supjav 浏览器检索已取消')
                    raise TransportError('Supjav 浏览器读取超时；请打开会话窗口检查验证或网络')
            if request.error:
                raise TransportError(request.error)
            self._cache_put(url, request.html)
            return request.html
        finally:
            self._lock.release()

    @Slot(object)
    def _begin(self, request):
        if request.done.is_set() or self._disposed:
            return
        # 这里是 GUI 线程，能安全地显示窗口
        self._ensure_visible()
        self._current = request
        try:
            self._navigate(request)
        except Exception as exc:
            self._complete(request, error=f'浏览器初始化失败：{exc}')

    def _navigate(self, request):
        self._ensure_page()
        _debug(f"[nav] load {request.url}")
        self._page.load(QUrl(request.url))
        self._timer.start()

    @Slot(bool)
    def _loaded(self, ok):
        if not ok and not self._page.url().toString():
            # URL 为空说明连导航都没起来：多半是 Chromium 沙箱在本机起不来
            # （受限账户、安全软件）。给出可执行的下一步，而不是干等 45 秒。
            self.status.setText(
                '浏览器内核没有启动（页面未加载）。若反复出现，可尝试在启动脚本里设置 '
                'JELLYFIN_TOOLKIT_WEBENGINE_NO_SANDBOX=1 后重启程序。')
        _debug(f"[nav] loadFinished ok={ok} url={self._page.url().toString()!r}")
        if self._current is not None:
            self._poll()
        elif ok:
            self.status.setText('页面已加载；如果出现挑战，请手动完成。检索会复用此独立会话。')

    def _poll(self):
        request = self._current
        if request is None or request.done.is_set():
            self._timer.stop()
            return
        if time.monotonic() >= request.deadline:
            self._complete(request, error='浏览器页面读取超时')
            return
        self._page.toHtml(lambda html, req=request: self._captured(req, html))

    def _captured(self, request, html):
        if self._current is not request or request.done.is_set():
            return
        effective = self._page.url().toString()
        if not effective or effective == 'about:blank':
            return
        if urlsplit(effective).scheme == 'chrome-error':
            self._complete(request, error='浏览器网络加载失败，请检查系统代理与网络')
            return
        if not metadata_url(effective):
            return
        if not _same_target(effective, request.url):
            return  # Ignore callbacks from the previous page while navigation is pending.
        if HttpTransport._looks_blocked(html.encode('utf-8')):
            self.status.setText('Supjav 仍要求验证，请在此窗口手动完成，然后检索会继续。')
        elif '<body' in html.lower() and len(html) > 120:
            self._complete(request, html)

    def _complete(self, request, html='', *, error=''):
        if self._current is request:
            self._timer.stop()
            self._current = None
        request.html, request.error = html, error
        request.done.set()

    @Slot(object)
    def _cancel(self, request):
        if self._current is request:
            self._timer.stop()
            self._current = None
            if self._page is not None:
                try:
                    self._page.triggerAction(self._page.WebAction.Stop)
                except RuntimeError:
                    pass  # 页面已随窗口销毁，取消本身已经生效

    def dispose(self):
        self._disposed = True
        self._timer.stop()
        if self._registered:
            register_site_reader('supjav.com', None)
            self._registered = False
        if self._current:
            self._complete(self._current, error='浏览器会话已关闭')
        self.window.close()
        if self._view is not None:
            self._layout.removeWidget(self._view)
            self._view.deleteLater()
            self._view = None
        if self._page is not None:
            self._page.deleteLater()
            self._page = None
        if self._profile is not None:
            self._profile.deleteLater()
            self._profile = None


_session = None


def prepare_supjav_session():
    """Register a lazy reader; Chromium starts only when Supjav is requested."""
    global _session
    if _session is None or _session._disposed:
        _session = SupjavBrowserSession()
    register_site_reader('supjav.com', _session.read)
    _session._registered = True
    return _session


def open_supjav_session(parent=None):
    try:
        session = prepare_supjav_session()
        session.open()
    except (ImportError, TransportError) as exc:
        QMessageBox.warning(parent, 'Supjav 浏览器会话不可用', str(exc))
    return _session
