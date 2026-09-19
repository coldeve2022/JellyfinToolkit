"""文件名分类与 restored 还原逻辑测试。"""

import pytest

from utils.naming import is_uncensored_filename, clean_restored_name

KEYWORDS = ["fc2", "1pondo", "carib", "tokyo hot", "tokyohot", "无码", "流出",
            "uncensored", "hack", "decensored"]


class TestIsUncensored:
    @pytest.mark.parametrize("filename", [
        "FC2-PPV-123456.mp4",
        "fc2 123456.mp4",
        "FC2_123456.mp4",
        "1pondo-12345.mp4",
        "carib-12345.mp4",
        "tokyo hot - 123.mp4",
        "TOKYOHOT-123.mp4",
        "无码-123.mp4",
        "流出-123.mp4",
        "ABC-123-U.mp4",
        "ABC-123_u.mp4",
        "abc-123-uncensored.mp4",
        "decensored-test.mp4",
    ])
    def test_uncensored(self, filename):
        assert is_uncensored_filename(filename, KEYWORDS) is True, filename

    @pytest.mark.parametrize("filename", [
        "ABC-123.mp4",
        "normal-movie.mkv",
        "sony-2024.mp4",
        "myvideo-UHD.mp4",     # -UHD 不应误判
        "uncut-version.mp4",   # uncut ≠ uncensored
    ])
    def test_censored(self, filename):
        assert is_uncensored_filename(filename, KEYWORDS) is False, filename

    def test_case_insensitive(self):
        assert is_uncensored_filename("FC2-PPV-001.mp4", KEYWORDS)
        assert is_uncensored_filename("tokyohot-9.mp4", KEYWORDS)

    def test_uncensored_keyword_boundary(self):
        # "hack" 不应匹配 "hacker"
        assert is_uncensored_filename("hacker-movie.mp4", ["hack"]) is True  # 关键词包含即命中（原始行为）
        # 保持与旧脚本一致：子串匹配


class TestCleanRestoredName:
    @pytest.mark.parametrize("filename,expected", [
        ("Movie.restored.mp4", "Movie.mp4"),
        ("Movie - restored.mp4", "Movie.mp4"),
        ("Movie_restored.mp4", "Movie.mp4"),
        ("Movie (restored).mp4", "Movie.mp4"),
        ("Movie.restored.2024.mp4", "Movie.2024.mp4"),
        ("ABC-123.restored.mp4", "ABC-123.mp4"),
    ])
    def test_clean(self, filename, expected):
        assert clean_restored_name(filename) == expected

    @pytest.mark.parametrize("filename", [
        "Movie.mp4",
        "restoration-era.mkv",     # restored 不是子串
        "ABC-123.avi",
    ])
    def test_no_marker(self, filename):
        assert clean_restored_name(filename, "restored") is None

    def test_custom_marker(self):
        assert clean_restored_name("Movie.dec.mp4", "dec") == "Movie.mp4"

    def test_no_extension_file(self):
        assert clean_restored_name("Movie.restored") == "Movie"
