"""番号解析的用例 —— 全部来自本机真实文件名，避免"想当然"。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.numbering import MovieNumber, parse_from_path, parse_number  # noqa: E402


@pytest.mark.parametrize("text,number,provider", [
    ("[PIYO-170] エッチが気持ちいい.mp4", "PIYO-170", ""),
    ("PIYO-170.mp4", "PIYO-170", ""),
    ("STARS-016-U.mp4", "STARS-016", ""),
    ("FC2PPV 1039683 【无码流出】完全版.mp4", "FC2-PPV-1039683", "fc2"),
    ("FC2-3349153-C.mp4", "FC2-PPV-3349153", "fc2"),
    ("hhd800.com@ABC-123.mp4", "ABC-123", ""),
    ("heyzo-1234.mp4", "HEYZO-1234", "heyzo"),
    ("1pondo_010119_001.mp4", "1PONDO-010119_001", "1pondo"),
    ("Tokyo-Hot-n1234.mp4", "TOKYO-HOT-n1234", "tokyo-hot"),
    ("MISM-273.mkv", "MISM-273", ""),
])
def test_parse_number(text, number, provider):
    got = parse_number(text)
    assert got.number == number
    assert got.provider == provider


def test_cn_subtitle_suffix_is_not_uncensored():
    """``-C`` / ``-CH`` 是中文字幕，历史上被当成无码会误判一大批。"""
    got = parse_number("ABC-123-C.mp4")
    assert got.number == "ABC-123"
    assert got.is_cn_subbed
    assert not got.has_uncensored_suffix


def test_uncensored_suffix():
    got = parse_number("STARS-016-UC.mp4")
    assert got.has_uncensored_suffix


def test_unknown_name_returns_empty_number():
    got = parse_number("n1049.mp4")
    assert not got.ok
    assert isinstance(got, MovieNumber)


def test_parse_from_path_falls_back_to_parent_dir():
    got = parse_from_path(r"I:\lib\[PIYO-170] タイトル\random_name.mp4")
    assert got.number == "PIYO-170"


def test_quality_tokens_do_not_become_numbers():
    """``x264`` / ``h265`` 这类画质词不能被当成番号。"""
    for name in ("movie x264 1080p.mp4", "clip h265.mkv"):
        assert not parse_number(name).ok


def test_uncensored_provider_flag():
    assert parse_number("heyzo-1234.mp4").is_uncensored_provider
    assert not parse_number("PIYO-170.mp4").is_uncensored_provider


def test_parse_is_total_on_garbage():
    """任何字符串都不许抛异常 —— 这条链路上游是 700 多个真实文件名。"""
    for text in ("", "   ", "???", "\x00", "a" * 500, "【】"):
        assert isinstance(parse_number(text), MovieNumber)
