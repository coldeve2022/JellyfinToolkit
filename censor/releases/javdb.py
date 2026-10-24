"""JavDB 检索源 —— 目前唯一能稳定给出"无码版是否存在"和"能不能直接下载"的站。

为什么是它
----------
实测（2026-10）在本机能直连直出：

- 搜索页 ``/search?q=番號&f=all`` 服务端渲染，条目里带番号、日期、是否含磁链；
- 详情页 ``/v/xxxx`` 有「番號 / 日期 / 時長 / 片商 / 系列 / 類別」结构化字段；
- 详情页的磁链区块**直接给出 ``magnet:`` 链接**，还带 ``data-size`` 字节量级，
  所以"确有"和"可下载"能一次拿全，不用再去第二个站碰运气。

无码判定用三路，任一命中即算该条目是无码版：

1. **片商**属于无码厂牌（一本道 / カリビアンコム / 日本コンテンツ流通 …）；
2. **類別**里出现 無碼 / 無修正 / 流出版 / 破解 等字样；
3. **番号本身**属于天生无码系列（FC2 / HEYZO …）。

判定依据必须写成可读文本存进候选里 —— 用户要能自己核对，而不是信一个布尔值。
"""

from __future__ import annotations

import urllib.parse

from censor.numbering import parse_number
from censor.releases.base import ReleaseSource, search_keyword, same_number
from censor.releases.html import attr, clean
from censor.releases.models import MagnetLink, ReleaseCandidate, ReleaseQuery
from censor.tag_signals import classify_tag
from censor.releases.transport import TransportError

__all__ = ["JavDbSource", "UNCENSORED_MAKERS", "UNCENSORED_WORDS"]

BASE = "https://javdb.com"

#: 无码/流出专门厂牌。命中即"这条作品本身就是无码发行"。
UNCENSORED_MAKERS = frozenset({
    "日本コンテンツ流通", "一本道", "カリビアンコム", "10musume", "パコパコママ",
    "ムラムラ", "東京熱", "東熱", "ガチンコ", "Heyzo", "天然むすめ",
    "エッチな4610", "エッチな0930", "エッチな一本道", "無修正", "素人",
    "Caribbeancom", "1pondo", "Pacopacomama", "Muramura", "Tokyo Hot",
})

#: 类别/标题里表示无码的词。中文繁体与日文写法都要覆盖。
UNCENSORED_WORDS = (
    "無碼", "无码", "無修正", "无修正", "流出版", "流出", "破解",
    "Decensored", "Uncensored", "レイプ?無修正",
)

_ITEM = r'<a href="(/v/[^"]+)" class="box" title="([^"]*)"(.*?)</a>'
_MAGNET_ITEM = r'<div class="item[^"]*"[^>]*data-size="[^"]*"[^>]*>'


class JavDbSource(ReleaseSource):
    name = "javdb"
    title = "JavDB"
    homepage = BASE
    capabilities = ("http",)
    version = "4"

    def search(self, query: ReleaseQuery, *, should_stop=lambda: False,
               log=lambda _m: None, limit: int = 4) -> list[ReleaseCandidate]:
        self.require_transport()
        if not query.usable:
            return []

        keyword = search_keyword(query.number, site='javdb', fallback=query.title_hint)
        search_url = f"{BASE}/search?q={urllib.parse.quote(keyword)}&f=all"
        page = self.transport.get(search_url)
        if not page.strip():
            raise TransportError('搜索响应为空，无法查证')
        rows = self._parse_search(page, query)
        log(f"JavDB 搜索「{keyword}」命中 {len(rows)} 条同番号条目")

        out: list[ReleaseCandidate] = []
        errors = []
        for row in rows[: max(1, limit)]:
            if should_stop():
                break
            try:
                detail = self.transport.get(BASE + row["href"], referer=search_url)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它条目
                log(f"JavDB 详情读取失败 {row['href']}：{exc}")
                errors.append(str(exc))
                continue
            try:
                candidate = self._parse_detail(detail, row, query)
            except (ValueError, TypeError) as exc:
                errors.append(str(exc))
                continue
            if not query.number or same_number(candidate.number, query.number):
                out.append(candidate)
            else:
                errors.append("详情番号与查询不一致")
        self.incomplete_reason = "；".join(errors)
        if errors and not out:
            raise TransportError(self.incomplete_reason)
        return out

    # ── 解析：搜索页 ──
    def _parse_search(self, page: str, query: ReleaseQuery) -> list[dict]:
        import re

        rows = []
        for match in re.finditer(_ITEM, page or "", re.S):
            href, title, body = match.group(1), match.group(2), match.group(3)
            number = clean(re.search(r"<strong>(.*?)</strong>", body, re.S).group(1)) \
                if re.search(r"<strong>(.*?)</strong>", body, re.S) else ""
            if not number:
                continue
            if query.number and not same_number(number, query.number):
                continue
            rows.append({
                "href": href,
                "title": clean(title),
                "number": number,
                "has_magnet": "含磁鏈" in body or "含磁链" in body,
            })
        return rows

    # ── 解析：详情页 ──
    def _parse_detail(self, page: str, row: dict, query: ReleaseQuery) -> ReleaseCandidate:
        fields = self._panel_fields(page)
        number = self._squash(fields.get("番號", "") or fields.get("番号", ""))
        if not number:
            raise ValueError('详情缺少结构化番号，无法查证')
        categories = tuple(self._categories(page))
        maker = fields.get("片商", "")
        title = row.get("title") or fields.get("標題", "")

        uncensored, reason = self._judge_uncensored(number, maker, categories, title)
        return ReleaseCandidate(
            source=self.name,
            number=number,
            title=title[:200],
            page_url=BASE + row["href"],
            maker=maker,
            label=fields.get("系列", ""),
            release_date=fields.get("日期", ""),
            duration_minutes=self._minutes(fields.get("時長", "")),
            categories=categories,
            uncensored=uncensored,
            uncensored_reason=reason,
            uncensored_confidence=(0.55 if reason.startswith("标题") else 0.9) if uncensored else 0.0,
            magnets=tuple(self._parse_magnets(page)),
        )

    @staticmethod
    def _panel_fields(page: str) -> dict[str, str]:
        import re

        out: dict[str, str] = {}
        for match in re.finditer(r"<strong>(.*?)</strong>(.*?)</div>", page or "", re.S):
            key = clean(match.group(1)).rstrip(":：").strip()
            value = clean(match.group(2))
            if key and key not in out:
                out[key] = value
        return out

    @staticmethod
    def _categories(page: str) -> list[str]:
        import re

        match = re.search(r"<strong>\s*類別.*?</strong>(.*?)</div>", page or "", re.S)
        if not match:
            match = re.search(r"<strong>\s*类别.*?</strong>(.*?)</div>", page or "", re.S)
        if not match:
            return []
        chunk = match.group(1)
        names = [clean(name) for name in re.findall(r"<a[^>]*>(.*?)</a>", chunk, re.S)]
        if not names:
            names = [part.strip() for part in clean(chunk).split(",")]
        return [name for name in dict.fromkeys(names) if name]

    @staticmethod
    def _parse_magnets(page: str) -> list[MagnetLink]:
        import re

        out: list[MagnetLink] = []
        seen: set[str] = set()
        items = list(re.finditer(_MAGNET_ITEM, page or ""))
        for index, match in enumerate(items):
            start = match.start()
            end = items[index + 1].start() if index + 1 < len(items) else len(page)
            depth = 0
            for tag in re.finditer(r'</?div\b[^>]*>', page[start:end], re.I):
                depth += -1 if tag.group().startswith('</') else 1
                if depth == 0:
                    end = start + tag.end()
                    break
            block = page[start:end]
            href = attr(block, "href")
            if not href.startswith("magnet:"):
                link = re.search(r'href="(magnet:[^"]+)"', block)
                href = attr(link.group(0), "href") if link else ""
            if not href.startswith("magnet:") or href in seen:
                continue
            seen.add(href)
            name_match = re.search(r'<span class="name">(.*?)</span>', block, re.S)
            tags = tuple(clean(t) for t in re.findall(
                r'<span class="tag[^"]*">(.*?)</span>', block, re.S) if clean(t))
            out.append(MagnetLink(
                uri=href,
                name=clean(name_match.group(1)) if name_match else "",
                size_mb=int(attr(block, "data-size") or 0),
                files=int(attr(block, "data-files") or 0),
                date=attr(block, "data-date"),
                tags=tags,
            ))
        return out

    @staticmethod
    def _squash(number: str) -> str:
        """JavDB 把番号拆成 ``<a>STARS</a>-016``，去标签后会多出空格。"""
        import re

        return re.sub(r"\s+", "", number or "")

    @staticmethod
    def _minutes(text: str) -> int:
        import re

        match = re.search(r"(\d+)\s*(分|min)", text or "")
        return int(match.group(1)) if match else 0

    @staticmethod
    def _judge_uncensored(number: str, maker: str, categories, title: str) -> tuple[bool, str]:
        movie = parse_number(number)
        if movie.is_uncensored_provider:
            return True, f"番号属于天生无码系列（{movie.provider}）"
        if maker and maker in UNCENSORED_MAKERS:
            return True, f"片商为无码厂牌（{maker}）"
        hits = [str(cat) for cat in categories if classify_tag(cat)[0]]
        if hits:
            return True, f"类别含无码标记（{hits[0]}）"
        if classify_tag(title)[0]:
            return True, "标题含无码标记（仅为软线索）"
        return False, ""

    def require_transport(self) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(why)
