"""NFO 修复纯函数测试。"""

import os

import pytest

from utils.nfo import (
    fix_nfo_content, nfo_new_filename, read_text_any_encoding, safe_rename,
)


class TestFixNfoContent:
    def test_replace_ts_reference(self):
        content = '<?xml version="1.0"?>\n<movie>\n  <filename>Movie.ts</filename>\n</movie>\n'
        result = fix_nfo_content(content)
        assert result is not None
        assert ".mp4" in result
        assert ".ts" not in result

    def test_no_ts_no_change(self):
        content = "plain nfo without references"
        assert fix_nfo_content(content) is None

    def test_case_insensitive(self):
        content = "Movie.TS"
        assert fix_nfo_content(content) == "Movie.mp4"

    def test_word_boundary(self):
        # ".tst" 不应被误替换
        content = "backup.tst file"
        assert fix_nfo_content(content) is None


class TestNfoNewFilename:
    @pytest.mark.parametrize("name,expected", [
        ("Movie.ts.nfo", "Movie.mp4.nfo"),
        ("Movie.TS.NFO", "Movie.mp4.NFO"),
        ("Movie.ts-poster.jpg", "Movie.mp4-poster.jpg"),
        ("Movie.ts-thumb.jpg", "Movie.mp4-thumb.jpg"),
    ])
    def test_rename(self, name, expected):
        assert nfo_new_filename(name) == expected

    def test_no_change(self):
        assert nfo_new_filename("Movie.mp4.nfo") is None
        assert nfo_new_filename("Movie.nfo") is None


class TestReadTextAnyEncoding:
    def test_utf8(self, tmp_path):
        p = tmp_path / "a.nfo"
        p.write_text("你好 hello", encoding="utf-8")
        assert read_text_any_encoding(str(p)) == "你好 hello"

    def test_gbk(self, tmp_path):
        p = tmp_path / "b.nfo"
        p.write_bytes("中文内容".encode("gbk"))
        result = read_text_any_encoding(str(p))
        assert result == "中文内容"

    def test_missing_file(self):
        assert read_text_any_encoding("nonexistent.nfo") is None


class TestSafeRename:
    def test_basic(self, tmp_path):
        src = tmp_path / "old.nfo"
        dst = tmp_path / "new.nfo"
        src.write_text("x", encoding="utf-8")
        safe_rename(str(src), str(dst))
        assert src.exists() is False
        assert dst.read_text(encoding="utf-8") == "x"

    def test_overwrite_existing(self, tmp_path):
        src = tmp_path / "old.nfo"
        dst = tmp_path / "new.nfo"
        src.write_text("x", encoding="utf-8")
        dst.write_text("y", encoding="utf-8")
        safe_rename(str(src), str(dst))  # 不应抛错
        assert dst.read_text(encoding="utf-8") == "x"

    def test_same_path(self, tmp_path):
        src = tmp_path / "same.nfo"
        src.write_text("x", encoding="utf-8")
        safe_rename(str(src), str(src))  # 幂等
        assert src.read_text(encoding="utf-8") == "x"
