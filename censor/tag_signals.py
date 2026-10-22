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
