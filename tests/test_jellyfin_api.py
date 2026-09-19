"""Jellyfin API 联动 - 清洗纯函数单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.jellyfin_api import (  # noqa: E402
    get_clean_tags_genres, load_dirty_backup, save_dirty_backup,
)


def test_clean_dirty_prefixes():
    item = {
        "Tags": ["有码", "发行: ヤヌス", "巨乳"],
        "Genres": ["片商: OFFICE K'S", "系列: FC2系列", "中出"],
    }
    tags, genres, removed = get_clean_tags_genres(item)
    assert tags == ["有码", "巨乳"]
    assert genres == ["中出"]
    assert "发行: ヤヌス" in removed
    assert "片商: OFFICE K'S" in removed
    assert "系列: FC2系列" in removed


def test_clean_no_dirty():
    item = {"Tags": ["中出", "巨乳"], "Genres": ["素人"]}
    _, _, removed = get_clean_tags_genres(item)
    assert removed == []


def test_clean_drops_empty_strings():
    item = {"Tags": ["", "   ", "发行: x"], "Genres": None}
    tags, genres, removed = get_clean_tags_genres(item)
    assert "发行: x" in removed
    assert all(s and s.strip() for s in tags)
    assert genres == []


def test_backup_roundtrip_preserves_before_values(tmp_path):
    # 清洗前备份必须保留原始 before_tags/before_genres（供回滚）
    dirty = [{
        "item_id": "abc", "name": "FC2-1", "path": r"D:\x\FC2-1.mp4",
        "before_tags": ["无码", "发行: x"], "before_genres": ["中出"],
        "after_tags": ["无码"], "after_genres": ["中出"],
        "removed": ["发行: x"],
    }]
    p = tmp_path / "backup.json"
    assert save_dirty_backup(dirty, str(p)) == 1
    loaded = load_dirty_backup(str(p))
    assert loaded[0]["before_tags"] == ["无码", "发行: x"]
    assert loaded[0]["before_genres"] == ["中出"]
    assert loaded[0]["after_tags"] == ["无码"]
