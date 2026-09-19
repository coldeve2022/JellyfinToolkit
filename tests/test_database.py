"""跨库播放记录转移逻辑测试（使用临时 SQLite 数据库）。"""

import sqlite3

import pytest

from workers.database import transfer_userdata


def _make_db(path, ts_paths, mp4_paths=None, userdata=None):
    """构造模拟 Jellyfin 数据库。

    Args:
        path: 数据库文件路径
        ts_paths: 需要插入 BaseItems 的 .ts 路径列表
        mp4_paths: 需要插入 BaseItems 的 .mp4 路径列表（默认与 ts 对应）
        userdata: {item_path: [(userId, ...)]} 或 None
    """
    conn = sqlite3.connect(str(path))
    c = conn.cursor()
    c.execute("CREATE TABLE BaseItems (Id TEXT PRIMARY KEY, Path TEXT, PresentationUniqueKey TEXT)")
    c.execute(
        "CREATE TABLE UserData (ItemId TEXT, UserId TEXT, CustomDataKey TEXT, "
        "AudioStreamIndex INTEGER, IsFavorite INTEGER, LastPlayedDate TEXT, "
        "Likes INTEGER, PlayCount INTEGER, PlaybackPositionTicks INTEGER, "
        "Played INTEGER, Rating REAL, SubtitleStreamIndex INTEGER, RetentionDate TEXT)"
    )

    item_ids = {}
    for p in ts_paths:
        iid = f"ts-{len(item_ids)}"
        c.execute("INSERT INTO BaseItems VALUES (?, ?, ?)", (iid, p, None))
        item_ids[p] = iid
    for p in (mp4_paths or []):
        iid = f"mp4-{len(item_ids)}"
        c.execute("INSERT INTO BaseItems VALUES (?, ?, ?)", (iid, p, None))
        item_ids[p] = iid

    if userdata:
        for p, rows in userdata.items():
            iid = item_ids[p]
            for ud in rows:
                # (userId, CustomDataKey, AudioStreamIndex, IsFavorite, LastPlayedDate,
                #  Likes, PlayCount, PlaybackPositionTicks, Played, Rating,
                #  SubtitleStreamIndex, RetentionDate)
                c.execute(
                    "INSERT INTO UserData VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (iid, *ud),
                )
    conn.commit()
    conn.close()
    return item_ids


def _read_userdata(path, item_id=None):
    conn = sqlite3.connect(str(path))
    c = conn.cursor()
    if item_id:
        c.execute("SELECT * FROM UserData WHERE ItemId = ?", (item_id,))
    else:
        c.execute("SELECT * FROM UserData")
    rows = c.fetchall()
    conn.close()
    return rows


def test_transfer_basic(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/Movie.ts"], userdata={
        "/lib/Movie.ts": [("user1", None, 1, 1, "2024-01-01", 0, 5,
                          100000, 1, 5.0, 1, None)],
    })
    ids = _make_db(new, [], mp4_paths=["/lib/Movie.mp4"])

    logs = []
    stats = transfer_userdata(old, new, log=logs.append)
    assert stats["matched"] == 1
    assert stats["transferred"] == 1
    assert stats["not_found"] == 0

    rows = _read_userdata(new, ids["/lib/Movie.mp4"])
    assert len(rows) == 1
    assert rows[0][1] == "user1"          # UserId
    assert rows[0][4] == 1                # IsFavorite
    assert rows[0][8] == 100000           # PlaybackPositionTicks


def test_transfer_multi_user(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/A.ts"], userdata={
        "/lib/A.ts": [
            ("u1", None, 1, 1, "2024-01-01", 0, 3, 500, 1, 4.0, 1, None),
            ("u2", None, 0, 0, None, 0, 1, 0, 0, None, 0, None),
        ],
    })
    ids = _make_db(new, [], mp4_paths=["/lib/A.mp4"])
    stats = transfer_userdata(old, new)
    assert stats["transferred"] == 2
    assert len(_read_userdata(new, ids["/lib/A.mp4"])) == 2


def test_transfer_not_found(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/Movie.ts"])
    _make_db(new, [], mp4_paths=["/lib/Other.mp4"])  # 新库中无对应 mp4
    stats = transfer_userdata(old, new)
    assert stats["matched"] == 0
    assert stats["not_found"] == 1


def test_transfer_clears_existing_empty_record(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/Movie.ts"], userdata={
        "/lib/Movie.ts": [("user1", None, 1, 1, "2024-01-01", 0, 5, 100, 1, 5.0, 1, None)],
    })
    ids = _make_db(new, [], mp4_paths=["/lib/Movie.mp4"], userdata={
        "/lib/Movie.mp4": [("user1", None, 0, 0, None, 0, 0, 0, 0, None, 0, None)],
    })
    transfer_userdata(old, new)
    rows = _read_userdata(new, ids["/lib/Movie.mp4"])
    # 旧记录覆盖新空记录，而不是新增重复
    assert len(rows) == 1
    assert rows[0][8] == 100


def test_progress_callback(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/A.ts", "/lib/B.ts", "/lib/C.ts"])
    _make_db(new, [], mp4_paths=["/lib/A.mp4", "/lib/B.mp4", "/lib/C.mp4"])
    progresses = []
    transfer_userdata(old, new, progress=progresses.append)
    assert progresses == [33, 66, 100]


def test_stop_flag(tmp_path):
    old = tmp_path / "old.db"
    new = tmp_path / "new.db"
    _make_db(old, ["/lib/A.ts", "/lib/B.ts", "/lib/C.ts"])
    _make_db(new, [], mp4_paths=["/lib/A.mp4", "/lib/B.mp4", "/lib/C.mp4"])
    stats = transfer_userdata(old, new, should_stop=lambda: True)
    assert stats["matched"] == 0  # 第一轮即停止


def test_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        transfer_userdata(tmp_path / "nope.db", tmp_path / "nope2.db")
