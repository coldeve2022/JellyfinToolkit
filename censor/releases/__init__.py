"""版本可用性网络检索 —— 和检测子系统平行的另一个可插拔子系统。

分工
----
``censor``（检测）回答：**本地这个文件在画面上是否需要处理**。
``censor.releases``（检索）回答：**网络上有没有这部作品的替代版本，能不能下载**。

两件事的证据不能互相冒充：外部元数据这样写，不代表本地文件如此；
网络上存在替代版本，也不代表你手上这份不是替代版本。

分层
----
``models``    候选/下载链接/四态结论，纯数据。
``html``      极小的 HTML 提取工具（不引入 BeautifulSoup）。
``transport`` HTTP + 代理 + 限速 + Cloudflare 旁路，全部标准库。
``base``      检索源基类（一个站点一个类）。
``javdb`` / ``javbus`` / ``supjav`` 具体站点。
``store``     作品编号级缓存与待查队列（与检测共用 censor.db，但独立表）。
``service``   编排与判定（"确有 / 疑似 / 未见 / 未查"的定义在这）。

加一个站点
----------
1. 新建 ``xxx.py``，继承 :class:`censor.releases.base.ReleaseSource`，实现 ``search``；
2. 在下面 ``register_builtins`` 里加一行 ``sources.add(...)``；
3. 完。判定、缓存、界面都不用改。

站点挂掉或改版时：``search`` 抛异常即可，报告里会显示"失败：原因"，
不会把失败伪装成"没有替代版本"。
"""

from __future__ import annotations

from censor.registry import sources
from censor.releases.models import (
    Availability, MagnetLink, ReleaseCandidate, ReleaseQuery, ReleaseReport,
    normalize_number,
)
from censor.releases.service import (
    ALGORITHM_VERSION, ReleaseOptions, ReleaseService, decide_availability,
)
from censor.releases.store import ReleaseStats, ReleaseStore

__all__ = [
    "Availability", "MagnetLink", "ReleaseCandidate", "ReleaseQuery", "ReleaseReport",
    "normalize_number", "ReleaseService", "ReleaseOptions", "ReleaseStore",
    "ReleaseStats", "decide_availability", "ALGORITHM_VERSION", "register_builtins",
]


def register_builtins() -> None:
    """注册内置检索源。**重复调用安全。**"""
    from censor.releases.javbus import JavBusSource
    from censor.releases.javdb import JavDbSource
    from censor.releases.supjav import SupjavSource

    sources.add("javdb", JavDbSource, priority=10, title="JavDB",
                description="作品详情 + 片商/类别 + 下载链接，替代版本判定最稳",
                tags=("http",), replace=True)
    sources.add("javbus", JavBusSource, priority=20, title="JavBus",
                description="独立信源，站点自带無碼标记，磁链走 ajax",
                tags=("http", "cookie"), replace=True)
    sources.add("supjav", SupjavSource, priority=30, title="Supjav（破解版）",
                description="AI 破解/流出最集中；官网有 Cloudflare，需旁路服务",
                tags=("http", "cf-bypass"), replace=True)


register_builtins()
