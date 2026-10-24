"""Supjav 抓取链路诊断：把每一层真实响应打印出来，不猜。

用法：
    python scripts/diag_supjav.py                 # 默认番号 YMDD-365
    python scripts/diag_supjav.py --number ABC-123
    python scripts/diag_supjav.py --raw-only      # 只发裸请求，不跑 Source

它只读，不写数据库、不改配置。
"""

from __future__ import annotations

import argparse
import http.client
import re
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.console import force_utf8_stdout  # noqa: E402

force_utf8_stdout()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def raw_get(url, *, timeout=25.0):
    """自己发一次 HTTPS GET，不经过项目 transport，便于单独判断网络层。"""
    parsed = urlsplit(url)
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                       timeout=timeout, context=ctx)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    started = time.monotonic()
    try:
        conn.request("GET", path, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Cache-Control": "no-cache",
        })
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body, time.monotonic() - started
    finally:
        conn.close()


def describe(body):
    text = body.decode("utf-8", "replace")
    low = text.lower()
    flags = []
    if "just a moment" in low or "challenges.cloudflare.com" in low:
        flags.append("Cloudflare 挑战页")
    if "cf-chl" in low:
        flags.append("cf-chl")
    if "search-no-results" in low:
        flags.append("search-no-results")
    if "<article" in low:
        flags.append("有 <article>")
    if "post-meta" in low or "entry-meta" in low:
        flags.append("有 post-meta/entry-meta")
    return text, flags


def summarize_links(text, number):
    """页面里所有 a[href] 的形态统计 —— 判断 article_url 的正则是否还对得上。"""
    hrefs = re.findall(r'<a[^>]+href="([^"]+)"', text, re.I)
    html_like = [h for h in hrefs if re.search(r"\.html(\?|$)", h)]
    numeric = [h for h in html_like if re.fullmatch(r"(?:https?://[^/]+)?/(?:[a-z]{2}/)?\d+\.html/?", h)]
    with_number = [h for h in hrefs if number.lower() in h.lower()]
    return {
        "a[href] 总数": len(hrefs),
        "以 .html 结尾": len(html_like),
        "纯数字.html（当前正则接受）": len(numeric),
        "URL 里含番号": len(with_number),
        "示例": html_like[:6],
        "含番号示例": with_number[:6],
    }


def probe(url, number, limit=2400):
    print(f"\n── GET {url}")
    try:
        status, headers, body, cost = raw_get(url)
    except (socket.timeout, ssl.SSLError, OSError, http.client.HTTPException) as exc:
        print(f"   网络层失败：{type(exc).__name__}: {exc}")
        return None
    print(f"   HTTP {status} · {len(body)} 字节 · {cost:.2f}s · "
          f"server={headers.get('Server', '?')} · cf-ray={headers.get('CF-Ray', '-')}")
    text, flags = describe(body)
    print(f"   特征：{', '.join(flags) or '无'}")
    info = summarize_links(text, number)
    for key, value in info.items():
        print(f"   {key}: {value}")
    if not flags:
        print(f"   正文前 {limit} 字：\n{text[:limit]}\n")
    return text


def probe_source(number):
    """用项目自己的 SupjavSource 跑三种模式，看它自报的失败原因。"""
    from censor.releases.base import ReleaseQuery
    from censor.releases.supjav import SupjavSource
    from censor.releases.transport import HttpTransport, TransportConfig
    from config import ToolkitConfig

    cfg = ToolkitConfig.load()
    query = ReleaseQuery(number=number)
    for mode in ("direct", "auto", "index"):
        print(f"\n── SupjavSource mode={mode}")
        cfg_dict = {"release_supjav_mode": mode,
                    "release_supjav_engines": list(cfg.release_supjav_engines)}
        transport = HttpTransport(TransportConfig(
            proxy=cfg.release_proxy or None,
            timeout=float(cfg.release_timeout),
            retries=int(cfg.release_retries),
            min_interval=float(cfg.release_min_interval)))
        source = SupjavSource(transport, cfg_dict)
        try:
            found = source.search(query, limit=6, log=lambda m: print(f"   log: {m}"))
            print(f"   结果 {len(found)} 条")
            for item in found:
                print(f"     - {item.title[:80]} | url={item.page_url} | "
                      f"uncensored={item.uncensored} conf={item.uncensored_confidence}")
        except Exception as exc:  # noqa: BLE001 - 诊断脚本要打印所有失败
            print(f"   抛错 {type(exc).__name__}: {exc}")
        print(f"   status_detail: {source.status_detail}")
        print(f"   incomplete_reason: {source.incomplete_reason or '(空)'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--number", default="YMDD-365")
    parser.add_argument("--raw-only", action="store_true")
    args = parser.parse_args()
    number = args.number
    keyword = quote(number)

    print(f"诊断番号：{number}")
    print(f"Python: {sys.version.split()[0]} · 线程/网络：直连（不走项目 transport）")

    urls = [
        f"https://supjav.com/?s={keyword}",
        f"https://supjav.com/zh/?s={keyword}",
        f"https://supjav.com/ja/?s={keyword}",
        "https://supjav.com/",
    ]
    for url in urls:
        probe(url, number)

    if not args.raw_only:
        try:
            probe_source(number)
        except Exception as exc:  # noqa: BLE001
            print(f"\nSupjavSource 诊断本身失败：{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
