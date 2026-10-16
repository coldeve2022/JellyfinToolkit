"""Library count follow-up: real EF/legacy schema and REST DTO behavior, no real media."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from utils import library, library_api, library_source, profile, scoring


def entry(item_id, *, path=None, kind="Movie", extra=None, virtual=0, name=None,
          primary=None, deleted=0, favorite=True):
    return dict(Id=item_id, Name=name or item_id, Path=path or f"/__synthetic_remote__/{item_id}.mp4",
                Type=kind, ExtraType=extra, IsVirtualItem=virtual, PrimaryVersionId=primary,
                IsDeleted=deleted, ProductionYear=2024, IsFolder=0, MediaType="Video",
                Tags=["AuxOnly" if extra else "MainTag"], Genres=[], Studios=[],
                People=[{"Name": "AuxActor" if extra else "MainActor", "Type": "Actor"}],
                Size=500_000_000, UserData={"IsFavorite": favorite, "PlayCount": 0, "Played": False})


def collect_rows(source, rows, tmp_path, monkeypatch, *, old_schema=False, users=("viewer", "other")):
    if source == "api":
        monkeypatch.setattr(library_api, "detect", lambda *a, **k: SimpleNamespace(
            ok=True, kind="jellyfin", describe=lambda: "synthetic server"))
        monkeypatch.setattr(library_api, "list_users", lambda *a, **k: [
            {"id": user, "name": user, "is_admin": False} for user in users])
        monkeypatch.setattr(library_api, "_iter_pages", lambda *a, **k: iter([rows]))
        return library_api.collect_library_api("http://synthetic.invalid", "synthetic", user_id="viewer")[0]
    db = tmp_path / ("jellyfin.db" if source == "ef" else "library.db")
    with sqlite3.connect(db) as conn:
        if source == "ef":
            conn.executescript("""CREATE TABLE BaseItems(Id TEXT,Name TEXT,Path TEXT,ProductionYear INT,
            Genres TEXT,Tags TEXT,Studios TEXT,Size INT,Type TEXT,IsFolder INT,MediaType TEXT);
            CREATE TABLE Peoples(Id TEXT,Name TEXT); CREATE TABLE PeopleBaseItemMap(ItemId TEXT,PeopleId TEXT);""")
        else:
            conn.execute("CREATE TABLE TypedBaseItems(guid TEXT,type TEXT,data BLOB,Path TEXT,Name TEXT,IsFolder INT)")
        table = "BaseItems" if source == "ef" else "TypedBaseItems"
        if not old_schema:
            for col, typ in (("ExtraType", "INT"), ("IsVirtualItem", "INT"),
                             ("PrimaryVersionId", "TEXT"), ("IsDeleted", "INT")):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        conn.execute("""CREATE TABLE UserData(ItemId TEXT,IsFavorite INT,PlayCount INT,Played INT,
                    PlaybackPositionTicks INT,LastPlayedDate TEXT,Rating REAL,UserId TEXT)""")
        for raw in rows:
            if source == "ef":
                cols = ["Id", "Name", "Path", "ProductionYear", "Genres", "Tags", "Studios", "Size", "Type", "IsFolder", "MediaType"]
                values = [','.join(raw[key]) if key in ("Genres", "Tags", "Studios") else raw[key] for key in cols]
                conn.execute("INSERT INTO Peoples VALUES(?,?)", (raw["Id"], raw["People"][0]["Name"]))
                conn.execute("INSERT INTO PeopleBaseItemMap VALUES(?,?)", (raw["Id"], raw["Id"]))
            else:
                cols = ["guid", "type", "data", "Path", "Name", "IsFolder"]
                values = [raw["Id"], "MediaBrowser.Controller.Entities." + raw["Type"],
                          json.dumps(raw).encode(), raw["Path"], raw["Name"], 0]
            if not old_schema:
                cols += ["ExtraType", "IsVirtualItem", "PrimaryVersionId", "IsDeleted"]
                values += [raw[key] for key in cols[-4:]]
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES({','.join('?' for _ in cols)})", values)
            for user in users:
                conn.execute("INSERT INTO UserData VALUES(?,?,?,?,?,?,?,?)", (
                    raw["Id"], int(raw["UserData"]["IsFavorite"]) if user == "viewer" else 0,
                    0, 0, 0, None, None, user))
    return library.collect_library(str(db), user_id="viewer")


@pytest.mark.parametrize("source", ["ef", "legacy", "api"])
@pytest.mark.parametrize("extra", [2, 3, 9, "Trailer", "BehindTheScenes", "ThemeVideo"])
def test_typed_extras_do_not_enter_library_or_profile(source, extra, tmp_path, monkeypatch):
    rows = [entry("main", kind="Video"), entry("aux", kind="Video", extra=extra)]
    result = collect_rows(source, rows, tmp_path, monkeypatch)
    assert [it.id for it in result] == ["main"]
    assert library.summarize(result)["total"] == 1
    assert profile.build_profile(result, {})["n_pos"] == 1


@pytest.mark.parametrize("source", ["ef", "legacy", "api"])
def test_path_extras_trailer_virtual_and_deleted_excluded(source, tmp_path, monkeypatch):
    rows = [entry("main"), entry("theme", kind="Video", path="/__synthetic_remote__/film/theme.mp4"),
            entry("extras", kind="Video", path="/__synthetic_remote__/film/extras/bonus.mp4"),
            entry("trailer", kind="Trailer"), entry("virtual", virtual=1),
            entry("deleted", deleted=1)]
    assert [it.id for it in collect_rows(source, rows, tmp_path, monkeypatch)] == ["main"]


@pytest.mark.parametrize("source", ["ef", "legacy", "api"])
def test_primary_movies_episodes_generic_video_unknown_extra_and_versions_remain(source, tmp_path, monkeypatch):
    rows = [entry("movie", name="The Theme of Love"),
            entry("episode", kind="Episode", extra=0, name="Trailer Park Boys S01E01"),
            entry("generic", kind="Video", extra="Unknown", name="Interview"),
            entry("variant", primary="movie"),
            entry("new-enum", extra=99)]
    result = collect_rows(source, rows, tmp_path, monkeypatch)
    assert {it.id for it in result} == {raw["Id"] for raw in rows}
    assert next(it for it in result if it.id == "episode").bucket != library.BUCKET_TRAILER
    assert next(it for it in result if it.id == "variant").primary_version_id == "movie"


def test_legacy_without_extra_columns_uses_json_extra_metadata(tmp_path, monkeypatch):
    rows = [entry("main"), entry("json-theme", kind="Video", extra=9)]
    assert [it.id for it in collect_rows("legacy", rows, tmp_path, monkeypatch, old_schema=True)] == ["main"]


def test_older_ef_schema_without_new_metadata_columns_still_collects(tmp_path, monkeypatch):
    assert [it.id for it in collect_rows("ef", [entry("main")], tmp_path, monkeypatch, old_schema=True)] == ["main"]


def test_missing_local_file_is_excluded_but_remote_unknown_is_retained(tmp_path, monkeypatch):
    folder = tmp_path / "reachable"
    folder.mkdir()
    main = folder / "present.mp4"
    main.write_bytes(b"synthetic")
    rows = [entry("main", path=str(main)), entry("stale", path=str(folder / "missing.mp4")),
            entry("remote", path="/__synthetic_remote__/unknown/nested/file.mp4")]
    assert {it.id for it in collect_rows("ef", rows, tmp_path, monkeypatch)} == {"main", "remote"}


def test_duplicate_item_id_from_repeated_api_page_is_not_counted_twice(tmp_path, monkeypatch):
    main = entry("main")
    assert len(collect_rows("api", [main, main], tmp_path, monkeypatch)) == 1


def test_count_contract_distinguishes_entries_entities_and_identified_works(tmp_path):
    one = tmp_path / "ABC-123-CD1.mp4"
    two = tmp_path / "ABC-123-CD2.mp4"
    one.write_bytes(b"one"); two.write_bytes(b"two")
    rows = [library.build_item(item_id="a", name="ABC-123", path=str(one)),
            library.build_item(item_id="alias", name="ABC-123", path=str(one)),
            library.build_item(item_id="part2", name="ABC-123", path=str(two))]
    counts = library.summarize(rows)
    assert (counts["total"], counts["entry_count"], counts["file_count"], counts["work_count"]) == (3, 3, 2, 1)


def test_profile_combines_alias_behavior_and_feedback_once_without_dropping_ids():
    a = library.Item("unseen", "ABC-123", "/__synthetic_remote__/same.mp4", "Movie", 2024,
                     actors=["ActorA"], bucket="jav")
    b = library.Item("watched-alias", "ABC-123", a.path, "Movie", 2024,
                     actors=["ActorA"], bucket="jav", is_favorite=True, is_played=True)
    labels = {"unseen": scoring.LABEL_LIKE}
    p = profile.build_profile([a, b], labels)
    assert (p["n_pos"], p["n_fav"], p["n_played"], p["total"]) == (1, 1, 1, 1)
    assert p["top_actors"][0]["fav"] == 1
    assert p["entry_count"] == 2
    assert p["file_count"] == 1
    assert [a.id, b.id] == ["unseen", "watched-alias"]


def test_collection_explains_filtered_counts_without_listing_private_titles(tmp_path, monkeypatch):
    result = collect_rows("ef", [entry("main"), entry("aux", extra=9)], tmp_path, monkeypatch)
    counts = library.summarize(result)
    assert counts["collection_stats"]["raw_video_entries"] == 2
    assert counts["collection_stats"]["excluded_extra_type"] == 1
    assert counts["filtered_out"] == 1


def test_work_identity_unifies_an_alias_without_number_and_a_server_version_link():
    rows = [library.Item("main", "Title", "/__synthetic_remote__/same.mp4", "Movie", 2024,
                         num="ABC-123"),
            library.Item("alias", "Title", "/__synthetic_remote__/same.mp4", "Movie", 2024),
            library.Item("version", "Title", "/__synthetic_remote__/version.mp4", "Movie", 2024,
                         primary_version_id="main")]
    counts = library.summarize(rows)
    assert (counts["entry_count"], counts["file_count"], counts["work_count"]) == (3, 2, 1)


def test_source_mapping_preserves_diagnostics_and_checks_mapped_missing_file(tmp_path, monkeypatch):
    result = collect_rows("api", [entry("main", path="/remote/present.mp4"),
                                   entry("stale", path="/remote/missing.mp4"),
                                   entry("aux", extra=9)], tmp_path, monkeypatch)
    monkeypatch.setattr(library_source, "collect_library_api", lambda *a, **k: (result, "jellyfin"))
    local = tmp_path / "mapped"
    local.mkdir()
    (local / "present.mp4").write_bytes(b"synthetic")
    cfg = SimpleNamespace(library_source="api", jellyfin_server="http://synthetic.invalid",
                          jellyfin_api_key="synthetic", jellyfin_user_id="viewer",
                          library_path_mappings=[{"remote":"/remote", "local":str(local)}])
    mapped = library_source.collect(cfg)
    assert [it.id for it in mapped] == ["main"]
    assert mapped[0].source_path == "/remote/present.mp4"
    assert library.summarize(mapped)["collection_stats"]["raw_video_entries"] == 3
    assert library.summarize(mapped)["filtered_out"] == 2


def test_score_snapshot_copies_collection_diagnostics_and_profile_counts(tmp_path, monkeypatch):
    import config
    from utils.scoring_service import score_snapshot
    cfg = config.ToolkitConfig(library_source="api", jellyfin_server="http://synthetic.invalid",
                               jellyfin_user_id="viewer")
    rows = collect_rows("api", [entry("main"), entry("aux", extra=9)], tmp_path, monkeypatch)
    result = score_snapshot(rows, cfg, {})
    assert library.summarize(result["items"])["filtered_out"] == 1
    assert result["profile"]["entry_count"] == result["profile"]["file_count"] == 1


def test_multiviewer_userdata_rows_cannot_multiply_library_items(tmp_path, monkeypatch):
    rows = collect_rows("ef", [entry("main")], tmp_path, monkeypatch,
                        users=("viewer", "other", "third", "fourth"))
    assert len(rows) == 1
    assert rows[0].is_favorite
    assert profile.build_profile(rows, {})["n_pos"] == 1


def test_negative_alias_overrides_duplicate_behavior_for_profile_evidence():
    a = library.Item("a", "Title", "/__synthetic_remote__/same.mp4", "Movie", 2024,
                     actors=["ActorA"], is_favorite=True)
    b = library.Item("b", "Title", a.path, "Movie", 2024, actors=["ActorA"])
    p = profile.build_profile([a, b], {"b":scoring.LABEL_DELETE})
    assert p["n_pos"] == 0 and p["n_neg"] == 1


@pytest.mark.parametrize("source", ["ef", "legacy", "api"])
@pytest.mark.parametrize("extra", [0, "Unknown"])
def test_unset_extra_enum_never_excludes_primary_movie_or_episode(source, extra, tmp_path, monkeypatch):
    rows = [entry("film", extra=extra), entry("episode", kind="Episode", extra=extra)]
    assert len(collect_rows(source, rows, tmp_path, monkeypatch)) == 2


@pytest.mark.parametrize("source", ["ef", "legacy", "api"])
def test_primary_type_beats_ambiguous_attachment_words_in_actual_path(source, tmp_path, monkeypatch):
    rows = [entry("interview", path="/__synthetic_remote__/The Interview.mp4"),
            entry("theme", path="/__synthetic_remote__/The Theme.mp4"),
            entry("special", kind="Episode", path="/__synthetic_remote__/show/specials/01.mkv")]
    result = collect_rows(source, rows, tmp_path, monkeypatch)
    assert len(result) == 3
    assert all(it.bucket != library.BUCKET_TRAILER for it in result)


@pytest.mark.parametrize("extra", [1.5, 1.0, "3.5", "unknown-new-enum", True])
def test_invalid_extra_enum_formats_are_not_assumed_to_be_an_attachment(extra, tmp_path, monkeypatch):
    assert len(collect_rows("api", [entry("movie", extra=extra)], tmp_path, monkeypatch)) == 1


def test_cover_lookup_filters_item_ids_and_caches_repeated_paths(tmp_path, monkeypatch):
    db = tmp_path / "covers.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE BaseItemImageInfos(ItemId TEXT, Path TEXT, ImageType INT)")
        conn.executemany("INSERT INTO BaseItemImageInfos VALUES(?,?,?)", [
            ("main", "/synthetic/poster.jpg", 0), ("alias", "/synthetic/poster.jpg", 0),
            ("aux", "/synthetic/must-not-probe.jpg", 0)])
    calls = []
    monkeypatch.setattr(library.os.path, "isfile", lambda path: calls.append(str(path)) or True)
    result = library.load_primary_covers(str(db), item_ids={"main", "alias"})
    assert set(result) == {"main", "alias"}
    assert calls == ["/synthetic/poster.jpg"]


def test_cover_lookup_cancellation_never_publishes_partial_map(tmp_path, monkeypatch):
    db = tmp_path / "covers.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE BaseItemImageInfos(ItemId TEXT, Path TEXT, ImageType INT)")
        conn.executemany("INSERT INTO BaseItemImageInfos VALUES(?,?,0)", [("a", "a.jpg"), ("b", "b.jpg")])
    calls = []
    monkeypatch.setattr(library.os.path, "isfile", lambda path: calls.append(str(path)) or True)
    with pytest.raises(library.LibraryCollectionCancelled):
        library.load_primary_covers(str(db), should_stop=lambda: bool(calls))
    assert len(calls) == 1


def test_ef_only_requests_covers_for_metadata_filtered_primary_ids(tmp_path, monkeypatch):
    captured = []
    def covers(*args, **kwargs):
        captured.append(kwargs.get("item_ids"))
        return {}
    monkeypatch.setattr(library, "load_primary_covers", covers)
    collect_rows("ef", [entry("main"), entry("aux", extra=9), entry("trailer", kind="Trailer")], tmp_path, monkeypatch)
    assert captured == [{"main"}]


def test_overview_duplicate_counts_never_probe_media_quality(tmp_path, monkeypatch):
    from utils import merge
    a, b = tmp_path / "ABC-123.mp4", tmp_path / "ABC-123-CD1.mp4"
    a.write_bytes(b"a"); b.write_bytes(b"b")
    rows = [library.build_item(item_id="main", name="ABC-123", path=str(a), size=500_000_000),
            library.build_item(item_id="split", name="ABC-123", path=str(b), size=500_000_000)]
    def forbidden(*args, **kwargs):
        raise AssertionError("overview must not launch ffprobe")
    monkeypatch.setattr(merge, "probe_part", forbidden)
    counts = library.summarize(rows)
    assert (counts["dup_groups"], counts["dup_true"], counts["dup_cd_groups"]) == (1, 1, 0)


def test_main_movie_metadata_title_is_not_junk_evidence():
    row = library.Item("main", "Interview", "/__synthetic_remote__/feature.mp4", "Movie", 2024,
                       size=500_000_000, actors=["ActorA"], bucket="jav")
    assert scoring.is_junk_item(row) is False


def test_manual_display_movie_type_does_not_hide_a_theme_attachment():
    row = library.Item("manual-theme", "theme", "/__synthetic_remote__/theme.mp4", "Movie", 2024,
                       size=20_000_000, num="ABC-123", bucket="jav")
    assert scoring.is_junk_item(row) is True
    assert library.summarize([row])["entry_count"] == 0


def test_collected_movie_type_remains_authoritative_for_ambiguous_names():
    row = library.build_item(item_id="primary", name="The Theme",
                             path="/__synthetic_remote__/The Theme.mp4", type_full="Movie",
                             size=500_000_000)
    assert scoring.is_junk_item(row) is False
    assert library.summarize([row])["entry_count"] == 1
