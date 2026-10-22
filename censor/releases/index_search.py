"""站点索引检索 —— 借搜索引擎的索引去"看"那些直接抓不到的站。

为什么需要这一层
----------------
supjav.com 整站挂在 Cloudflare 挑战后面，直连一律 403。但我们真正要回答的
只有一个问题：**这部作品在这个站上有没有无码/破解条目**。这件事搜索引擎早就
替我们做完了 —— 它的爬虫抓过那些页面，索引里就留着标题和链接。

于是换个思路：不突破 Cloudflare，而是**查索引**。
实测（2026-10-22）用 ``site:supjav.com NCRA-026`` 直接就能拿到：

    https://supjav.com/139614.html
    【無修正流出】NCRA-026 Pure Moe Mix/土屋あさみ - SupJav

这比硬闯挑战更稳、更快，也不需要浏览器或任何外部服务。

设计要点
--------
- 一个引擎一个类（``BingEngine`` / ``DuckDuckGoEngine``），失败自动换下一个；
- 引擎返回的是**通用网页结果**，由调用方按域名过滤 —— 所以这一层可以服务
  任何被反爬的站点，而不只是 supjav；
- 限流（429/202）当成"这个引擎这次不行"，不是"没有结果"，必须如实上报。
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from dataclasses import dataclass

from censor.releases.html import clean
from censor.releases.transport import TransportBlocked

__all__ = [
    "IndexHit", "SearchEngine", "BingEngine", "DuckDuckGoEngine",
    "ENGINES", "get_engine", "lookup_site",
]


@dataclass(frozen=True)
class IndexHit:
    """搜索引擎返回的一条结果。"""

    url: str
    title: str = ""
    snippet: str = ""

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc.lower()


class SearchEngine:
    """一个搜索引擎。子类只需实现 :meth:`build_url` 与 :meth:`parse`。"""

    name: str = ""
    title: str = ""
    #: 是否要求走代理（大陆网络下这些站基本都要）
    needs_proxy: bool = True

    def build_url(self, query: str) -> str:
        raise NotImplementedError

    def parse(self, html: str) -> list[IndexHit]:
        raise NotImplementedError

    def search(self, transport, query: str) -> list[IndexHit]:
        return self.parse(transport.get(self.build_url(query)))

    # ── 工具 ──
    @staticmethod
    def _decode_bing(href: str) -> str:
        """Bing 的结果是 ``/ck/a?...&u=a1<base64url>``，把真实地址解回来。"""
        if "bing.com/ck/a" not in href and "/ck/a?" not in href:
            return href
        raw = href.replace("&amp;", "&")
        if raw.startswith("/"):
            raw = "https://www.bing.com" + raw
        value = urllib.parse.parse_qs(urllib.parse.urlsplit(raw).query).get("u", [""])[0]
        if not value.startswith("a1"):
            return ""
        payload = value[2:]
        padding = "=" * (-len(payload) % 4)
        try:
            return base64.urlsafe_b64decode(payload + padding).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""


class BingEngine(SearchEngine):
    """Bing —— 实测能直接给出目标站链接，且限流比 DDG 宽松。"""

    name = "bing"
    title = "Bing"

    def build_url(self, query: str) -> str:
        return ("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
                + "&mkt=en-US&setlang=en-US&count=20")

    def parse(self, html: str) -> list[IndexHit]:
        hits: list[IndexHit] = []
        for block in re.findall(r'<li class="b_algo".*?</li>', html or "", re.S):
            match = re.search(r"<h2[^>]*>.*?<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                              block, re.S)
            if not match:
                continue
            url = self._decode_bing(match.group(1))
            title = clean(match.group(2))
            if not url or not title:
                continue
            snippet = clean(re.search(r"<p[^>]*>(.*?)</p>", block, re.S).group(1)) \
                if re.search(r"<p[^>]*>(.*?)</p>", block, re.S) else ""
            hits.append(IndexHit(url=url, title=title, snippet=snippet))
        return hits


class DuckDuckGoEngine(SearchEngine):
    """DuckDuckGo 的 HTML 端点 —— 结果最干净，但限流很凶（连续几次就 202）。"""

    name = "duckduckgo"
    title = "DuckDuckGo"

    def build_url(self, query: str) -> str:
        return "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)

    def parse(self, html: str) -> list[IndexHit]:
        hits: list[IndexHit] = []
        body = html or ""
        pattern = r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
        matched = re.findall(pattern, body, re.S)
        if not matched and ("anomaly" in body.lower() or "bots use DuckDuckGo" in body):
            # 这是限流/人机校验页，不是"没有结果"。必须区分，
            # 否则报告会把"没查成"写成"该站没有这个番号"。
            raise TransportBlocked("DuckDuckGo 触发限流（要求人机校验）")
        for href, title_html in matched:
            url = href.replace("&amp;", "&")
            # DDG 用自己的跳转：//duckduckgo.com/l/?uddg=<编码后的真实地址>
            if "uddg=" in url:
                value = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("uddg", [""])[0]
                url = urllib.parse.unquote(value)
            if not url.startswith("http"):
                continue
            title = clean(title_html)
            if title:
                hits.append(IndexHit(url=url, title=title))
        return hits


#: 引擎注册表。顺序就是尝试顺序：先快后慢、先稳后不稳。
ENGINES: dict[str, SearchEngine] = {
    engine.name: engine for engine in (BingEngine(), DuckDuckGoEngine())
}


def get_engine(name: str) -> SearchEngine | None:
    return ENGINES.get(str(name or "").strip().lower())


def lookup_site(
    transport,
    site: str,
    keyword: str,
    *,
    engines: list[str] | tuple[str, ...] = ("bing", "duckduckgo"),
    limit: int = 8,
    should_stop=None,
    log=None,
) -> tuple[list[IndexHit], dict[str, str]]:
    """在搜索引擎索引里查"某个站上关于某个关键词的条目"。

    返回 ``(命中的本站条目, 各引擎状态)``。**引擎失败与"没有结果"分开记录** ——
    报告里必须能看出是"索引里没有"还是"这次没查成"。
    """
    site = str(site or "").strip().lower()
    if not site or not str(keyword or "").strip():
        return [], {}
    query = f"site:{site} {keyword}"
    stopped = should_stop or (lambda: False)
    statuses: dict[str, str] = {}
    for name in engines:
        if stopped():
            statuses[name] = "已取消"
            break
        engine = get_engine(name)
        if engine is None:
            statuses[name] = "未注册"
            continue
        try:
            raw = engine.search(transport, query)
        except Exception as exc:  # noqa: BLE001 - 单引擎失败换下一个
            statuses[name] = f"失败：{exc}"
            if log:
                log(f"{engine.title} 索引查询失败：{exc}")
            continue
        matched = [hit for hit in raw if hit.host.endswith(site)]
        if raw and not matched:
            # 实测：Bing 有时会忽略 site: 限定，返回一堆无关结果（例如查 STARS-016
            # 却给出一堆"stars"天文页）。这种情况要说清楚，否则看起来像"本站没有"。
            statuses[name] = f"返回 {len(raw)} 条但都不在本站（可能忽略了 site: 限定）"
        else:
            statuses[name] = f"返回 {len(raw)} 条，其中本站 {len(matched)} 条"
        deduped = _dedupe(matched)
        if deduped:
            return deduped[:limit], statuses
    return [], statuses


def _dedupe(hits: list[IndexHit]) -> list[IndexHit]:
    """同一个条目常有 ja/en/zh 三种语言版本，按路径去重，保留标题信息量最大的。"""
    best: dict[str, IndexHit] = {}
    for hit in hits:
        path = urllib.parse.urlsplit(hit.url).path
        # 去掉语言前缀：/ja/139614.html 与 /139614.html 是同一集
        key = re.sub(r"^/(?:ja|en|zh|ko|de|es|tw)/", "/", path)
        current = best.get(key)
        if current is None or len(hit.title) > len(current.title):
            best[key] = hit
    return list(best.values())
