"""检索源基类 —— 一个站点就是一个类，注册进 ``censor.registry.sources`` 就能用。

为什么要有这一层
----------------
提供这类资源的站会不停换域名、加 Cloudflare、关站。如果站点逻辑散落在
service 里，换一个站就得动主流程；抽成基类之后，加站点 = 加一个文件。

刻意**不做**的事：

- 不在基类里下"是否存在替代版本"的结论 —— 那是 :mod:`censor.releases.service` 的事；
- 不吞异常 —— 站点失败要带着原因上报，报告里必须能区分"没查到"和"没查成"；
- 不依赖配置里的具体开关 —— ``enabled`` 由 service 统一裁决。
"""

from __future__ import annotations

import time
import re
import unicodedata
from typing import Any, Callable

from censor.numbering import parse_number
from censor.releases.models import ReleaseCandidate, ReleaseQuery, normalize_number

__all__ = ["ReleaseSource", "same_number", "contains_number", "search_keyword",
           "match_token", "fc2_digits", "FC2_DIGITS_SITES"]

_STOP = {"should_stop": lambda: False}


def same_number(left: str, right: str) -> bool:
    """番号比较：忽略分隔符与大小写；两边都为空时视为相同。"""
    a, b = normalize_number(left), normalize_number(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # 允许站点写成 PIYO170 而本地是 PIYO-170 这类差异（已在 normalize 中处理），
    # 但不允许 PIYO-17 命中 PIYO-170
    return False


def fc2_digits(number: str) -> str:
    """FC2 的数字后缀（``FC2-PPV-1631136`` → ``1631136``），非 FC2 返回空串。

    为什么单独拎出来：FC2 在各站的写法根本不统一 ——
    ``FC2-PPV-1631136`` / ``FC2PPV-1631136`` / ``FC2PPV 1631136`` / 只写 ``1631136``。
    把整串丢给站内搜索经常直接 0 结果（Supjav 实测如此），而只搜数字反而能命中。
    """
    try:
        parsed = parse_number(number)
    except Exception:  # noqa: BLE001 - 解析失败就按普通番号处理
        return ""
    if parsed.provider != "fc2" or not parsed.number:
        return ""
    return parsed.number.rsplit("-", 1)[-1]


#: 实测"FC2 用整串搜会 0 命中、只搜数字才有结果"的站点。
#: 注意这不是所有站都如此（JavDB 用整串能命中，用纯数字反而会偏），
#: 所以按站点声明，不要一刀切。
FC2_DIGITS_SITES = frozenset({"supjav"})


def search_keyword(number: str, *, site: str = "", fallback: str = "") -> str:
    """**发给站点的搜索词**。与复核用的番号分开，因为"怎么搜"和"怎么认"不是一回事。

    - 在 :data:`FC2_DIGITS_SITES` 里的站点：FC2 只发数字后缀
      （Supjav 实测：搜 ``FC2-PPV-1631136`` 返回 0 条，搜 ``1631136`` 有结果）
    - 其它情况：规范化番号
    """
    if site in FC2_DIGITS_SITES:
        digits = fc2_digits(number)
        if digits:
            return digits
    # **保留横线**：normalize_number 会去掉分隔符（IPX-123 → IPX123），
    # 而站点搜索框认的是带横线的写法；拿掉横线经常搜不到。
    try:
        parsed = parse_number(number)
    except Exception:  # noqa: BLE001
        parsed = None
    canonical = (parsed.number if parsed is not None else "") or ""
    return canonical or normalize_number(number) or str(fallback or "").strip()


def match_token(number: str) -> str:
    """**复核用的关键串**。FC2 用数字，其它用完整番号。

    复核仍然带边界（见 :func:`contains_number`），所以 ``1631136`` 不会命中
    ``FC2-PPV-16311360``，也不会命中标题里的其它长数字。
    """
    return fc2_digits(number) or normalize_number(number)


def contains_number(text: str, number: str) -> bool:
    """Match a complete identifier without erasing its surrounding boundaries."""
    key = match_token(number)
    if not key:
        return False
    pattern = r"(?<![A-Z0-9])" + r"[\s_.-]*".join(re.escape(c) for c in key) + r"(?![A-Z0-9])"
    return bool(re.search(pattern, unicodedata.normalize("NFKC", str(text)).upper()))


class ReleaseSource:
    """一个检索站点。子类只需实现 :meth:`search`。"""

    #: 注册名，也是配置里开关用的名字
    name: str = ""
    #: 界面显示名
    title: str = ""
    #: 站点主页（文档/排查用）
    homepage: str = ""
    #: 声明需要什么能力：``http`` 必然有，``cf-bypass`` 表示会被 Cloudflare 拦
    capabilities: tuple[str, ...] = ("http",)
    #: 算法/解析变更时递增，让缓存失效
    version: str = "1"

    def __init__(self, transport: Any = None, cfg: Any = None) -> None:
        self.transport = transport
        self.cfg = cfg

    # ── 生命周期 ──
    def available(self) -> tuple[bool, str]:
        """(是否可用, 不可用原因)。默认只要有传输层就可用。"""
        if self.transport is None:
            return False, "没有可用的网络传输层"
        return True, ""

    def search(
        self,
        query: ReleaseQuery,
        *,
        should_stop: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = lambda _m: None,
        limit: int = 4,
    ) -> list[ReleaseCandidate]:
        raise NotImplementedError

    # ── 便捷 ──
    def timed(self, fn: Callable[[], list[ReleaseCandidate]]) -> tuple[list, float]:
        start = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - start

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {self.name}>"
