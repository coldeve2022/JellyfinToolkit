"""JavBus 检索源 —— 第二个独立信源，用来和 JavDB 互证。

它有两个 JavDB 没有的好处：

1. **直接给出 ``uc`` 标记**（``var uc = 0/1``）—— 站点自己就标了这条是不是無碼，
   不用我们从片商/类别去推；
2. 磁链走 ajax 接口，一次能拿到整页种子（含大小），适合"确有其片且能下载"。

代价：站点有年龄门槛，必须带 ``age=verified`` cookie；详情页 URL 就是 ``/{番號}``，
无码作品在 ``/uncensored`` 分支下，找不到时会返回 404 —— 这属于"该站没有"，不算失败。
"""

from __future__ import annotations

import re
import urllib.parse

from censor.numbering import parse_number
from censor.releases.base import ReleaseSource
from censor.releases.html import clean, numbers
from censor.releases.models import MagnetLink, ReleaseCandidate, ReleaseQuery
from censor.releases.transport import TransportBlocked, TransportNotFound

__all__ = ["JavBusSource"]

BASE = "https://www.javbus.com"

#: 年龄门槛 cookie。缺了它所有详情页都会变成验证页。
COOKIE = "age=verified; dv=1; existmag=all; languages=zh"

_HEADER = {
    "Cookie": COOKIE,
    "X-Requested-With": "XMLHttpRequest",
}


class JavBusSource(ReleaseSource):
    name = "javbus"
    title = "JavBus"
    homepage = BASE
    capabilities = ("http", "cookie")
    version = "1"

    def search(self, query: ReleaseQuery, *, should_stop=lambda: False,
               log=lambda _m: None, limit: int = 2) -> list[ReleaseCandidate]:
        if not self.available()[0] or not query.number:
            return []
        number = query.number.strip().upper().replace("_", "-")
        candidate = self._fetch_movie(number, referer=BASE, log=log)
        if candidate is not None:
            return [candidate]
        # 有码分支没有，再试无码分支
        return [c] if (c := self._fetch_movie(f"uncensored/{number}", BASE, log)) else []

    # ── 内部 ──
    def _fetch_movie(self, slug: str, referer: str, log) -> ReleaseCandidate | None:
        url = f"{BASE}/{urllib.parse.quote(slug)}"
        try:
            page = self.transport.get(url, referer=referer, headers={"Cookie": COOKIE})
        except TransportNotFound:
            return None
        except TransportBlocked:
            raise          # 被 Cloudflare 拦是"没查成"，必须如实上报
        except Exception as exc:  # noqa: BLE001 - 超时等算"该站这次没给出"
            log(f"JavBus {slug} 不可用：{exc}")
            return None
        if "識別碼" not in page and "识别码" not in page:
            return None

        fields = self._fields(page)
        number = re.sub(r"\s+", "", fields.get("識別碼", "") or fields.get("识别码", ""))
        gid = self._js_int(page, "gid")
        uc = self._js_int(page, "uc")
        categories = self._categories(page)
        title = self._title(page)
        maker = fields.get("發行商", "") or fields.get("发行商", "")
        uncensored = uc == 1
        reason = "JavBus 标记为無碼（uc=1）" if uncensored else ""
        if not uncensored:
            hits = [w for w in ("無碼", "无码", "無修正", "无修正", "流出", "破解")
                    if any(w in c for c in categories) or w in maker]
            if hits:
                uncensored, reason = True, f"JavBus 类别/发行商含无码标记（{hits[0]}）"
        if not uncensored:
            movie = parse_number(number)
            if movie.is_uncensored_provider:
                uncensored, reason = True, f"番号属于天生无码系列（{movie.provider}）"

        magnets = self._magnets(gid, uc, url) if gid else []
        return ReleaseCandidate(
            source=self.name,
            number=number,
            title=title,
            page_url=url,
            maker=maker,
            label=fields.get("系列", ""),
            release_date=fields.get("發行日期", ""),
            duration_minutes=self._minutes(fields.get("長度", "")),
            categories=categories,
            uncensored=uncensored,
            uncensored_reason=reason,
            uncensored_confidence=0.9 if uncensored else 0.0,
            magnets=tuple(magnets),
        )

    @staticmethod
    def _title(page: str) -> str:
        match = re.search(r"<title>(.*?)</title>", page or "", re.S)
        text = clean(match.group(1)) if match else ""
        return re.sub(r"\s*-\s*JavBus\s*$", "", text).strip()

    @staticmethod
    def _fields(page: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for match in re.finditer(
            r'<span class="header">([^<]+)</span>(.*?)</p>', page or "", re.S
        ):
            key = clean(match.group(1)).rstrip(":：").strip()
            value = clean(match.group(2))
            if key and value and key not in out:
                out[key] = value
        return out

    @staticmethod
    def _categories(page: str) -> tuple[str, ...]:
        match = re.search(r'類別[:：]?.*?</p>(.*?)</div>', page or "", re.S)
        if not match:
            match = re.search(r'类别[:：]?.*?</p>(.*?)</div>', page or "", re.S)
        if not match:
            return ()
        chunk = match.group(1)
        names = [clean(name) for name in re.findall(r'<span class="genre">.*?<a[^>]*>(.*?)</a>', chunk, re.S)]
        return tuple(dict.fromkeys(n for n in names if n))

    @staticmethod
    def _js_int(page: str, name: str) -> int:
        match = re.search(rf"var\s+{name}\s*=\s*['\"]?(\d+)", page or "")
        return int(match.group(1)) if match else 0

    def _magnets(self, gid: int, uc: int, referer: str) -> list[MagnetLink]:
        url = (f"{BASE}/ajax/uncledatoolsbyajax.php?gid={gid}&lang=zh&uc={uc}&floor=1")
        try:
            page = self.transport.get(url, referer=referer, headers=_HEADER)
        except Exception:  # noqa: BLE001 - 磁链拿不到不影响"确有无码版"这个结论
            return []
        out: list[MagnetLink] = []
        seen: set[str] = set()
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page or "", re.S):
            match = re.search(r'href="(magnet:[^"]+)"', row)
            if not match:
                continue
            uri = match.group(1)
            if uri in seen:
                continue
            seen.add(uri)
            name = clean(re.search(r"<a[^>]*>(.*?)</a>", row, re.S).group(1)) \
                if re.search(r"<a[^>]*>(.*?)</a>", row, re.S) else ""
            cells = " ".join(clean(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S))
            out.append(MagnetLink(
                uri=uri, name=name, size_mb=numbers(cells),
                files=1 if "1個" in cells or "1个" in cells else 0,
            ))
        return out

    @staticmethod
    def _minutes(text: str) -> int:
        match = re.search(r"(\d+)\s*分", text or "")
        return int(match.group(1)) if match else 0
