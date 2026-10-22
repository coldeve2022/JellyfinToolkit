"""检索源基类 —— 一个站点就是一个类，注册进 ``censor.registry.sources`` 就能用。

为什么要有这一层
----------------
无码资源的站会不停换域名、加 Cloudflare、关站。如果站点逻辑散落在
service 里，换一个站就得动主流程；抽成基类之后，加站点 = 加一个文件。

刻意**不做**的事：

- 不在基类里下"是否存在无码版"的结论 —— 那是 :mod:`censor.releases.service` 的事；
- 不吞异常 —— 站点失败要带着原因上报，报告里必须能区分"没查到"和"没查成"；
- 不依赖配置里的具体开关 —— ``enabled`` 由 service 统一裁决。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from censor.releases.models import ReleaseCandidate, ReleaseQuery, normalize_number

__all__ = ["ReleaseSource", "same_number"]

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
