"""版本可用性检索的领域模型。

和 `censor.models` 一样，这一层不碰网络、不碰 Qt，只描述"查到的东西长什么样"。

三个概念要分清：

``ReleaseCandidate``
    网络上**一个具体条目**（某个站点的作品页 / 种子页），
    带片商、类别、发行日期和它自己的下载链接。

``Availability``
    这部作品**是否存在可用的替代版本**的四态结论。注意它描述的是"网络上有没有"，
    不是"本地这个文件是否需要处理" —— 后者由 :mod:`censor.fusion` 负责。

``ReleaseReport``
    一次检索的完整结果：四态结论 + 全部条目 + 每个站点的成败。
    有任何站点失败都会记在 ``sources`` 里，绝不用"未查到"掩盖"没查成"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from censor.models import ProcessingStatus

__all__ = [
    "Availability", "ReleaseQuery", "MagnetLink", "ReleaseCandidate",
    "ReleaseReport", "normalize_number",
]


def normalize_number(value: str) -> str:
    """把番号压成可比较的形式：大写、去掉分隔符与多余空白。

    ``abc-123`` / ``ABC_123`` / ``ABC 123`` 都归一成 ``ABC123``，
    这样站内条目和本地文件名才能对得上。
    """
    text = str(value or "").upper()
    for ch in (" ", "-", "_", ".", "\t", "\u3000"):
        text = text.replace(ch, "")
    return text


class Availability(str, Enum):
    """这部**作品**在网络上的替代版本可得性。"""

    CONFIRMED = "confirmed"   # 查到明确的替代版本条目
    LIKELY = "likely"         # 有条目线索（标题/标签命中标记）但没有确证条目
    NONE = "none"             # 查到作品，但没有任何替代版本
    UNKNOWN = "unknown"       # 没查成：无番号 / 站点全部不可用 / 无结果

    @property
    def label(self) -> str:
        return {
            "confirmed": "确有",
            "likely": "疑似",
            "none": "未见",
            "unknown": "未查",
        }[self.value]

    @property
    def actionable(self) -> bool:
        """值不值得让用户去下载替换。"""
        return self in (Availability.CONFIRMED, Availability.LIKELY)


@dataclass(frozen=True)
class ReleaseQuery:
    """一次检索的输入。"""

    number: str = ""            # 规范化前的原始番号
    title_hint: str = ""        # 文件名里的标题片段，用于在无番号时兜底搜索
    duration_seconds: float = 0.0   # 本地文件时长，用来在同番号多条目里挑最像的

    @property
    def key(self) -> str:
        return normalize_number(self.number) or normalize_number(self.title_hint)

    @property
    def usable(self) -> bool:
        return bool(self.key)


@dataclass(frozen=True)
class MagnetLink:
    """一条磁链。``size_mb`` 用于排序（大的一般是高清版）。"""

    uri: str
    name: str = ""
    size_mb: int = 0
    files: int = 0
    date: str = ""
    tags: tuple[str, ...] = ()

    @property
    def size_text(self) -> str:
        if self.size_mb <= 0:
            return "大小未知"
        if self.size_mb >= 1024:
            return f"{self.size_mb / 1024:.2f} GB"
        return f"{self.size_mb} MB"


@dataclass
class ReleaseCandidate:
    """网络上的一个条目。"""

    source: str                    # 站点标识，如 ``javdb``
    number: str = ""
    title: str = ""
    page_url: str = ""
    maker: str = ""                # 片商
    label: str = ""                # 厂牌/系列
    release_date: str = ""
    duration_minutes: int = 0
    categories: tuple[str, ...] = ()
    uncensored: bool = False       # 该条目本身是否属于替代版本
    uncensored_reason: str = ""    # 判定依据（必须可读，便于核对）
    uncensored_confidence: float = 0.0  # 依据有多硬：站点标记/厂牌 0.9，仅标题线索 0.55
    magnets: tuple[MagnetLink, ...] = ()

    @property
    def downloadable(self) -> bool:
        return self.best_magnet is not None

    @property
    def best_magnet(self) -> MagnetLink | None:
        """优先要文件最大的那条 —— 通常是高清正片而非片段。"""
        if not self.magnets:
            return None
        from censor.tag_signals import classify_tag
        from censor.releases.base import contains_number
        matches, unmarked = [], []
        for magnet in self.magnets:
            if self.number and not contains_number(magnet.name, self.number):
                continue
            uncensored, censored = classify_tag(" ".join((magnet.name, *magnet.tags)))
            if uncensored and not censored:
                matches.append(magnet)
            elif not censored:
                unmarked.append(magnet)
        pool = matches if self.uncensored else matches or unmarked
        return max(pool, key=lambda m: (m.size_mb, m.files == 1)) if pool else None

    @property
    def total_size_mb(self) -> int:
        return max((m.size_mb for m in self.magnets), default=0)


@dataclass
class ReleaseReport:
    """一次检索的完整结果。"""

    number: str = ""
    availability: Availability = Availability.UNKNOWN
    candidates: tuple[ReleaseCandidate, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)  # 站点 -> 状态说明
    note: str = ""
    elapsed: float = 0.0
    queried_at: float = 0.0
    status: ProcessingStatus = ProcessingStatus.COMPLETED
    cache_hit: bool = False

    @property
    def uncensored(self) -> tuple[ReleaseCandidate, ...]:
        return tuple(c for c in self.candidates if c.uncensored)

    @property
    def uncensored_confidence(self) -> float:
        return max((c.uncensored_confidence for c in self.candidates if c.uncensored),
                   default=0.0)

    @property
    def best(self) -> ReleaseCandidate | None:
        """最值得下载的那条：有下载链接的替代版本条目优先，其次体积大的。"""
        pool = [c for c in self.uncensored if c.best_magnet] or list(self.uncensored)
        if not pool:
            return None
        return max(pool, key=lambda c: (c.uncensored, c.total_size_mb))

    @property
    def has_download(self) -> bool:
        return any(c.uncensored and c.downloadable for c in self.candidates)

    @property
    def stage(self) -> str:
        """在"找替换"这一步里，这部片处在哪：todo / confirmed / likely / none / unknown。

        放在模型里而不是界面里：界面只该显示结论，不该自己去解释 Available 的取值。
        """
        return {
            Availability.CONFIRMED: "confirmed",
            Availability.LIKELY: "likely",
            Availability.NONE: "none",
        }.get(self.availability, "unknown")

    def as_text(self) -> str:
        """一行摘要，给表格单元格用。"""
        if self.status is ProcessingStatus.CANCELLED:
            return "已取消（可继续）"
        if self.status is ProcessingStatus.FAILED:
            return f"查询失败（{self.note or '可重试'}）"
        if self.status is ProcessingStatus.PARTIAL and self.availability is Availability.UNKNOWN:
            return "部分来源未完成（可重试）"
        if self.availability is Availability.CONFIRMED:
            count = len(self.uncensored)
            tail = "有磁链" if self.has_download else "无磁链"
            return f"确有（{count} 条 · {tail}）"
        if self.availability is Availability.LIKELY:
            return "疑似"
        if self.availability is Availability.NONE:
            return "未见替代版本"
        return "未查" + (f"（{self.note}）" if self.note else "")
