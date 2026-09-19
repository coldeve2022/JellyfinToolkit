"""utils.library.find_cover 纯函数单元测试 — 本地封面查找。"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from utils.library import find_cover, Item  # noqa: E402


@pytest.fixture
def media(tmp_path: Path):
    """构造一个含视频与封面候选的目录。"""
    d = tmp_path / "片库"
    d.mkdir()
    video = d / "MIAA-743 制服.mp4"
    video.write_bytes(b"x")
    return d, video


def test_matches_num_poster(media):
    d, video = media
    (d / "MIAA-743-poster.jpg").write_bytes(b"x")
    assert find_cover(str(video), num="MIAA-743") == str(d / "MIAA-743-poster.jpg")


def test_loose_norm_match(media):
    """番号带分隔符差异也应命中（MIAA-743 vs miaa743.jpg）。"""
    d, video = media
    (d / "miaa743.jpg").write_bytes(b"x")
    assert find_cover(str(video), num="MIAA-743") == str(d / "miaa743.jpg")


def test_cd_prefers_cd_index(media):
    d, video = media
    (d / "FC2-3581881-poster.jpg").write_bytes(b"x")
    (d / "FC2-3581881-cd4-poster.jpg").write_bytes(b"x")
    # cd_index=4 → 命中 cd4 版本
    assert find_cover(str(video), num="FC2-3581881", cd_index=4) == str(
        d / "FC2-3581881-cd4-poster.jpg")
    # 无 cd 索引 → 命中基础版
    assert find_cover(str(video), num="FC2-3581881") == str(d / "FC2-3581881-poster.jpg")


def test_falls_back_to_folder_jpg(media):
    d, video = media
    (d / "folder.jpg").write_bytes(b"x")
    assert find_cover(str(video), num="MIAA-743") == str(d / "folder.jpg")


def test_excludes_backdrop_fanart(media):
    """fanart/backdrop 干扰图不该被当成封面。"""
    d, video = media
    (d / "fanart1.jpg").write_bytes(b"x")
    (d / "backdrop.png").write_bytes(b"x")
    # 仅剩干扰图，无法确定 → 返回 None（不误用 fanart）
    assert find_cover(str(video), num="MIAA-743") is None


def test_single_candidate_fallback(media):
    d, video = media
    (d / "任意图.png").write_bytes(b"x")
    assert find_cover(str(video), num=None) == str(d / "任意图.png")


def test_no_image_returns_none(media):
    d, video = media
    assert find_cover(str(video), num="MIAA-743") is None


def test_bad_path_returns_none():
    assert find_cover("Z:/不存在/视频.mp4", num="MIAA-743") is None
    assert find_cover("", num="A-1") is None


def test_item_has_cover_property():
    it = Item(id="x", name="N", path="P", type_cn="Movie", year=None, cover="")
    assert not it.has_cover
    it.cover = "Z:/no.jpg"
    assert not it.has_cover  # 文件不存在


# ── Jellyfin 主封面(ImageType=0) 接入 ──────────────────────────────
import sqlite3  # noqa: E402
from utils.library import load_primary_covers, collect_library  # noqa: E402


def _mk_db(tmp_path, rows, extra_videos=None):
    """构造迷你 jellyfin.db：BaseItemImageInfos(ImageType=0 主封面) + BaseItems。"""
    db = tmp_path / "jellyfin.db"
    conn = sqlite3.connect(db)
    c = conn.cursor()
    c.execute("CREATE TABLE BaseItemImageInfos (Id TEXT, ItemId TEXT, ImageType INTEGER, Path TEXT)")
    c.executemany("INSERT INTO BaseItemImageInfos (ItemId, ImageType, Path) VALUES (?,?,?)", rows)
    if extra_videos:
        c.execute("CREATE TABLE BaseItems (Id TEXT, Name TEXT, Path TEXT, ProductionYear INTEGER, "
                  "Genres TEXT, Tags TEXT, Studios TEXT, Size INTEGER, Type TEXT, "
                  "IsFolder INTEGER, MediaType TEXT)")
        c.executemany("INSERT INTO BaseItems (Id, Name, Path, ProductionYear, Genres, Tags, Studios, "
                      "Size, Type, IsFolder, MediaType) VALUES (?,?,?,?,?,?,?,?,?,0,'Video')", extra_videos)
    # collect_library 全量加载还需要这几张表，建空表防报错
    c.execute("CREATE TABLE PeopleBaseItemMap (ItemId TEXT, PeopleId TEXT)")
    c.execute("CREATE TABLE Peoples (Id TEXT, Name TEXT)")
    c.execute("CREATE TABLE UserData (ItemId TEXT, IsFavorite INTEGER, PlayCount INTEGER, "
              "Played INTEGER, PlaybackPositionTicks INTEGER, LastPlayedDate TEXT, Rating REAL)")
    conn.commit()
    conn.close()
    return db


def test_load_primary_covers_only_existing_local(tmp_path):
    """只返回本地存在的主封面，跳过 URL 与失效路径。"""
    d = tmp_path / "媒体"
    d.mkdir()
    poster = d / "MIAA-743-poster.jpg"
    poster.write_bytes(b"x")
    db = _mk_db(tmp_path, [
        ("id1", 0, str(poster)),                    # 本地存在 → 命中
        ("id2", 0, "https://cdn/xxx/poster.jpg"),   # URL → 跳过
        ("id3", 0, str(d / "不存在.jpg")),          # 失效路径 → 跳过
        ("id1", 2, str(poster)),                    # fanart 不在此表逻辑里(ImageType=0 才纳入)
    ])
    m = load_primary_covers(str(db))
    assert m.get("id1") == str(poster)
    assert "id2" not in m
    assert "id3" not in m


def test_collect_library_prefers_jellyfin_cover(tmp_path):
    """collect_library 封面包优先级：Jellyfin 主封面 > 视频目录 sidecar 盲猜。"""
    d = tmp_path / "片库"
    d.mkdir()
    video = d / "MIAA-743 制服.mp4"
    video.write_bytes(b"x")
    # 视频目录里放一个错误的 sidecar（fanart），Jellyfin 主封面才是真海报
    (d / "MIAA-743-fanart.jpg").write_bytes(b"x")
    (d / "MIAA-743-poster.jpg").write_bytes(b"x")   # 真海报
    db = _mk_db(tmp_path, [
        ("VID1", 0, str(d / "MIAA-743-poster.jpg")),
    ], extra_videos=[
        ("VID1", "MIAA-743 制服", str(video), 2020, "", "", "", 100, "Movie"),
    ])
    items = collect_library(str(db))
    assert len(items) == 1
    it = items[0]
    assert it.cover == str(d / "MIAA-743-poster.jpg")  # Jellyfin 主封面优先
    assert it.has_cover


def test_load_primary_covers_falls_back_to_screenshot(tmp_path):
    """无主封面时，回退使用 Jellyfin 视频帧截图(ImageType=5)。"""
    d = tmp_path / "媒体"
    d.mkdir()
    shot = d / "FC2-3581881-thumb.jpg"
    shot.write_bytes(b"x")
    db = _mk_db(tmp_path, [("id1", 5, str(shot))])
    m = load_primary_covers(str(db))
    assert m.get("id1") == str(shot)


def test_load_primary_covers_prefers_primary_over_screenshot(tmp_path):
    """主封面与截图同时存在时，优先主封面(ImageType=0)。"""
    d = tmp_path / "媒体"
    d.mkdir()
    poster = d / "poster.jpg"
    poster.write_bytes(b"x")
    shot = d / "shot.jpg"
    shot.write_bytes(b"x")
    db = _mk_db(tmp_path, [("id1", 5, str(shot)), ("id1", 0, str(poster))])
    m = load_primary_covers(str(db))
    assert m.get("id1") == str(poster)


def test_collect_library_uses_nfo_title_for_bare_code(tmp_path):
    """Jellyfin 名称只有裸番号时，回退读取同目录 NFO 标题。"""
    d = tmp_path / "片库"
    d.mkdir()
    video = d / "FC2-3581881.mp4"
    video.write_bytes(b"x")
    (d / "FC2-3581881.nfo").write_text(
        "<movie><title>FC2-3581881 某标题</title></movie>", encoding="utf-8")
    db = _mk_db(tmp_path, [], extra_videos=[
        ("VID1", "FC2-3581881", str(video), 2020, "", "", "", 100, "Movie"),
    ])
    items = collect_library(str(db))
    assert len(items) == 1
    assert items[0].name == "FC2-3581881 某标题"

