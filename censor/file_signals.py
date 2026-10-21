"""文件级检测器 —— 不抽帧、不联网，纯读文件名 / NFO / 服务器标签。

这些信号单独看都不可靠（正是用户"不信任标签"的原因），
所以它们的置信度上限被 :class:`~censor.models.EvidenceSource` 压得很低，
在融合阶段只能起到"提示"和"补位"的作用。

但有一个例外值得单列：**番号本身属于天生无码厂牌**（FC2 / HEYZO / 1pondo…）。
"这部作品不存在有码版本"是作品级事实，与刮削标签是否准确无关，
因此给它单独的 :class:`~censor.models.EvidenceSource.SERIES`，置信度可以到 0.8。
"""

from __future__ import annotations

from censor.base import BaseDetector, ProbeContext
from censor.models import CensorState, Evidence, EvidenceSource
from censor.numbering import parse_from_path

__all__ = [
    "SeriesDetector", "FileNameDetector", "NfoTagDetector", "ServerTagDetector",
    "STATE_WORDS_UNCENSORED", "STATE_WORDS_CENSORED",
]

#: 标签/文件名里表示"无码"的词
STATE_WORDS_UNCENSORED = (
    "无码", "無碼", "無修正", "无修正", "流出", "破解", "uncensored",
    "decensored", "leak", "leaked", "restored",
)

#: 标签/文件名里表示"有码"的词
STATE_WORDS_CENSORED = ("有码", "有碼", "有马", "有馬", "薄码", "薄碼", "mosaic", "馬賽克")


def _classify_words(values: list[str]) -> tuple[bool, bool, list[str]]:
    """在标签列表里找状态词，返回 (有无码词, 有有码词, 命中的词)。"""
    hits_unc, hits_cen = [], []
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if any(w in text for w in STATE_WORDS_UNCENSORED):
            hits_unc.append(value)
        elif any(w in text for w in STATE_WORDS_CENSORED):
            hits_cen.append(value)
    return bool(hits_unc), bool(hits_cen), hits_unc + hits_cen


class SeriesDetector(BaseDetector):
    """番号系列规则：这部作品是不是天生无码。"""

    name = "series.rules"
    title = "番号系列规则（天生无码厂牌）"

    def detect(self, ctx: ProbeContext) -> list[Evidence]:
        number = ctx.number
        if not number.ok:
            return []
        if number.is_uncensored_provider:
            return [Evidence(
                EvidenceSource.SERIES, CensorState.UNCENSORED, 0.80,
                f"{number.number} 属于天生无码厂牌（{number.provider}）",
            )]
        return []


class FileNameDetector(BaseDetector):
    """文件名规则：关键词、``-U`` 后缀。**弱证据。**"""

    name = "filename.rules"
    title = "文件名关键词"

    def detect(self, ctx: ProbeContext) -> list[Evidence]:
        name = ctx.filename
        lowered = name.lower()
        out: list[Evidence] = []

        keywords = [str(k).lower() for k in (ctx.cfg_get("uncensored_keywords", []) or [])]
        hit_words = [k for k in keywords if k and k in lowered]
        if hit_words:
            out.append(Evidence(
                EvidenceSource.FILENAME, CensorState.UNCENSORED, 0.40,
                f"文件名命中关键词 {hit_words[:3]}",
            ))

        if ctx.number.has_uncensored_suffix:
            out.append(Evidence(
                EvidenceSource.FILENAME, CensorState.UNCENSORED, 0.50,
                f"番号后缀 -{ctx.number.suffix} 表示无码版",
            ))
        return out


class NfoTagDetector(BaseDetector):
    """同目录 NFO 里的标签。**弱证据，且不覆盖画面结论。**"""

    name = "nfo.tags"
    title = "NFO 标签"

    def detect(self, ctx: ProbeContext) -> list[Evidence]:
        tags = list(ctx.nfo_tags or [])
        if not tags:
            # 上下文没带就自己读（保持检测器可单独使用）
            try:
                from utils.nfo_source import read_nfo_features

                tags, _genres, _studios = read_nfo_features(ctx.path)
            except Exception:  # noqa: BLE001 - 读 NFO 失败不影响判定
                tags = []
            ctx.nfo_tags = list(tags)
        if not tags:
            return []
        unc, cen, hits = _classify_words(tags)
        if unc and not cen:
            return [Evidence(EvidenceSource.NFO, CensorState.UNCENSORED, 0.50,
                             f"NFO 标签命中 {hits[:3]}")]
        if cen and not unc:
            return [Evidence(EvidenceSource.NFO, CensorState.CENSORED, 0.40,
                             f"NFO 标签命中 {hits[:3]}")]
        if unc and cen:
            return [Evidence(EvidenceSource.NFO, CensorState.UNKNOWN, 0.0,
                             f"NFO 标签自相矛盾 {hits[:4]}")]
        return []


class ServerTagDetector(BaseDetector):
    """Jellyfin / Emby 库里的 Genres / Tags。**弱证据。**"""

    name = "server.tags"
    title = "媒体服务器标签"

    def detect(self, ctx: ProbeContext) -> list[Evidence]:
        tags = list(ctx.server_tags or [])
        if not tags:
            return []
        unc, cen, hits = _classify_words(tags)
        if unc and not cen:
            return [Evidence(EvidenceSource.SERVER_TAG, CensorState.UNCENSORED, 0.45,
                             f"服务器标签命中 {hits[:3]}")]
        if cen and not unc:
            return [Evidence(EvidenceSource.SERVER_TAG, CensorState.CENSORED, 0.40,
                             f"服务器标签命中 {hits[:3]}")]
        return []


def fill_number(ctx: ProbeContext) -> ProbeContext:
    """上下文没解析过番号时补上（服务层调用，保持检测器无状态）。"""
    if not ctx.number.ok:
        ctx.number = parse_from_path(ctx.path)
    return ctx
