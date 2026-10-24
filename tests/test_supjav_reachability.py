"""Supjav 抓不到信息的四个根因，逐个钉住。

线上症状："我在 supjav 上有找到无码版，但软件里什么都抓不到"。
排查出来的链条是四段，每段单独都会让结果变成空：

1. **代理写法**：``127.0.0.1:7897`` 这种最自然的填法如果不补 ``http://``，
   urllib 会静默直连 → 连接超时 → 看起来"站点抓不到任何东西"。
2. **代理探测**：Clash 常常只开端口、不开系统代理。只读"系统代理"就会
   判定为无代理 → 直连拉黑。
3. **浏览器通道**：Supjav 站内 403，必须走有头浏览器；但以前要用户
   手动去设置页点"连接会话"，没点就等于整个源失效。
4. **页面完成判定**：Cloudflare 会给 URL 追加 ``__cf_chl_*`` 参数，
   严格比较 URL 会导致永远等不到"完成"，最后超时。

这组用例全部离线，不发网络请求。
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.releases import transport as tp  # noqa: E402
from censor.releases.supjav_pages import detail_metadata, search_links  # noqa: E402


# ── 1. 代理写法 ────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("127.0.0.1:7897", "http://127.0.0.1:7897"),
    ("http://127.0.0.1:7897", "http://127.0.0.1:7897"),
    ("http://127.0.0.1:7897/", "http://127.0.0.1:7897"),
    ("socks5://127.0.0.1:7891", "socks5://127.0.0.1:7891"),
    ("http=127.0.0.1:7890;https=127.0.0.1:7891", "http://127.0.0.1:7891"),
    ("", ""),
    ("   ", ""),
])
def test_normalize_proxy(raw, expected):
    assert tp.normalize_proxy(raw) == expected


def test_detect_proxy_normalizes_explicit_value(monkeypatch):
    """显式配置没写 scheme 时必须补上，否则等于没配。"""
    monkeypatch.setenv(tp.PROBE_ENV, "0")
    assert tp.detect_proxy("127.0.0.1:7897") == "http://127.0.0.1:7897"


def test_detect_proxy_normalizes_env_value(monkeypatch):
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "127.0.0.1:8888")
    assert tp.detect_proxy("") == "http://127.0.0.1:8888"


# ── 2. 本地端口探测 ────────────────────────────────────────

def test_probe_finds_listening_port(monkeypatch):
    """没有声明任何代理时，探测到有人在本机端口上听就当代理用。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        monkeypatch.setattr(tp, "LOCAL_PROXY_PORTS", (port,))
        monkeypatch.setattr(tp, "_probe_state", {"at": 0.0, "value": None, "loaded": False})
        monkeypatch.delenv(tp.PROBE_ENV, raising=False)
        assert tp.probe_local_proxy(force=True) == f"http://127.0.0.1:{port}"
    finally:
        server.close()


def test_probe_respects_off_switch(monkeypatch):
    monkeypatch.setenv(tp.PROBE_ENV, "0")
    monkeypatch.setattr(tp, "_probe_state", {"at": 0.0, "value": None, "loaded": False})
    assert tp.probe_local_proxy(force=True) == ""


def test_probe_caches_result(monkeypatch):
    """探测有缓存：不能每次检索都重新扫一遍端口。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    monkeypatch.setattr(tp, "LOCAL_PROXY_PORTS", (port,))
    monkeypatch.setattr(tp, "_probe_state", {"at": 0.0, "value": None, "loaded": False})
    monkeypatch.delenv(tp.PROBE_ENV, raising=False)
    try:
        first = tp.probe_local_proxy(force=True)
        assert first == f"http://127.0.0.1:{port}"
        # 关掉端口后仍返回缓存值（第二次不再扫）
        server.close()
        assert tp.probe_local_proxy() == first
    finally:
        try:
            server.close()
        except OSError:
            pass


def test_fallback_routes_include_probed_proxy(monkeypatch):
    """环境变量里的代理未必能转发所有站点（实测有 502），探测到的端口要进候选。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setattr(tp, "probe_local_proxy", lambda **_: "http://127.0.0.1:7897")
    routes = tp._fallback_routes("")
    assert "http://127.0.0.1:7897" in routes
    assert routes[-1] == "", "直连永远是最后兜底"


# ── 3. 页面完成判定（Cloudflare 追加参数）─────────────────

def test_same_target_ignores_challenge_params():
    from ui.supjav_browser import _same_target

    plain = "https://supjav.com/?s=YMDD-365"
    challenged = ("https://supjav.com/?s=YMDD-365&__cf_chl_rt_tk="
                  "4cOPT0SuHQBDqPL.O9tgJMuABoifYCRwrggZfIqzKO8-1.0.1.1-ZKPj")
    assert _same_target(challenged, plain), "挑战参数不能算作另一个页面"
    assert _same_target(plain, plain)
    assert not _same_target("https://supjav.com/?s=OTHER-1", plain)
    assert _same_target("https://supjav.com/256514.html?ref=x",
                        "https://supjav.com/256514.html")
    assert not _same_target("https://supjav.com/254427.html",
                            "https://supjav.com/256514.html")


def test_metadata_url_accepts_challenge_prefixed_search():
    from ui.supjav_browser import metadata_url

    assert metadata_url("https://supjav.com/?s=YMDD-365")
    assert metadata_url("https://supjav.com/?__cf_chl_tk=abc&s=YMDD-365")
    assert metadata_url("https://supjav.com/256514.html")
    assert not metadata_url("https://supjav.com/category/reducing-mosaic")


# ── 4. 真实站点页面（2026-10-24 抓取）────────────────────

def _fixture(name: str) -> str:
    return (ROOT / "tests" / "fixtures" / name).read_text(encoding="utf-8")


def test_real_search_page_yields_both_versions():
    """YMDD-365 在站上有两条：带 [Reducing Mosaic] 的与普通版。"""
    links, has_next = search_links(_fixture("supjav_search_real.html"), "YMDD-365")
    assert len(links) == 2
    assert has_next is False
    titles = " ".join(links.values())
    assert "Reducing Mosaic" in titles
    assert "https://supjav.com/256514.html" in links


def test_real_detail_page_reads_reducing_mosaic_category():
    title, categories = detail_metadata(_fixture("supjav_detail_real.html"), "YMDD-365")
    assert "YMDD-365" in title
    paths = [path for _, path in categories]
    assert "/category/reducing-mosaic" in paths


# ── 5. 页面缓存 ───────────────────────────────────────────

def test_browser_page_cache_roundtrip(qapp, tmp_path, monkeypatch):
    """同一地址第二次读取走缓存，不再过一次 Cloudflare。"""
    from ui import supjav_browser as sb

    monkeypatch.setattr(sb, "DATA_DIR", tmp_path, raising=False)
    session = sb.SupjavBrowserSession()
    try:
        url = "https://supjav.com/?s=YMDD-365"
        session._cache_put(url, "<html><body>cached</body></html>")
        assert "cached" in (session._cache_get(url) or "")
        # 过期后不再命中
        path = session._cache_file(url)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["at"] = time.time() - 10 * 86400
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert session._cache_get(url) is None
    finally:
        session.dispose()
