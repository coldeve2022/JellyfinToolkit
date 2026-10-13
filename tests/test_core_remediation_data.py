"""Consumer-visible regressions for C04-C10/C14/C15/C24/C29/C30."""
import csv
import io
import sqlite3

import pytest

from utils import library, library_api, profile, scoring, scoring_v2


def movie(iid, **kwargs):
    values = dict(id=iid, name=iid, path=f"Z:/fixtures/{iid}.mp4", type_cn="Movie",
                  year=2024, size=500_000_000, num=None, bucket="jav", actors=["ActorA"])
    values.update(kwargs)
    return library.Item(**values)


@pytest.mark.parametrize("engine", [scoring, scoring_v2])
def test_cold_start_is_unknown_not_delete(engine):
    result = engine.rank_items([movie("new")], {})[0]
    assert result.decision == scoring.DEC_REVIEW


@pytest.mark.parametrize("engine", [scoring, scoring_v2])
def test_ai_negative_suggestion_cannot_create_cold_start_truth(engine):
    result = engine.rank_items([movie("new")], {}, extra_disliked={("actor", "ActorA")})[0]
    assert result.decision == scoring.DEC_REVIEW


@pytest.mark.parametrize("engine", [scoring, scoring_v2])
def test_explicit_like_learns_while_keep_is_only_protection(engine):
    seed, candidate = movie("seed"), movie("candidate")
    result = engine.rank_items([seed, candidate], {seed.id: scoring.LABEL_LIKE})
    assert result[1].decision == scoring.DEC_LIKE
    keep = engine.rank_items([seed, candidate], {seed.id: scoring.LABLE_KEEP})
    assert keep[0].decision == scoring.DEC_KEEP
    assert keep[1].decision == scoring.DEC_REVIEW


@pytest.mark.parametrize("engine", [scoring, scoring_v2])
def test_prefix_only_is_insufficient_for_content_deletion(engine):
    seed = movie("seed", is_favorite=True)
    blank = movie("blank", actors=[], num="XYZ-321")
    assert engine.rank_items([seed, blank], {})[1].decision == scoring.DEC_REVIEW


def test_same_physical_file_is_never_a_deletion_version(tmp_path):
    path = tmp_path / "ABC-123.mp4"
    path.write_bytes(b"synthetic")
    a = movie("a", path=str(path), num="ABC-123")
    b = movie("b", path=str(path.parent / "." / path.name), num="ABC-123")
    assert scoring.dedup_loser_ids([a, b]) == set()
    assert scoring.dedup_groups([a, b]) == []


def test_keep_on_an_alias_protects_the_entity_against_another_version(tmp_path):
    same = tmp_path / "same.mp4"
    same.write_bytes(b"synthetic")
    a = movie("a", path=str(same), num="ABC-123", size=500_000_000)
    alias = movie("alias", path=str(same), num="ABC-123", size=500_000_000)
    other = movie("other", num="ABC-123", size=900_000_000)
    assert scoring.dedup_loser_ids([a, alias, other], {"alias": "KEEP"}) == set()
    assert scoring.dedup_groups([a, alias, other], {"alias": "KEEP"})[0]["losers"] == set()


def test_title_falls_back_to_file_number_and_split_index():
    item = library.build_item(item_id="a", name="Readable title", path="Z:/ABC-123-cd2.mp4")
    assert (item.num, item.is_cd, item.cd_index) == ("ABC-123", True, 2)


@pytest.mark.parametrize("name,want", [("FC2-PPV-1234567.mp4", "FC2-1234567"),
                                     ("FC2-PPV-123456.mp4", "FC2-123456")])
def test_common_fc2_ppv_names_normalize(name, want):
    assert library.extract_number(name) == want


def test_api_studio_objects_are_names_in_scoring():
    it = library.build_item(item_id="a", name="ABC-123", path="Z:/ABC-123.mp4",
                           studios=[{"Name": "Studio A", "Id": "s"}])
    assert it.studios == ["Studio A"]
    assert ("studio", "Studio A") in scoring.item_features(it)


def test_cracked_file_is_detected_and_kept_over_larger_censored_copy():
    a = library.build_item(item_id="u", name="ABC-123-U", path="Z:/ABC-123-U.mp4", size=500_000_000)
    b = library.build_item(item_id="c", name="ABC-123", path="Z:/ABC-123.mp4", size=900_000_000)
    assert a.is_uncensored
    assert scoring.dedup_loser_ids([a, b]) == {"c"}


def test_known_resolution_beats_larger_file_size():
    low = movie("low", num="ABC-123", size=900_000_000)
    high = movie("high", num="ABC-123", size=500_000_000)
    low.width, low.height = 1280, 720
    high.width, high.height = 1920, 1080
    assert scoring.dedup_loser_ids([low, high]) == {"low"}


def test_api_media_stream_facts_reach_the_version_ranker(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(library_api, "detect", lambda *a, **k:
                        SimpleNamespace(ok=True, kind="jellyfin", describe=lambda:"Synthetic"))
    raw = {"Id":"api", "Name":"ABC-123", "Path":"Z:/ABC-123.mp4", "Type":"Movie",
           "Studios":[{"Name":"StudioA","Id":"s"}], "People":[], "Genres":[], "Tags":[],
           "MediaSources":[{"Size":500_000_000,"RunTimeTicks":10_000_000,
                            "MediaStreams":[{"Type":"Video","Width":1920,"Height":1080,"BitRate":800000}]}],
           "UserData":{"IsFavorite":False,"Played":False,"PlayCount":0,"PlaybackPositionTicks":0}}
    def request(method,url,key,**kwargs):
        return (200,[{"Id":"viewer"}]) if url.endswith("/Users") else (200,{"Items":[raw],"TotalRecordCount":1})
    monkeypatch.setattr(library_api,"_req",request)
    item = library_api.collect_library_api("http://example.invalid","synthetic")[0][0]
    assert (item.width,item.height,item.duration) == (1920,1080,1.0)


def test_multiuser_api_requires_explicit_viewer(monkeypatch):
    users = [{"Id": "viewer", "Policy": {}}, {"Id": "unused-admin", "Policy": {"IsAdministrator": True}}]
    monkeypatch.setattr(library_api, "_req", lambda *a, **k: (200, users))
    with pytest.raises(library_api.LibraryApiError):
        library_api.pick_user_id("http://example.invalid", "synthetic")


def test_explicit_api_viewer_does_not_choose_admin(monkeypatch):
    users = [{"Id": "viewer", "Policy": {}}, {"Id": "admin", "Policy": {"IsAdministrator": True}}]
    monkeypatch.setattr(library_api, "_req", lambda *a, **k: (200, users))
    assert library_api.pick_user_id("http://example.invalid", "synthetic", user_id="viewer") == "viewer"


def make_ef_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""CREATE TABLE BaseItems(Id TEXT,Name TEXT,Path TEXT,ProductionYear INTEGER,
          Genres TEXT,Tags TEXT,Studios TEXT,Size INTEGER,Type TEXT,IsFolder INTEGER,MediaType TEXT);
          CREATE TABLE Peoples(Id TEXT,Name TEXT); CREATE TABLE PeopleBaseItemMap(ItemId TEXT,PeopleId TEXT);
          CREATE TABLE UserData(ItemId TEXT,IsFavorite INTEGER,PlayCount INTEGER,Played INTEGER,
          PlaybackPositionTicks INTEGER,LastPlayedDate TEXT,Rating REAL,UserId TEXT);""")
        conn.execute("INSERT INTO BaseItems VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("item", "ABC-123", "Z:/ABC-123.mp4", 2024, "", "", "", 500000000, "Movie", 0, "Video"))
        conn.executemany("INSERT INTO UserData VALUES(?,?,?,?,?,?,?,?)", [
            ("item", 1, 4, 1, 0, None, None, "viewer"), ("item", 0, 0, 0, 0, None, None, "other")])


def test_db_multiuser_never_overwrites_viewer(tmp_path):
    db = tmp_path / "jellyfin.db"
    make_ef_db(db)
    it = library.collect_library(str(db), user_id="viewer")[0]
    assert (it.is_favorite, it.is_played, it.play_count) == (True, True, 4)


def test_db_ambiguous_behavior_is_unknown(tmp_path):
    db = tmp_path / "jellyfin.db"
    make_ef_db(db)
    it = library.collect_library(str(db))[0]
    assert it.behavior_known is False


def test_db_behavior_without_user_identity_is_unknown(tmp_path):
    db = tmp_path / "jellyfin.db"
    make_ef_db(db)
    with sqlite3.connect(db) as connection:
        connection.executescript("""CREATE TABLE OldUserData AS SELECT ItemId,IsFavorite,PlayCount,Played,
        PlaybackPositionTicks,LastPlayedDate,Rating FROM UserData;
        DROP TABLE UserData; ALTER TABLE OldUserData RENAME TO UserData;""")
    assert library.collect_library(str(db))[0].behavior_known is False


def test_explicit_positive_is_in_profile_and_not_invented_negative():
    a, b = movie("a"), movie("b")
    positive = profile.build_profile([a, b], {"a": scoring.LABEL_LIKE})
    assert positive["n_pos"] == 1
    predicted = profile.build_profile([a, b], {}, scoring_v2.rank_items([a, b], {}))
    assert predicted["neg_features"] == []


def test_recommendation_excludes_zero_hit_and_delete_candidates():
    a = movie("a")
    fake = scoring.Scored(a, s_content=0, decision=scoring.DEC_DELETE)
    assert profile.build_profile([a], {}, [fake])["recs"] == []


def test_overview_uses_same_junk_and_cd_group_rules():
    a = movie("a", num="ABC-123")
    junk = movie("theme", num="ABC-123", path="Z:/theme.mp4")
    assert library.summarize([a, junk])["dup_groups"] == len(scoring.dedup_groups([a, junk]))
    cd = movie("cd", num="ABC-123", is_cd=True)
    assert library.summarize([a, cd])["dup_cd_groups"] == 0


def test_csv_preserves_comma_quote_and_newline():
    p = profile.build_profile([], {})
    p["top_actors"] = [{"name": 'A, "B"\nC', "weight": 2, "fav": 1, "played": 0}]
    rows = list(csv.reader(io.StringIO(profile.profile_to_csv(p))))
    row = next(r for r in rows if r and r[0] == "Top演员")
    assert row[1] == 'A, "B"\nC'
