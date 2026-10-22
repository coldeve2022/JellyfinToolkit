"""Supjav 检索源 —— 破解版（Decensored / Leak）最集中的地方，但有 Cloudflare。

定位
----
JavDB / JavBus 覆盖的是**官方无码**与**流出**；粉丝用 AI 破解的版本
（站点上标 ``Decensored`` / ``Leak``）主要出现在 Supjav 这类流媒体站。
所以这个源的价值不在于"权威"，而在于**补上 JavDB 查不到的那一类**。

现实情况（2026-10 实测）
------------------------
``supjav.com`` 整站挂在 Cloudflare 挑战后面，纯 HTTP 请求一律返回
"Just a moment..."（403）。因此：

- 本类**默认可用但很可能取不到内容**，失败原因会如实写进检索报告；
- 想真正用起来，需要配置 ``release_cf_bypass_url`` 指向一个 FlareSolverr
  兼容服务（MDCX 用的就是这套协议），传输层会自动切换；
- 解析策略写得比较宽容：站点改版时宁可"解析不到"也不抛异常。

正因为无法在本机稳定验证选择器，这里**不臆造字段**：只取标题、链接和时间，
片商/类别/磁链留空。它产出的是"疑似"，不是"确有" —— 判定权交给调用方。
"""

from __future__ import annotations

import re
import urllib.parse

from censor.releases.base import ReleaseSource
from censor.releases.html import clean
from censor.releases.models import ReleaseCandidate, ReleaseQuery, normalize_number

__all__ = ["SupjavSource", "UNCENSORED_HINTS"]

BASE = "https://supjav.com"

#: 站点上表示"破解/无码"的标记
UNCENSORED_HINTS = (
    "uncensored", "decensored", "leak", "leaked", "无码", "無碼", "無修正",
    "无修正", "破解", "流出", "reduced mosaic",
)

_ITEM = r'<a[^>]+href="(https?://[^"]*supjav[^"]*?/(\d+)\.html)"[^>]*>(.*?)</a>'


class SupjavSource(ReleaseSource):
    name = "supjav"
    title = "Supjav（破解版）"
    homepage = BASE
    capabilities = ("http", "cf-bypass")
    version = "1"

    def search(self, query: ReleaseQuery, *, should_stop=lambda: False,
               log=lambda _m: None, limit: int = 6) -> list[ReleaseCandidate]:
        if not query.usable:
            return []
        keyword = query.number or query.title_hint
        url = f"{BASE}/?s={urllib.parse.quote(keyword)}"
        # 被 Cloudflare 拦住时这里会抛 TransportBlocked，
        # 由 service 记录成"该源被拦"，而不是"没有无码版"
        page = self.transport.get(url)
        out: list[ReleaseCandidate] = []
        seen: set[str] = set()
        for match in re.finditer(_ITEM, page or "", re.S):
            if should_stop() or len(out) >= limit:
                break
            href, _id, body = match.group(1), match.group(2), match.group(3)
            if href in seen:
                continue
            seen.add(href)
            title = clean(body)
            if not title or len(title) < 4:
                continue
            number = self._guess_number(title)
            if query.number and normalize_number(query.number) not in normalize_number(title) \
                    and normalize_number(number) != normalize_number(query.number):
                continue
            uncensored, reason = self._judge(title)
            out.append(ReleaseCandidate(
                source=self.name,
                number=number or query.number,
                title=title[:200],
                page_url=href,
                uncensored=uncensored,
                uncensored_reason=reason,
                uncensored_confidence=0.55 if uncensored else 0.0,
            ))
        log(f"Supjav 命中 {len(out)} 条")
        return out

    # ── 解析 ──
    @staticmethod
    def _judge(title: str) -> tuple[bool, str]:
        lowered = title.lower()
        hits = [hint for hint in UNCENSORED_HINTS if hint in lowered]
        if hits:
            return True, f"标题含无码/破解标记（{hits[0]}）"
        return False, ""

    @staticmethod
    def _guess_number(title: str) -> str:
        from censor.numbering import parse_number

        return parse_number(title).number
