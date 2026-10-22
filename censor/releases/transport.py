"""HTTP 传输层 —— 只用标准库，代理/限速/重试/Cloudflare 判定都在这里收口。

为什么不用 requests
-------------------
这个项目刻意只依赖 PySide6 + numpy，抓站点这点需求用 ``urllib`` 完全够。
把"怎么发请求"单独抽出来，是为了三件事：

1. **换传输方式不改业务**：直连失败可以换成 Cloudflare 旁路服务
   （FlareSolverr 兼容接口），插件代码一行都不用动；
2. **限速只可能有一处**：同一站点默认 1.5 秒一次，避免把人站点打挂导致封 IP；
3. **失败要能说清楚**：被墙、被 Cloudflare 拦、超时、返回 5xx 是四种不同的事，
   上层报告里必须区分 —— 不能把"没查成"说成"没有无码版"。

代理的取法：配置 → 环境变量 → Windows 系统代理（读注册表），
**任何情况下都不写死 127.0.0.1:7897 这类地址**。
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

__all__ = [
    "TransportError", "TransportBlocked", "TransportNotFound", "HttpTransport",
    "FlareSolverrTransport", "detect_proxy", "build_transport", "offline",
    "OFFLINE_ENV",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 命中这些特征说明拦在 Cloudflare 挑战页，而不是真的返回了内容
_CHALLENGE_MARKERS = (
    "Just a moment...",
    "cf-challenge",
    "challenges.cloudflare.com",
    "Attention Required! | Cloudflare",
)


class TransportError(RuntimeError):
    """可预期的传输失败（超时、连接不上、状态码异常）。"""


class TransportBlocked(TransportError):
    """被 Cloudflare / 反爬拦住了 —— 需要旁路，不是站点没有内容。"""


class TransportNotFound(TransportError):
    """站点明确回答"没有这个东西"（404/410）。**重试没有意义。**"""


#: 设成 1 就彻底禁止联网（CI / 离线环境 / 只想看本地结果时用）
OFFLINE_ENV = "JELLYFIN_TOOLKIT_OFFLINE"


def offline() -> bool:
    return str(os.environ.get(OFFLINE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def detect_proxy(explicit: str = "") -> str:
    """按 配置 → 环境变量 → Windows 系统代理 的顺序取代理地址。

    读系统代理想解决的是"用户开了 Clash 但没配环境变量"这个最常见的场景，
    同时避免在代码里写死任何一个本地端口。
    """
    text = str(explicit or "").strip()
    if text:
        return text
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if os.name != "nt":
        return ""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (OSError, ImportError, ValueError):
        return ""
    server = str(server or "").strip()
    if not server:
        return ""
    # 形如 "127.0.0.1:7897" 或 "http=127.0.0.1:7897;https=...;socks=..."
    if "=" in server:
        parts = dict(
            item.split("=", 1) for item in server.split(";") if "=" in item
        )
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return ""
    if "://" not in server:
        server = "http://" + server
    return server


@dataclass
class TransportConfig:
    """一次会话的传输参数。全部可配置，默认值对站点友好。"""

    proxy: str = ""
    #: 备选线路：主代理不通时依次尝试（含空串 = 直连）
    fallbacks: tuple[str, ...] = ()
    timeout: float = 20.0
    retries: int = 2
    min_interval: float = 1.5      # 同一 host 两次请求的最小间隔（秒）
    max_requests: int = 40         # 单次检索的请求上限，防止翻页翻飞
    user_agent: str = DEFAULT_UA

    @classmethod
    def from_config(cls, cfg) -> "TransportConfig":
        def get(key, default=None):
            if cfg is None:
                return default
            if isinstance(cfg, dict):
                return cfg.get(key, default)
            return getattr(cfg, key, default)

        explicit = str(get("release_proxy", "") or "").strip()
        return cls(
            proxy=detect_proxy(explicit),
            fallbacks=tuple(_fallback_routes(explicit)),
            timeout=float(get("release_timeout", 20.0) or 20.0),
            retries=int(get("release_retries", 2) or 0),
            min_interval=float(get("release_min_interval", 1.5) or 0.0),
            max_requests=int(get("release_max_requests", 40) or 40),
        )


def _fallback_routes(explicit: str) -> list[str]:
    """收集"还能试哪些线路"：显式配置 → 环境变量 → 系统代理 → 直连。

    这里**不猜端口**，只把本机已经声明过的代理都列进去；
    真正的兜底是最后那个空串（直连）。
    """
    routes: list[str] = []

    def push(value: str) -> None:
        value = (value or "").strip()
        if value and value not in routes:
            routes.append(value)

    push(explicit)
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        push(os.environ.get(name, ""))
    push(detect_proxy(""))
    routes.append("")   # 直连兜底
    return routes


class HttpTransport:
    """最普通的直连/走代理的 HTTP 客户端。"""

    def __init__(self, config: TransportConfig | None = None) -> None:
        self.config = config or TransportConfig()
        self._last_hit: dict[str, float] = {}
        self._requests = 0
        self.failures: list[str] = []
        #: 已经验证能通的线路会挪到最前面，避免每次都从坏线路开始重试
        self._routes: list[str] = []
        #: 一旦某条线路成功过就钉住它 —— 否则一次偶发失败会连带去试其它线路，
        #: 而那些线路往往是超时而不是快速失败，白白拖几十秒
        self._pinned = False
        for route in (self.config.proxy, *self.config.fallbacks):
            if route not in self._routes:
                self._routes.append(route)
        if self._routes:
            self.config.proxy = self._routes[0]

    # ── 对外 ──
    @property
    def description(self) -> str:
        if self.config.proxy:
            return f"经代理 {self.config.proxy}"
        return "直连（不走代理）"

    def get(self, url: str, *, referer: str = "", headers: dict | None = None) -> str:
        """取一个文本页面。返回解压后的字符串。"""
        return self.request(url, referer=referer, headers=headers).decode("utf-8", "replace")

    def request(self, url: str, *, referer: str = "", headers: dict | None = None) -> bytes:
        if offline():
            raise TransportError(f"离线模式（{OFFLINE_ENV}=1）：已禁止联网检索")
        if self._requests >= self.config.max_requests:
            raise TransportError(f"单次检索请求数已达上限 {self.config.max_requests}")
        host = urllib.parse.urlsplit(url).netloc
        self._throttle(host)
        self._requests += 1

        last: Exception | None = None
        routes = [self._routes[0]] if self._pinned and self._routes else (list(self._routes) or [""])
        for route in routes:
            self.config.proxy = route
            for attempt in range(self.config.retries + 1):
                try:
                    body = self._once(url, referer, headers)
                except (TransportBlocked, TransportNotFound):
                    raise
                except TransportError as exc:
                    last = exc
                    if attempt < self.config.retries:
                        time.sleep(min(1.5 * (attempt + 1), 5.0))
                    continue
                # 这条线路通了就钉住，下次直接用
                index = self._routes.index(route) if route in self._routes else -1
                if index > 0:
                    self._routes.insert(0, self._routes.pop(index))
                self._pinned = True
                return body
            # 这条线路彻底不通，换下一条
            continue
        assert last is not None
        self.failures.append(f"{host}: {last}")
        raise last

    # ── 内部 ──
    def _throttle(self, host: str) -> None:
        gap = self.config.min_interval
        if gap <= 0:
            return
        last = self._last_hit.get(host, 0.0)
        wait = gap - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _once(self, url: str, referer: str, extra: dict | None = None) -> bytes:
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        request = urllib.request.Request(url, headers=headers)
        handlers = []
        if self.config.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.config.proxy, "https": self.config.proxy}))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=self.config.timeout) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                raise TransportNotFound(f"HTTP {exc.code}") from exc
            if exc.code in (403, 503):
                body = b""
                try:
                    body = exc.read()
                except Exception:  # noqa: BLE001
                    pass
                if self._looks_blocked(body):
                    raise TransportBlocked(
                        f"被 Cloudflare/反爬拦截（HTTP {exc.code}）"
                    ) from exc
            raise TransportError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise TransportError(f"连接失败：{exc}") from exc

        if status >= 400:
            raise TransportError(f"HTTP {status}")
        if encoding == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        elif encoding == "deflate":
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                pass
        if self._looks_blocked(raw):
            raise TransportBlocked("被 Cloudflare/反爬拦截（返回挑战页）")
        return raw

    @staticmethod
    def _looks_blocked(body: bytes) -> bool:
        head = body[:4000].decode("utf-8", "replace")
        return any(marker in head for marker in _CHALLENGE_MARKERS)


class FlareSolverrTransport(HttpTransport):
    """通过 FlareSolverr 兼容服务绕 Cloudflare。

    MDCX 用的就是这套协议（``POST /v1 {"cmd":"request.get","url":...}``），
    所以用户在那边已经配好的旁路地址可以直接填过来复用。
    """

    def __init__(self, endpoint: str, config: TransportConfig | None = None) -> None:
        super().__init__(config)
        self.endpoint = endpoint.rstrip("/")

    @property
    def description(self) -> str:
        return f"Cloudflare 旁路（{self.endpoint}）"

    def _once(self, url: str, referer: str, extra: dict | None = None) -> bytes:  # noqa: ARG002
        payload = json.dumps(
            {"cmd": "request.get", "url": url, "maxTimeout": int(self.config.timeout * 1000)}
        ).encode()
        request = urllib.request.Request(
            self.endpoint + "/v1",
            data=payload,
            headers={"Content-Type": "application/json", **(extra or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout + 10) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TransportError(f"旁路服务不可用：{exc}") from exc
        if str(data.get("status", "")).lower() != "ok":
            raise TransportBlocked(str(data.get("message") or "旁路服务未能通过挑战"))
        solution = data.get("solution") or {}
        body = str(solution.get("response") or "")
        if self._looks_blocked(body.encode("utf-8", "replace")):
            raise TransportBlocked("旁路服务返回的仍是挑战页")
        return body.encode("utf-8", "replace")


def build_transport(cfg) -> HttpTransport:
    """按配置选传输方式：配了旁路地址就走旁路，否则直连。"""
    config = TransportConfig.from_config(cfg)
    endpoint = ""
    if cfg is not None:
        endpoint = str(
            cfg.get("release_cf_bypass_url", "")
            if isinstance(cfg, dict)
            else getattr(cfg, "release_cf_bypass_url", "")
            or ""
        ).strip()
    if endpoint:
        return FlareSolverrTransport(endpoint, config)
    return HttpTransport(config)
