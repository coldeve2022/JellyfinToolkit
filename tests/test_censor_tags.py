"""破解替换后的有码/无码标签同步测试。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.censor_tags import (  # noqa: E402
    TARGET_TAG,
    join_tag_list,
    normalize_state_tag,
    replace_state_tags,
    split_tag_list,
    update_jellyfin_db_tags,
)
from utils.nfo import update_nfo_censor_tags  # noqa: E402


def test_normalize_state_tag():
    assert normalize_state_tag("有码") == TARGET_TAG
    assert normalize_state_tag("有碼") == TARGET_TAG
    assert normalize_state_tag("无码") == TARGET_TAG
    assert normalize_state_tag("中出") == "中出"


def test_split_join_tag_list():
    assert split_tag_list("有码|中出||制服") == ["有码", "中出", "制服"]
    assert join_tag_list(["无码破解", "中出"]) == "无码破解|中出"


def test_replace_state_tags():
    assert replace_state_tags(["有码", "中出"]) == ["无码破解", "中出"]
    assert replace_state_tags(["无码", "无码破解", "中出"]) == ["无码破解", "中出"]
    assert replace_state_tags(["中出"]) == ["无码破解", "中出"]


def test_update_nfo_censor_tags(tmp_path):
    nfo = tmp_path / "ABC-123.nfo"
    nfo.write_text("<movie><title>ABC-123</title><tag>有码</tag><genre>中出</genre></movie>",
                   encoding="utf-8")
    ok, detail = update_nfo_censor_tags(str(nfo))
    assert ok, detail
    text = nfo.read_text(encoding="utf-8")
    assert "无码破解" in text
    assert "有码" not in text
    assert "中出" in text


def test_update_jellyfin_db_tags(tmp_path):
    db = tmp_path / "jellyfin.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE BaseItems (Id TEXT PRIMARY KEY, Path TEXT, Genres TEXT, Tags TEXT)")
    conn.execute(
        "INSERT INTO BaseItems VALUES (?, ?, ?, ?)",
        ("item-1", str(tmp_path / "ABC-123.mp4"), "有码|中出", "有码"),
    )
    conn.commit()
    conn.close()

    ok, detail = update_jellyfin_db_tags(
        db, str(tmp_path / "ABC-123.mp4"), backup=False)
    assert ok, detail
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT Genres, Tags FROM BaseItems WHERE Id='item-1'").fetchone()
    conn.close()
    assert row[0] == "无码破解|中出"
    assert row[1] == "无码破解"
