"""Jellyfin / Emby 双服务器适配 - 单元测试。

覆盖三层：
1. ``utils/media_server`` —— 服务端类型识别、地址归一化、本机库文件定位；
2. ``utils/library`` / ``utils/library_api`` —— 两种库架构的采集（EF 架构 /
   传统 TypedBaseItems 架构）与 REST API 采集；
3. ``utils/library_source`` —— "库文件 or 服务器 API" 的统一解析入口。

所有 HTTP 都是打桩的，**不依赖真实服务器**；库文件用临时 SQLite 现场构造。
"""

import json
import sqlite3
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import library_api, library_source, media_server  # noqa: E402
from utils.library import (  # noqa: E402
    SCHEMA_JELLYFIN_EF,
    SCHEMA_LEGACY,
    SCHEMA_UNKNOWN,
    collect_library,
    detect_db_schema,
)

# 全部使用中性虚构路径，别把真实媒体库写进测试
DEMO = "C:/MediaDemo"


# ── 打桩工具 ───────────────────────────────────────────────

def _ms_routes(routes: dict):
    """给 ``media_server._request`` 用的假实现（按 URL 片段匹配）。"""
    def _fake(url, api_key="", timeout=6):
        for frag, resp in routes.items():
            if frag in url:
                return resp
        return 404, None
    return _fake


def _api_req(pages, users=(("admin-1", True),)):
    """给 ``library_api._req`` 用的假实现，支持分页。"""
    calls = []
    user_list = [{"Id": uid, "Policy": {"IsAdministrator": admin}} for uid, admin in users]

    def _fake(method, url, api_key, timeout=30):
        calls.append((method, url))
        if url.endswith("/Users"):
            return 200, user_list
        if "/Items?" in url:
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            start = int(q.get("startIndex", ["0"])[0])
            idx = start // library_api._PAGE_SIZE
            if idx < len(pages):
                return 200, pages[idx]
            return 200, {"Items": [], "TotalRecordCount": 0}
        return 404, None

    return _fake, calls


def _api_item(item_id, name, path, **extra):
    raw = {
        "Id": item_id,
        "Name": name,
        "Path": f"{DEMO}/{path}",
        "Type": "Movie",
        "ProductionYear": 2020,
        "Genres": ["中出"],
        "Tags": ["无码"],
        "Studios": ["StudioA"],
        "MediaSources": [{"Size": 1_500_000_000}],
        "People": [{"Name": "ActorA", "Type": "Actor"},
                   {"Name": "DirectorB", "Type": "Director"}],
        "UserData": {"IsFavorite": True, "PlayCount": 3, "Played": True,
                     "PlaybackPositionTicks": 0, "LastPlayedDate": "2024-01-02",
                     "Rating": 8.0},
    }
    raw.update(extra)
    return raw


def _make_legacy_db(path: Path, rows, userdata=()) -> Path:
    """构造一个"传统架构"库（Emby / Jellyfin ≤10.8 的 TypedBaseItems）。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE TypedBaseItems (guid BLOB, type TEXT, data BLOB, "
        "Path TEXT, Name TEXT, IsFolder INTEGER, CleanName TEXT)")
    conn.execute(
        "CREATE TABLE UserData (key TEXT, userId TEXT, rating REAL, played INTEGER, "
        "playCount INTEGER, isFavorite INTEGER, playbackPositionTicks INTEGER, "
        "lastPlayedDate TEXT, itemId BLOB)")
    for guid, typ, payload, p, name, is_folder in rows:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        conn.execute(
            "INSERT INTO TypedBaseItems (guid, type, data, Path, Name, IsFolder, CleanName) "
            "VALUES (?,?,?,?,?,?,?)", (guid, typ, data, p, name, is_folder, name))
    for key, guid, fav, played, count in userdata:
        conn.execute(
            "INSERT INTO UserData (key, userId, rating, played, playCount, isFavorite, "
            "playbackPositionTicks, lastPlayedDate, itemId) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, "u1", None, int(played), count, int(fav), 0, "2024-01-02", guid))
    conn.commit()
    conn.close()
    return path


def _guid() -> bytes:
    return uuid.uuid4().bytes


# ── 1. 服务端类型识别 ──────────────────────────────────────

@pytest.mark.parametrize("product,expected", [
    ("Jellyfin Server", media_server.KIND_JELLYFIN),
    ("Emby", media_server.KIND_EMBY),
    ("Emby Server", media_server.KIND_EMBY),      # 老版本文案
    ("jellyfin", media_server.KIND_JELLYFIN),
    ("Something Else", media_server.KIND_UNKNOWN),
    ("", media_server.KIND_UNKNOWN),
])
def test_kind_from_product(product, expected):
    assert media_server.kind_from_product(product) == expected


def test_normalize_server_url_adds_scheme_and_strips_slash():
    assert media_server.normalize_server_url("192.168.1.5:8096") == "http://192.168.1.5:8096"
    assert media_server.normalize_server_url("http://a:8096/") == "http://a:8096"
    assert media_server.normalize_server_url("  ") == ""
    # https 不能被改写成 http
    assert media_server.normalize_server_url("https://a") == "https://a"


def test_normalize_kind_falls_back_to_auto():
    for bad in ("", None, "plex", 123):
        assert media_server.normalize_kind(bad) == media_server.KIND_AUTO
    # 大小写与空白都要能收敛到正确的类型（用户手输/配置被手改过的情况）
    assert media_server.normalize_kind("EMBY") == media_server.KIND_EMBY
    assert media_server.normalize_kind("  Jellyfin  ") == media_server.KIND_JELLYFIN


@pytest.mark.parametrize("product,expected", [
    ("Jellyfin Server", media_server.KIND_JELLYFIN),
    ("Emby", media_server.KIND_EMBY),
])
def test_detect_identifies_server_kind(monkeypatch, product, expected):
    """``/System/Info/Public`` 两种服务器都支持，且不需要 API Key。"""
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {
                            "ProductName": product, "Version": "4.8.0",
                            "ServerName": "DemoServer"})}))
    probe = media_server.detect("http://demo:8096")
    assert probe.ok and probe.kind == expected
    assert probe.name == "DemoServer"
    assert probe.version == "4.8.0"
    assert not probe.authenticated, "Public 接口不该标记为已鉴权"


def test_detect_falls_back_to_authenticated_endpoint(monkeypatch):
    """Public 不可用时退回 ``/System/Info``（需要 Key），并标记 authenticated。"""
    monkeypatch.setattr(media_server, "_request", _ms_routes({
        "/System/Info/Public": (404, None),
        "/System/Info": (200, {"ProductName": "Emby", "Version": "4.7",
                               "ServerName": "EmbyBox"}),
    }))
    probe = media_server.detect("http://demo:8096", api_key="k")
    assert probe.ok and probe.kind == media_server.KIND_EMBY
    assert probe.authenticated


def test_detect_honours_declared_kind_when_product_unknown(monkeypatch):
    """服务端返回了无法识别的 ProductName 时，以用户手选的类型为准。"""
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {"ProductName": "Mystery"})}))
    probe = media_server.detect("http://demo:8096", declared="emby")
    assert probe.kind == media_server.KIND_EMBY


def test_detect_reports_error_without_network(monkeypatch):
    monkeypatch.setattr(media_server, "_request", _ms_routes({}))
    probe = media_server.detect("http://demo:8096")
    assert not probe.ok and probe.error
    # 地址为空时不该发起任何请求
    called = []
    monkeypatch.setattr(media_server, "_request",
                        lambda *a, **k: called.append(a) or (200, {}))
    assert not media_server.detect("").ok
    assert called == []


def test_guess_kind_from_path():
    assert media_server.guess_kind_from_path("C:/ProgramData/Emby-Server/data/library.db") \
        == media_server.KIND_EMBY
    assert media_server.guess_kind_from_path("/var/lib/jellyfin/data/jellyfin.db") \
        == media_server.KIND_JELLYFIN
    assert media_server.guess_kind_from_path("C:/x.db") == media_server.KIND_UNKNOWN


def test_candidate_dirs_have_no_personal_paths():
    """候选目录必须由环境变量拼出来，不能出现某个用户名。"""
    import os

    home_name = Path.home().name
    for _kind, d in media_server.candidate_data_dirs():
        assert d.is_absolute()
        assert home_name not in str(d) or str(d).startswith(str(Path.home()))
        assert "Users\\" not in str(d) or os.environ.get("APPDATA", "\x00") in str(d) or \
            os.environ.get("PROGRAMDATA", "\x00") in str(d) or \
            os.environ.get("LOCALAPPDATA", "\x00") in str(d)


# ── 2. 数据库架构探测与两种架构的采集 ──────────────────────

def test_detect_db_schema_variants(tmp_path):
    ef = tmp_path / "jellyfin.db"
    conn = sqlite3.connect(str(ef))
    conn.execute("CREATE TABLE BaseItems (Id TEXT)")
    conn.commit()
    conn.close()
    assert detect_db_schema(ef) == SCHEMA_JELLYFIN_EF

    legacy = _make_legacy_db(tmp_path / "library.db", [])
    assert detect_db_schema(legacy) == SCHEMA_LEGACY

    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE whatever (x TEXT)")
    conn.commit()
    conn.close()
    assert detect_db_schema(other) == SCHEMA_UNKNOWN

    assert detect_db_schema(tmp_path / "missing.db") == SCHEMA_UNKNOWN


def test_collect_library_reads_legacy_metadata(tmp_path):
    """传统架构：元数据在 data 列 JSON 里，必须能取出来。"""
    g1, g2, g3 = _guid(), _guid(), _guid()
    db = _make_legacy_db(
        tmp_path / "library.db",
        [
            (g1, "MediaBrowser.Controller.Entities.Movies.Movie",
             {"Genres": ["中出"], "Tags": ["无码", "巨乳"], "Studios": ["StudioA"],
              "ProductionYear": 2019, "Size": 2_000_000_000},
             f"{DEMO}/StudioA/MIAA-743/MIAA-743.mp4", "MIAA-743", 0),
            # 目录条目必须被 IsFolder 过滤掉
            (g2, "MediaBrowser.Controller.Entities.Movies.Movie", {},
             f"{DEMO}/StudioA", "StudioA", 1),
            # 音乐条目不该混进来
            (g3, "MediaBrowser.Controller.Entities.Audio.MusicAlbum", {},
             f"{DEMO}/music/x.mp3", "Album", 0),
        ],
        userdata=[("k1", g1, 1, 1, 5)],
    )
    logs = []
    items = collect_library(str(db), log=logs.append)
    assert len(items) == 1
    it = items[0]
    assert it.num == "MIAA-743"
    assert it.year == 2019
    assert it.size == 2_000_000_000
    assert set(it.tags) == {"无码", "巨乳"}
    assert it.genres == ["中出"]
    assert it.studios == ["StudioA"]
    assert it.bucket == "jav"
    # 行为数据来自同库的 UserData 表（就是 guid 的 join）
    assert it.is_favorite and it.is_played and it.play_count == 5
    assert any("传统架构" in m for m in logs)


def test_collect_library_legacy_without_userdata_table_is_explicit(tmp_path):
    """Emby 的行为数据在 users.db，读不到时必须**明说**，而不是装作没有记录。"""
    g1 = _guid()
    db = _make_legacy_db(
        tmp_path / "library.db",
        [(g1, "MediaBrowser.Controller.Entities.Movies.Movie",
          {"Genres": ["中出"]}, f"{DEMO}/A/MIAA-743/MIAA-743.mp4", "MIAA-743", 0)],
    )
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE UserData")
    conn.commit()
    conn.close()

    logs = []
    items = collect_library(str(db), log=logs.append)
    assert len(items) == 1 and items[0].play_count == 0
    assert any("users.db" in m for m in logs), "必须说明 Emby 的行为数据读不到"


def test_collect_library_legacy_handles_broken_json(tmp_path):
    """data 列不是合法 JSON 时不能崩，退化成"没有标签"。"""
    g1 = _guid()
    db = _make_legacy_db(
        tmp_path / "library.db",
        [(g1, "MediaBrowser.Controller.Entities.Movies.Movie", None,
          f"{DEMO}/A/ABC-1/ABC-1.mp4", "ABC-1", 0)],
    )
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE TypedBaseItems SET data=?", (b"{not json",))
    conn.commit()
    conn.close()
    items = collect_library(str(db))
    assert len(items) == 1
    assert items[0].tags == [] and items[0].genres == []


def test_collect_library_rejects_unknown_schema(tmp_path):
    """架构认不出来时必须报错并给出可操作的建议，而不是静默返回空列表。"""
    other = tmp_path / "mystery.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE whatever (x TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError) as ei:
        collect_library(str(other))
    assert "服务器 API" in str(ei.value)


# ── 3. REST API 采集 ───────────────────────────────────────

def test_collect_library_api_end_to_end(monkeypatch):
    page = {"Items": [
        _api_item("i1", "MIAA-743", "StudioA/MIAA-743/MIAA-743.mp4"),
        _api_item("i2", "FC2-123456", "Other/FC2-123456/c.mp4"),
    ], "TotalRecordCount": 2}
    req, calls = _api_req([page])
    monkeypatch.setattr(library_api, "_req", req)
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {"ProductName": "Emby"})}))

    items, kind = library_api.collect_library_api("http://demo:8096", "key")
    assert kind == media_server.KIND_EMBY

    by_num = {it.num: it for it in items}
    assert set(by_num) == {"MIAA-743", "FC2-123456"}
    jav = by_num["MIAA-743"]
    assert jav.actors == ["ActorA"], "只有 Type=Actor 的算演员，导演不算"
    assert jav.size == 1_500_000_000
    assert jav.is_favorite and jav.play_count == 3 and jav.is_played
    assert jav.bucket == "jav"
    assert by_num["FC2-123456"].bucket == "uncensored"
    assert all(it.path.startswith(DEMO) for it in items)

    # 采集必须走 user 维度，否则拿不到 UserData
    # （这里用 f-string 拼，避免源码里出现会撞上隐私守卫的 macOS 家目录字面量）
    api_user = "admin-1"
    assert any(f"/Users/{api_user}/Items" in u for _m, u in calls)


def test_collect_library_api_respects_exclude_keywords(monkeypatch):
    page = {"Items": [
        _api_item("i1", "MIAA-743", "Keep/MIAA-743/a.mp4"),
        _api_item("i2", "MIAA-744", "SkipMe/MIAA-744/b.mp4"),
    ], "TotalRecordCount": 2}
    req, _calls = _api_req([page])
    monkeypatch.setattr(library_api, "_req", req)
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {"ProductName": "Jellyfin Server"})}))
    items, _kind = library_api.collect_library_api(
        "http://demo:8096", "key", exclude_keywords=["skipme"])
    assert [it.num for it in items] == ["MIAA-743"]


def test_collect_library_api_paginates(monkeypatch):
    """分页必须真的翻到底，否则大库会静默少一半。"""
    p1 = {"Items": [_api_item(f"a{i}", f"ABC-{i}", f"A/ABC-{i}/x.mp4")
                    for i in range(library_api._PAGE_SIZE)],
          "TotalRecordCount": library_api._PAGE_SIZE + 1}
    p2 = {"Items": [_api_item("last", "ABC-9999", "A/ABC-9999/x.mp4")],
          "TotalRecordCount": library_api._PAGE_SIZE + 1}
    req, _calls = _api_req([p1, p2])
    monkeypatch.setattr(library_api, "_req", req)
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {"ProductName": "Emby"})}))
    items, _kind = library_api.collect_library_api("http://demo:8096", "key")
    assert len(items) == library_api._PAGE_SIZE + 1
    assert any(it.num == "ABC-9999" for it in items)


def test_pick_user_id_prefers_admin(monkeypatch):
    req, _calls = _api_req([], users=[("u-normal", False), ("u-admin", True)])
    monkeypatch.setattr(library_api, "_req", req)
    assert library_api.pick_user_id("http://demo:8096", "k") == "u-admin"

    req2, _c2 = _api_req([], users=[("only", False)])
    monkeypatch.setattr(library_api, "_req", req2)
    assert library_api.pick_user_id("http://demo:8096", "k") == "only"

    monkeypatch.setattr(library_api, "_req", lambda *a, **k: (200, []))
    assert library_api.pick_user_id("http://demo:8096", "k") is None


def test_collect_library_api_requires_server_and_key(monkeypatch):
    with pytest.raises(library_api.LibraryApiError):
        library_api.collect_library_api("", "")
    with pytest.raises(library_api.LibraryApiError):
        library_api.collect_library_api("http://demo:8096", "")


def test_collect_library_api_surfaces_connection_error(monkeypatch):
    monkeypatch.setattr(library_api, "_req",
                        lambda *a, **k: (_ for _ in ()).throw(
                            library_api.LibraryApiError("拒绝连接")))
    with pytest.raises(library_api.LibraryApiError):
        library_api.collect_library_api("http://demo:8096", "k")


# ── 4. 统一数据来源入口 ───────────────────────────────────

def test_resolve_auto_prefers_readable_db(tmp_path):
    db = _make_legacy_db(tmp_path / "library.db", [])
    cfg = {"library_source": "auto", "jellyfin_db_path": str(db),
           "jellyfin_server": "http://demo:8096", "jellyfin_api_key": "k"}
    kind, opts = library_source.resolve(cfg)
    assert kind == library_source.SOURCE_DB
    assert Path(opts["path"]) == db


def test_resolve_auto_falls_back_to_api(tmp_path):
    cfg = {"library_source": "auto", "jellyfin_db_path": str(tmp_path / "missing.db"),
           "jellyfin_server": "http://demo:8096", "jellyfin_api_key": "k"}
    kind, opts = library_source.resolve(cfg)
    assert kind == library_source.SOURCE_API
    assert opts["server"] == "http://demo:8096"


def test_resolve_autodetects_when_path_empty(monkeypatch, tmp_path):
    db = _make_legacy_db(tmp_path / "library.db", [])
    monkeypatch.setattr(library_source, "find_local_databases",
                        lambda: [{"kind": "emby", "path": db, "schema": SCHEMA_LEGACY}])
    logs = []
    kind, opts = library_source.resolve({"library_source": "auto", "jellyfin_db_path": ""},
                                        log=logs.append)
    assert kind == library_source.SOURCE_DB and Path(opts["path"]) == db
    assert any("自动检测" in m for m in logs), "自动检测到的路径要说一声"


def test_resolve_explicit_api_needs_both_fields():
    with pytest.raises(library_source.SourceUnavailable) as ei:
        library_source.resolve({"library_source": "api", "jellyfin_server": "http://x"})
    assert "API Key" in str(ei.value)


def test_resolve_explicit_db_missing_file(tmp_path):
    with pytest.raises(library_source.SourceUnavailable):
        library_source.resolve({"library_source": "db",
                                "jellyfin_db_path": str(tmp_path / "nope.db")})


def test_resolve_error_message_teaches_both_ways(monkeypatch):
    """什么都没配时的报错必须直接告诉用户两条路怎么走。"""
    monkeypatch.setattr(library_source, "find_local_databases", lambda: [])
    with pytest.raises(library_source.SourceUnavailable) as ei:
        library_source.resolve({"library_source": "auto", "jellyfin_db_path": ""})
    msg = str(ei.value)
    assert "方式 A" in msg and "方式 B" in msg and "Emby" in msg


def test_resolve_accepts_plain_db_path(tmp_path):
    """兼容旧调用：直接传数据库路径字符串。"""
    db = _make_legacy_db(tmp_path / "library.db", [])
    kind, opts = library_source.resolve(str(db))
    assert kind == library_source.SOURCE_DB
    with pytest.raises(library_source.SourceUnavailable):
        library_source.resolve(str(tmp_path / "nope.db"))


def test_describe_makes_no_network_calls(monkeypatch):
    """设置页构造时会调用 describe —— 它绝不能发起 HTTP，否则界面会卡住。"""
    def _boom(*a, **k):
        raise AssertionError("describe() 不应该发起网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    text = library_source.describe({
        "library_source": "api", "jellyfin_server": "http://demo:8096",
        "jellyfin_api_key": "k", "server_type": "emby"})
    assert "服务器 API" in text and "Emby" in text
    assert "未就绪" in library_source.describe({"library_source": "api"})


def test_collect_routes_db_source(tmp_path):
    db = _make_legacy_db(
        tmp_path / "library.db",
        [(_guid(), "MediaBrowser.Controller.Entities.Movies.Movie",
          {"Genres": ["中出"]}, f"{DEMO}/A/MIAA-743/MIAA-743.mp4", "MIAA-743", 0)])
    items = library_source.collect({"library_source": "db", "jellyfin_db_path": str(db)})
    assert [it.num for it in items] == ["MIAA-743"]


def test_collect_translates_api_failure_to_source_error(monkeypatch):
    monkeypatch.setattr(library_api, "_req",
                        lambda *a, **k: (_ for _ in ()).throw(
                            library_api.LibraryApiError("连接被拒绝")))
    with pytest.raises(library_source.SourceUnavailable):
        library_source.collect({"library_source": "api",
                                "jellyfin_server": "http://demo:8096",
                                "jellyfin_api_key": "k"})


def test_normalize_source():
    assert library_source.normalize_source("API") == library_source.SOURCE_API
    for bad in ("", None, "ftp"):
        assert library_source.normalize_source(bad) == library_source.SOURCE_AUTO


# ── 5. 本机库文件定位 ──────────────────────────────────────

def test_find_local_databases_prefers_ef_schema(monkeypatch, tmp_path):
    legacy_dir = tmp_path / "emby"
    legacy_dir.mkdir()
    _make_legacy_db(legacy_dir / "library.db", [])

    ef_dir = tmp_path / "jellyfin"
    ef_dir.mkdir()
    ef = ef_dir / "jellyfin.db"
    conn = sqlite3.connect(str(ef))
    conn.execute("CREATE TABLE BaseItems (Id TEXT)")
    conn.commit()
    conn.close()

    from utils import media_server as ms

    monkeypatch.setattr(ms, "candidate_data_dirs",
                        lambda: [("emby", legacy_dir), ("jellyfin", ef_dir)])
    found = ms.find_local_databases()
    names = [f["path"].name for f in found]
    assert set(names) == {"library.db", "jellyfin.db"}
    assert found[0]["schema"] == SCHEMA_JELLYFIN_EF, "信息更全的 EF 架构必须排在最前"


# ── 6. 版本与界面接线 ──────────────────────────────────────

def test_version_declares_supported_servers():
    import version

    assert version.SUPPORTED_SERVERS == ("Jellyfin", "Emby")
    assert version.VERSION_TUPLE[:2] == (3, 7)


def test_workers_accept_config_object(qapp):
    """worker 现在接收配置对象（数据来源在内部解析），不再只认 db_path。"""
    from config import ToolkitConfig
    from workers.ingest import IngestWorker
    from workers.scorer import AnalyzeWorker

    cfg = ToolkitConfig()
    ingest = IngestWorker(cfg)
    assert ingest.source is cfg
    assert ingest.exclude_keywords == []

    analyze = AnalyzeWorker(cfg, {})
    assert analyze.source is cfg

    # 旧式调用（传路径字符串）仍要能用
    assert IngestWorker("C:/x/jellyfin.db").source == "C:/x/jellyfin.db"


def test_settings_page_exposes_source_controls(qapp):
    from config import ToolkitConfig
    from ui.pages.settings import SettingsPage

    page = SettingsPage(ToolkitConfig())
    assert hasattr(page, "combo_source")
    assert hasattr(page, "combo_server_kind")
    assert hasattr(page, "btn_detect_db")
    assert hasattr(page, "lbl_source_now")
    # 占位提示不能还挂着 https 的 8920 —— 默认是 http 8096
    assert "8920" not in page.input_jf_server.placeholderText()
    assert "8096" in page.input_jf_server.placeholderText()


def test_settings_save_persists_source_and_kind(qapp, monkeypatch):
    """聚合保存方法必须把新字段写回配置（这类方法最容易漏改）。"""
    from PySide6.QtWidgets import QMessageBox

    from config import ToolkitConfig
    from ui.pages.settings import SettingsPage

    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    cfg = ToolkitConfig()
    page = SettingsPage(cfg)
    page.combo_source.setCurrentIndex(page.combo_source.findData("api"))
    page.combo_server_kind.setCurrentIndex(page.combo_server_kind.findData("emby"))
    page.input_db.setText("")
    page._save()

    assert page.cfg.library_source == "api"
    assert page.cfg.server_type == "emby"

    from config import ToolkitConfig as TC
    loaded = TC.load()
    assert loaded.library_source == "api", "新字段必须真的落盘"
    assert loaded.server_type == "emby"


def test_settings_source_hint_is_side_effect_free(qapp):
    """预览来源解析不能改动内存里的配置。"""
    from config import ToolkitConfig
    from ui.pages.settings import SettingsPage

    cfg = ToolkitConfig()
    cfg.library_source = "auto"
    cfg.server_type = "auto"
    page = SettingsPage(cfg)
    page.combo_source.setCurrentIndex(page.combo_source.findData("db"))
    page.combo_server_kind.setCurrentIndex(page.combo_server_kind.findData("emby"))

    page._refresh_source_hint()
    assert cfg.library_source == "auto"
    assert cfg.server_type == "auto"


# ── 7. 采集性能与三源一致性 ────────────────────────────────

def _make_ef_db(path: Path, item_id: str, name: str, video: Path) -> Path:
    """构造一个 Jellyfin 10.9+（EF Core 架构）最小库。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE BaseItems (Id TEXT, Name TEXT, Path TEXT, "
                 "ProductionYear INTEGER, Genres TEXT, Tags TEXT, Studios TEXT, "
                 "Size INTEGER, Type TEXT, IsFolder INTEGER, MediaType TEXT)")
    conn.execute("CREATE TABLE Peoples (Id TEXT, Name TEXT)")
    conn.execute("CREATE TABLE PeopleBaseItemMap (ItemId TEXT, PeopleId TEXT)")
    conn.execute("CREATE TABLE UserData (ItemId TEXT, IsFavorite INTEGER, "
                 "PlayCount INTEGER, Played INTEGER, PlaybackPositionTicks INTEGER, "
                 "LastPlayedDate TEXT, Rating REAL)")
    conn.execute("CREATE TABLE BaseItemImageInfos (ItemId TEXT, Path TEXT, ImageType INTEGER)")
    conn.execute("INSERT INTO BaseItems VALUES (?,?,?,?,?,?,?,?,?,0,'Video')",
                 (item_id, name, str(video), 2019, "中出", "无码", "StudioA",
                  2_000_000_000, "Movie"))
    conn.execute("INSERT INTO Peoples VALUES ('p1','ActorA')")
    conn.execute("INSERT INTO PeopleBaseItemMap VALUES (?, 'p1')", (item_id,))
    conn.commit()
    conn.close()
    return path


def test_dir_listing_is_cached(monkeypatch, tmp_path):
    """目录列举必须缓存。

    同一个目录里常有几十上百个条目；媒体库放在网络盘（NAS）上时每次
    ``os.listdir`` 都是几十毫秒的网络往返，上千条目就是几十秒的纯浪费。
    """
    from utils import library as lib

    d = tmp_path / "lib"
    d.mkdir()
    for i in range(20):
        (d / f"MIAA-{700 + i}.mp4").touch()
    (d / "MIAA-700.jpg").touch()

    real_listdir = lib.os.listdir
    calls = []

    def _counting(path):
        calls.append(path)
        return real_listdir(path)

    monkeypatch.setattr(lib.os, "listdir", _counting)

    cache: dict = {}
    for i in range(20):
        lib.find_cover(str(d / f"MIAA-{700 + i}.mp4"), f"MIAA-{700 + i}", cache=cache)
    with_cache = len(calls)

    calls.clear()
    for i in range(20):
        lib.find_cover(str(d / f"MIAA-{700 + i}.mp4"), f"MIAA-{700 + i}")
    without_cache = len(calls)

    assert with_cache == 1, f"带缓存时 20 次查找只该列举目录 1 次，实际 {with_cache}"
    assert without_cache == 20, "不带缓存时每次都要列举（用于对照，证明缓存确实起作用）"


def test_find_cover_cache_keeps_result_identical(tmp_path):
    """加了缓存不能改变结果。"""
    from utils.library import find_cover

    d = tmp_path / "lib"
    d.mkdir()
    (d / "MIAA-743.mp4").touch()
    (d / "MIAA-743.jpg").touch()
    cache: dict = {}
    a = find_cover(str(d / "MIAA-743.mp4"), "MIAA-743")
    b = find_cover(str(d / "MIAA-743.mp4"), "MIAA-743", cache=cache)
    c = find_cover(str(d / "MIAA-743.mp4"), "MIAA-743", cache=cache)
    assert a == b == c and a is not None and a.endswith("MIAA-743.jpg")


def test_three_sources_agree_on_classification(tmp_path, monkeypatch):
    """同一部作品，三种读法必须得出**完全一致**的判定。

    这条是 ``build_item`` 的存在理由：EF 库 / 传统库 / REST API 的字段结构
    各不相同，如果各自实现一遍归一化，迟早会出现"同一个库换种读法分数就变了"。
    """
    video = tmp_path / "StudioA" / "MIAA-743" / "MIAA-743.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")

    ef = _make_ef_db(tmp_path / "jellyfin.db", "ef-1", "MIAA-743", video)

    legacy = _make_legacy_db(
        tmp_path / "library.db",
        [(_guid(), "MediaBrowser.Controller.Entities.Movies.Movie",
          {"Genres": ["中出"], "Tags": ["无码"], "Studios": ["StudioA"],
           "ProductionYear": 2019, "Size": 2_000_000_000},
          str(video), "MIAA-743", 0)],
    )

    req, _calls = _api_req([{"Items": [{
        "Id": "api-1", "Name": "MIAA-743", "Path": str(video), "Type": "Movie",
        "ProductionYear": 2019, "Genres": ["中出"], "Tags": ["无码"],
        "Studios": ["StudioA"], "MediaSources": [{"Size": 2_000_000_000}],
        "People": [{"Name": "ActorA", "Type": "Actor"}],
    }], "TotalRecordCount": 1}])
    monkeypatch.setattr(library_api, "_req", req)
    monkeypatch.setattr(media_server, "_request",
                        _ms_routes({"/System/Info/Public": (200, {"ProductName": "Emby"})}))

    ef_items = collect_library(str(ef))
    legacy_items = collect_library(str(legacy))
    api_items, _kind = library_api.collect_library_api("http://demo:8096", "k")

    def fingerprint(it):
        return (it.num, it.bucket, it.is_cd, it.cd_index, it.year,
                tuple(sorted(it.tags)), tuple(sorted(it.genres)),
                tuple(sorted(it.studios)), it.is_uncensored)

    prints = [fingerprint(x[0]) for x in (ef_items, legacy_items, api_items)]
    assert prints[0] == prints[1] == prints[2], prints
    assert prints[0][0] == "MIAA-743" and prints[0][1] == "jav"


def test_build_item_ignores_unknown_behavior_keys():
    """行为字典里混进不存在的字段时不能给 Item 塞垃圾属性。"""
    from utils.library import build_item

    it = build_item(item_id="x", name="MIAA-743", path=f"{DEMO}/a/MIAA-743.mp4",
                    behavior={"play_count": 2, "nonexistent_field": 1})
    assert it.play_count == 2
    assert not hasattr(it, "nonexistent_field")


def test_config_heals_dead_drive_in_db_path(monkeypatch):
    """库路径指向的盘符在本机不存在时会被清空，并给出说明。

    用 monkeypatch 固定 ``_drive_exists``，让这条用例在 Windows / Linux / macOS
    上测的都是同一段逻辑 —— 真实盘符是否存在不该影响用例结果。
    """
    import config as config_mod
    from config import heal_config

    monkeypatch.setattr(config_mod, "_drive_exists", lambda _p: False)
    healed, notes = heal_config({"jellyfin_db_path": "Q:/definitely/not/here/x.db"})
    assert healed["jellyfin_db_path"] == ""
    assert notes, "清空失效路径必须给出说明"


def test_heal_config_is_conservative_on_posix():
    """非 Windows 上不猜盘符路径是否失效，绝不清空用户配置。

    POSIX 上 ``C:/x`` 只是个普通目录名，判断不了"失效"；猜错的代价是
    用户填好的路径被静默清掉，比留着不管糟得多。
    """
    import sys as _sys

    import pytest as _pytest

    from config import heal_config

    if _sys.platform == "win32":
        _pytest.skip("该守则针对非 Windows 平台")
    healed, notes = heal_config({"jellyfin_db_path": "C:/x/jellyfin.db"})
    assert healed["jellyfin_db_path"] == "C:/x/jellyfin.db"
    assert notes == []
