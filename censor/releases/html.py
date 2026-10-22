"""极小的 HTML 提取工具 —— 够用就好，不引入 BeautifulSoup。

抓站点最大的维护成本不是"解析不出来"，而是**解析太脆**：站点改一个 class
整条链就断。所以这里的策略是：

- 只用"结构性标记"（``<strong>`` 键值、``data-size`` 属性、``magnet:`` 链接），
  不用嵌套层级深的 class 链；
- 取不到就返回空，让上层把"没解析到"如实报告，而不是抛异常炸掉整次检索；
- 所有文本统一走 :func:`clean`：去标签、解实体、压空白。
"""

from __future__ import annotations

import html as _html
import re

__all__ = ["clean", "strip_tags", "attr", "blocks", "numbers"]

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    return _TAG.sub(" ", text or "")


def clean(text: str) -> str:
    """去标签 + 解实体 + 压空白。"""
    return _WS.sub(" ", _html.unescape(strip_tags(text or ""))).strip()


def attr(block: str, name: str) -> str:
    """取属性值，兼容单双引号。取不到返回空串。"""
    match = re.search(rf"""{re.escape(name)}\s*=\s*["']([^"']*)["']""", block or "")
    return _html.unescape(match.group(1)).strip() if match else ""


def blocks(html: str, pattern: str) -> list[str]:
    """按正则切块。``pattern`` 必须自带命名分组以外的完整正则。"""
    return [m.group(0) for m in re.finditer(pattern, html or "", re.S)]


def numbers(text: str) -> int:
    """从 ``9.54GB`` / ``9766`` 这类文本里取整数兆字节估算。"""
    value = str(text or "")
    match = re.search(r"([\d.]+)\s*(GB|MB|G|M)", value, re.I)
    if not match:
        plain = re.search(r"(\d+)", value)
        return int(plain.group(1)) if plain else 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    return int(amount * 1024) if unit.startswith("G") else int(amount)
