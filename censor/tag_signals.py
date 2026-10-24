"""Normalize metadata labels without assuming that they describe file pixels."""

import re
import unicodedata

_TRADITIONAL = str.maketrans(
    {"無": "无", "碼": "码", "馬": "马", "賽": "赛", "洩": "泄", "發": "发", "還": "还"}
)
_UNCENSORED = re.compile(
    r"无码|无修正|去码|去马赛克|无马赛克|马赛克去除|破解|流出|モザイクなし|モザイク除去|"
    r"(?<![a-z0-9])(?:uncensored|decensored|uncen|un[\s_-]+censored|leak(?:ed)?)(?![a-z0-9])"
)
_CENSORED = re.compile(
    r"有码|有马|薄码|马赛克|"
    r"(?<![a-z0-9])(?:censored|mosaic)(?![a-z0-9])"
)
_NEGATED = re.compile(
    r"(?:非|不是|并非|并不|不属于|未|尚未|未经|未經|未被|没有|沒有|无)"
    r"\s*(?:无码|无修正|破解|流出|去码|去马赛克)|"
    r"\b(?:not|non)[\s_-]+(?:uncensored|decensored|uncen|leak(?:ed)?|un[\s_-]+censored)\b"
)


def classify_tag(value: str) -> tuple[bool, bool]:
    normalized = unicodedata.normalize("NFKC", str(value)).translate(_TRADITIONAL).casefold()
    # Fold separators within CJK phrases, preserving English word boundaries.
    normalized = re.sub(r"(?<=[\u3040-\u9fff])[\s_\-－]+(?=[\u3040-\u9fff])", "", normalized)
    if _NEGATED.search(normalized):
        return False, True
    uncensored = bool(_UNCENSORED.search(normalized))
    # A removal phrase contains 马赛克, but is not evidence that it remains.
    censored_text = _UNCENSORED.sub("", normalized)
    return uncensored, bool(_CENSORED.search(censored_text))


# ── 版本标记分级（检索源专用）─────────────────────────────
# 与 classify_tag 分开：那边回答"这个标签是不是在说有码/无码"（检测链路用），
# 这里回答"这个条目是什么版本"，而且要能区分**减码版**这种中间态。
_STRONG_VERSION = re.compile(
    r"无码|无修正|去码|去马赛克|无马赛克|破解|流出|モザイクなし|モザイク除去|"
    r"(?<![a-z0-9])(?:uncensored|decensored|uncen|leak(?:ed)?)(?![a-z0-9])"
)
_SOFT_VERSION = re.compile(
    r"reduc(?:ing|ed)[\s_-]*mosaic|mosaic[\s_-]*reduc\w*|"
    r"薄码|薄碼|减码|減碼|減馬|轻微马赛克|マイルド|"
    r"(?<![a-z0-9])reduc(?:ing|ed)[\s_-]*censor\w*"
)
_CENSORED_VERSION = re.compile(
    r"有码|有馬|有马|"
    r"(?<![a-z0-9])(?:censored|mosaic)(?![a-z0-9])"
)


def version_level(value: str) -> tuple[str, str]:
    """判断条目属于哪个版本，返回 ``(级别, 命中片段)``。

    级别含义（这是"下结论要利索"的关键：减码版不该被当成有码）：

    - ``strong``：站点点名是无码/流出/破解版
    - ``soft``  ：站点标注减码/薄码 —— 码被处理过，可能仍有残余；**算"有得换"**
    - ``censored``：站点标注有码
    - ``neutral``：与版本无关的标签（中文字幕、4K、60FPS 之类），不参与判断

    为什么不复用 :func:`classify_tag`：它把 ``mosaic`` 一律算"有码"，
    于是 ``[Reducing Mosaic]YSN-638`` 被判成有码 —— 这正是"大量条目要人工复核"
    的根源。这里把"减码"单独成一档，检测链路继续用原函数，互不影响。
    """
    normalized = unicodedata.normalize("NFKC", str(value)).translate(_TRADITIONAL).casefold()
    normalized = re.sub(r"(?<=[\u3040-\u9fff])[\s_\-－]+(?=[\u3040-\u9fff])", "", normalized)
    if _NEGATED.search(normalized):
        return "censored", "否定的无码表述"
    for level, pattern in (("strong", _STRONG_VERSION), ("soft", _SOFT_VERSION)):
        match = pattern.search(normalized)
        if match:
            return level, match.group(0)
    match = _CENSORED_VERSION.search(normalized)
    if match:
        return "censored", match.group(0)
    return "neutral", ""
