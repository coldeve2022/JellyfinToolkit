"""Supjav 检索源 —— 破解版（Decensored / Leak）最集中的地方，但它整站挂在 Cloudflare 后面。

为什么不直接抓
--------------
直连 ``supjav.com`` 一律 403「Just a moment...」，配了 FlareSolverr 也未必能过挑战
（本机实测 90 秒超时）。而我们要回答的问题其实很窄：**这部作品在这个站上有没有
无码/破解条目**。搜索引擎的爬虫早就抓过这些页面，索引里就留着标题和链接。

所以这里走 :mod:`censor.releases.index_search`：用 ``site:supjav.com <番号>``
在 Bing / DuckDuckGo 的索引里查。实测直接命中：

    https://supjav.com/139614.html
    【無修正流出】NCRA-026 Pure Moe Mix/土屋あさみ - SupJav

好处不只是"能查"：给出的 URL 用户点开就能自己核对，比抓一页 HTML 更可信，
也不需要浏览器、不需要旁路服务、不会被计入目标站的访问量。

判定
----
标题里出现 ``無修正流出`` / ``无码流出`` / ``Uncensored Leak`` / ``Decensored`` /
``破解`` 等站点自己的标记 → 判为无码条目（0.85）。
没有这些标记就**不算** —— Supjav 上同样有大量有码片，不能因为"站名叫 Supjav"就默认无码。
"""

from __future__ import annotations

import re

from censor.numbering import parse_number
from censor.releases.base import ReleaseSource
from censor.releases.index_search import lookup_site
from censor.releases.models import ReleaseCandidate, ReleaseQuery, normalize_number

__all__ = ["SupjavSource", "UNCENSORED_HINTS", "WEAK_HINTS", "DEFAULT_ENGINES"]

#: **硬标记**：站点自己声明这是无码/流出/破解版。命中即"确有"（0.85）。
UNCENSORED_HINTS = (
    "無修正流出", "无码流出", "無碼流出", "無修正", "无修正",
    "uncensored leak", "uncensored", "decensored", "leaked", "破解", "流出",
)

#: **软标记**：只说明"码被处理过"，不等于全无码。命中记"疑似"（0.60）。
#: 典型例子：``[Reducing Mosaic]PIYO-170 ...`` —— 减码/薄码版，仍可能有残余马赛克。
WEAK_HINTS = (
    "reducing mosaic", "reduced mosaic", "mosaic reduction",
    "薄码", "薄碼", "減馬", "减码", "减馬", "轻微马赛克",
)

#: 软标记对应的置信度：够进"疑似"，不够进"确有"
WEAK_CONFIDENCE = 0.60

#: 默认先用 Bing（限流宽松、能直出链接），DDG 作后备（结果更干净但容易限流）
DEFAULT_ENGINES = ("bing", "duckduckgo")


class SupjavSource(ReleaseSource):
    name = "supjav"
    title = "Supjav（破解版）"
    homepage = "https://supjav.com"
    capabilities = ("http", "index")
    version = "2"

    #: 本源负责的站点域名
    site = "supjav.com"

    def __init__(self, transport=None, cfg=None) -> None:
        super().__init__(transport, cfg)
        #: 上一次检索里各引擎的状态（给"测试连接"和排查用）
        self.status_detail: dict[str, str] = {}

    def search(self, query: ReleaseQuery, *, should_stop=lambda: False,
               log=lambda _m: None, limit: int = 8) -> list[ReleaseCandidate]:
        if not query.usable:
            return []
        keyword = query.number or query.title_hint
        hits, statuses = lookup_site(
            self.transport, self.site, keyword,
            engines=self._engines(), limit=max(limit, 6) * 2,
            should_stop=should_stop, log=log,
        )
        self.status_detail = statuses
        wanted = normalize_number(query.number)

        out: list[ReleaseCandidate] = []
        for hit in hits:
            number = parse_number(hit.title).number
            if wanted and not self._number_matches(hit.title, number, wanted):
                continue
            uncensored, confidence, reason = self._judge(hit.title)
            out.append(ReleaseCandidate(
                source=self.name,
                number=number or query.number,
                title=hit.title[:200],
                page_url=hit.url,
                uncensored=uncensored,
                uncensored_reason=reason,
                # 站点自己在标题里标了"無修正流出"，还给了可核对的链接 → 算硬依据；
                # 只写"减码"的算软依据
                uncensored_confidence=confidence,
            ))
            if len(out) >= limit:
                break
        log(f"Supjav 索引命中 {len(out)} 条")
        return out

    # ── 内部 ──
    @staticmethod
    def _number_matches(title: str, parsed: str, wanted: str) -> bool:
        """番号必须**成词**出现：``NCRA-026`` 不能命中 ``NCRA-0261``。"""
        if normalize_number(parsed) == wanted:
            return True
        # 归一化后按"后面不能紧跟数字"匹配，避免前缀误命中
        return bool(re.search(re.escape(wanted) + r"(?![0-9])", normalize_number(title)))


    def _engines(self) -> tuple[str, ...]:
        if isinstance(self.cfg, dict):
            configured = self.cfg.get("release_supjav_engines")
        else:
            configured = getattr(self.cfg, "release_supjav_engines", None)
        if isinstance(configured, (list, tuple)) and configured:
            return tuple(str(item) for item in configured)
        return DEFAULT_ENGINES

    @staticmethod
    def _judge(title: str) -> tuple[bool, float, str]:
        """返回 ``(是否无码条目, 依据强度, 依据说明)``。

        分两档是因为 Supjav 上有一类介于中间的条目：``[Reducing Mosaic]``
        这类"减码/薄码"版 —— 码被动过，但不保证已经全无码。把它当"确有"会
        让用户白下一次，当"有码"又会漏掉线索，所以单列成"疑似"。
        """
        lowered = str(title or "").lower()
        strong = [hint for hint in UNCENSORED_HINTS if hint.lower() in lowered]
        if strong:
            return True, 0.85, f"站点标题含无码/破解标记（{strong[0]}）"
        weak = [hint for hint in WEAK_HINTS if hint.lower() in lowered]
        if weak:
            return True, WEAK_CONFIDENCE, f"站点标题含减码/薄码标记（{weak[0]}），未必全无码"
        return False, 0.0, ""
