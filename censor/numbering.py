"""番号解析 —— 从文件名 / 目录名里认出「这是哪部作品」。

为什么值得单独一个模块
----------------------
"这部片有没有无码版"是**作品级**问题，必须先有可靠的番号才能去查；
而现实里的文件名长这样（都是本机真实样本）::

    [PIYO-170] エッチが気持ちいいと…\\PIYO-170.mp4
    FC2PPV 1039683 【无码流出】【完全版】… - Supjav.com - ….mp4
    STARS-016-U.mp4
    FC2-3349153-C.mp4
    hhd800.com@ABC-123.mp4
    n1049.mp4

这里只做**纯字符串处理**，不做网络请求、不读文件以外的任何东西，
所以可以放心地在 CI 上跑满用例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "MovieNumber", "UNCENSORED_PROVIDERS", "SUBTITLE_SUFFIXES",
    "parse_number", "parse_from_path", "looks_uncensored_series",
]

#: 天生无码 / 无码专门厂牌。命中即"这部作品本身就不存在马赛克"。
UNCENSORED_PROVIDERS = frozenset({
    "fc2", "fc2ppv", "heyzo", "1pondo", "1pon", "10musume", "10mu",
    "caribbeancom", "carib", "tokyo-hot", "tokyohot", "t-h", "muramura",
    "heydouga", "gachinco", "gachip", "pacopacomama", "pacopaco", "luxus",
    "luxu", "mesubuta", "avgle", "av-uncen", "uncensored",
})

#: 番号后缀里的"字幕/版本"标记。注意 **C 是中文字幕，不代表无码**，
#: 历史上把它当无码会把一大批有码中字片误判。
SUBTITLE_SUFFIXES = frozenset({"c", "ch", "cn", "uc", "u", "hd", "4k", "a", "b"})

#: 常见的站点水印前缀，解析前先剥掉
_NOISE_PREFIXES = (
    "hhd800.com@", "hhd800.com", "thzu.cc@", "18av.mm-cg.com@",
    "javmix.tv@", "sextb.net@", "supjav.com@",
)

_QUALITY_NOISE = re.compile(
    r"\b(1080p|720p|2160p|4k|8k|fhd|hdtv|web[-_ ]?dl|bluray|bd|hd|sd|"
    r"x264|x265|h264|h265|hevc|aac|uncensored|leak|leaked|decensored|"
    r"restored|hack|破解|无码流出|無修正流出|流出|无码|無碼)\b",
    re.IGNORECASE,
)

# ── 各厂牌/形态的番号规则（**顺序敏感**：先匹配更具体的）──────────────
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # FC2-PPV-1234567
    ("fc2", re.compile(r"fc2[\s\-_]*(?:ppv)?[\s\-_]*(\d{6,9})", re.IGNORECASE)),
    # HEYZO-1234
    ("heyzo", re.compile(r"heyzo[\s\-_]*(\d{3,5})", re.IGNORECASE)),
    # 1pondo 010119_001 / 1pondo-123456
    ("1pondo", re.compile(r"1pondo[\s\-_]*(\d{6}[\-_]\d{2,3}|\d{5,7})", re.IGNORECASE)),
    # 10musume 010119_01
    ("10musume", re.compile(r"10mu(?:sume)?[\s\-_]*(\d{6}[\-_]\d{2})", re.IGNORECASE)),
    # Caribbeancom 010119-001 / carib-010119-001 / n1234
    ("caribbeancom", re.compile(
        r"(?:carib(?:beancom)?|カリビアンコム)[\s\-_]*(\d{6}[\-_]\d{2,3})", re.IGNORECASE)),
    # Tokyo Hot n1234 / k1234
    ("tokyo-hot", re.compile(r"(?:tokyo[\s\-_]*hot|東京熱)[\s\-_]*([nkt]\d{3,4})", re.IGNORECASE)),
    # Muramura 010119_001
    ("muramura", re.compile(r"(?:muramura|ムラムラ)[\s\-_]*(\d{6}[\-_]\d{2,3})", re.IGNORECASE)),
    # Heydouga 4017-123 / 4017_123
    ("heydouga", re.compile(r"heydouga[\s\-_]*(\d{4})[\s\-_]+(\d{3})", re.IGNORECASE)),
    # Gachinco gachi1234 / gachip123
    ("gachinco", re.compile(r"(?:gachi[\s\-_]?nco|gachip)[\s\-_]*(\d{3,4})", re.IGNORECASE)),
    # Pacopacomama 010119_001
    ("pacopacomama", re.compile(r"(?:pacopaco(?:mama)?|パコパコママ)[\s\-_]*(\d{6}[\-_]\d{2,3})",
                                re.IGNORECASE)),
    # Luxus 259luxu-1234
    ("luxus", re.compile(r"(\d{3})luxu[\s\-_]*(\d{3,4})", re.IGNORECASE)),
    # Mesubuta 010119_001
    ("mesubuta", re.compile(r"(?:mesubuta|メスブタ)[\s\-_]*(\d{6}[\-_]\d{2,3})", re.IGNORECASE)),
    # 常规 JAV 番号：ABC-123 / ABC123 / AB-01234，允许 1~3 位字母后缀版本标记
    ("", re.compile(
        r"(?<![A-Za-z0-9])([A-Za-z]{2,7})[\s\-_]?(\d{2,5})((?:[\-_][A-Za-z]{1,2})?)(?![0-9])")),
)


@dataclass(frozen=True)
class MovieNumber:
    """解析结果。``number`` 是规范化后的番号，查库时用它。"""

    number: str = ""                 # 规范化番号，如 ``PIYO-170`` / ``FC2-PPV-1234567``
    provider: str = ""               # 命中的无码厂牌（空 = 普通有码厂牌）
    series: str = ""                 # 番号字母前缀，如 ``PIYO``
    suffix: str = ""                 # 番号后缀，如 ``U``（无码）/ ``C``（中字）
    raw: str = ""                    # 原始输入

    @property
    def ok(self) -> bool:
        return bool(self.number)

    @property
    def is_uncensored_provider(self) -> bool:
        """是否天生无码厂牌/系列。"""
        return bool(self.provider) and self.provider in UNCENSORED_PROVIDERS

    @property
    def has_uncensored_suffix(self) -> bool:
        """番号后缀是不是 ``-U`` / ``-UC``（U 代表无码版）。"""
        return self.suffix.upper().startswith("U")

    @property
    def is_cn_subbed(self) -> bool:
        """是否中文字幕版（``-C`` / ``-CH``）。**与有码无关，别混用。**"""
        suffix = self.suffix.upper()
        return suffix in {"C", "CH", "CN"} or suffix.endswith("C")

    def __str__(self) -> str:
        return self.number or self.raw


def _strip_noise(name: str) -> str:
    """剥掉站点水印与画质词。

    **不动方括号**：番号本身常常就写在 ``[PIYO-170]`` 里，把括注整段删掉
    会直接丢掉番号（这是实测踩过的坑）。中文状态词由画质/状态词表处理。
    """
    text = name
    lowered = text.lower()
    for prefix in _NOISE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            lowered = text.lower()
    return _QUALITY_NOISE.sub(" ", text)


def parse_number(text: str) -> MovieNumber:
    """从任意字符串里解析番号。解析不出来时返回 ``number=""`` 的空结果。"""
    raw = str(text or "").strip()
    if not raw:
        return MovieNumber(raw=raw)

    cleaned = _strip_noise(Path(raw).stem if "." in Path(raw).name else raw)
    for provider, pattern in _RULES:
        match = pattern.search(cleaned)
        if not match:
            continue
        if provider == "":
            series = match.group(1).upper()
            digits = match.group(2)
            suffix = (match.group(3) or "").lstrip("-_").upper()
            # 纯字母+数字的误命中（如 x264、h265）在这里挡掉
            if series in {"X", "H", "UHD", "WEB", "DL"}:
                continue
            return MovieNumber(number=f"{series}-{digits}", series=series,
                               suffix=suffix, raw=raw)
        if provider == "heydouga":
            number = f"HEYDOUGA-{match.group(1)}-{match.group(2)}"
        elif provider == "luxus":
            number = f"{match.group(1)}LUXU-{match.group(2)}"
        elif provider == "fc2":
            number = f"FC2-PPV-{match.group(1)}"
        else:
            number = f"{provider.upper().replace('_', '-')}-{match.group(1)}"
        return MovieNumber(number=number, provider=provider, raw=raw)
    return MovieNumber(raw=raw)


def parse_from_path(path: str | Path) -> MovieNumber:
    """先看文件名，再看所在目录名（很多库是 ``[PIYO-170] 标题/xxx.mp4``）。"""
    p = Path(str(path))
    got = parse_number(p.name)
    if got.ok:
        return got
    got = parse_number(p.parent.name)
    if got.ok:
        return got
    # 有些把番号写在更上一层目录
    if p.parent.parent and str(p.parent.parent) not in {".", ""}:
        return parse_number(p.parent.parent.name)
    return got


def looks_uncensored_series(number: MovieNumber) -> bool:
    """作品级：这部番号本身是否属于"必然无码"的系列。"""
    return number.is_uncensored_provider


def split_uncensored_keywords(keywords: Iterable[str]) -> tuple[list[str], list[str]]:
    """把配置里的关键词分成 (厂牌类, 状态类) —— 前者可作作品级证据，后者不能。"""
    provider_like, state_like = [], []
    for kw in keywords or ():
        word = str(kw).strip().lower()
        if not word:
            continue
        if word in UNCENSORED_PROVIDERS:
            provider_like.append(word)
        else:
            state_like.append(word)
    return provider_like, state_like


__all__ += ["split_uncensored_keywords"]


def _self_check() -> Optional[str]:  # pragma: no cover - 手工排查用
    """快速自检：``python -m censor.numbering`` 打印一组样例的解析结果。"""
    samples = [
        ("[PIYO-170] エッチが…/PIYO-170.mp4", "PIYO-170", ""),
        ("FC2PPV 1039683 【无码流出】….mp4", "FC2-PPV-1039683", "fc2"),
        ("STARS-016-U.mp4", "STARS-016", ""),
        ("FC2-3349153-C.mp4", "FC2-PPV-3349153", "fc2"),
        ("hhd800.com@ABC-123.mp4", "ABC-123", ""),
        ("n1049.mp4", "", ""),
        ("heyzo-1234.mp4", "HEYZO-1234", "heyzo"),
    ]
    for name, expect, provider in samples:
        got = parse_number(name)
        flag = "OK " if got.number == expect and got.provider == provider else "DIFF"
        print(f"{flag} {name!r:45} -> {got.number!r} provider={got.provider!r}")
    return None


if __name__ == "__main__":  # pragma: no cover
    _self_check()
