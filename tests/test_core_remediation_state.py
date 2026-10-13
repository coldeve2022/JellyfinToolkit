"""State isolation and invalidation; retain legacy no-context behavior."""
from types import SimpleNamespace
import pytest
from utils.ai_profile import AIProfileStore
from utils.ai_scoring import AIScoreStore
from utils.deck_state import DeckState
from utils.labels import LabelStore


def test_labels_do_not_leak_between_servers_or_users(tmp_path):
    db = tmp_path / "state.db"
    LabelStore(db).set("same-id", "DELETE")
    a = LabelStore(db, context="server:a/user:one")
    b = LabelStore(db, context="server:a/user:two")
    assert a.all() == {}
    a.set("same-id", "KEEP")
    assert b.all() == {}
    assert LabelStore(db).get("same-id") == "DELETE"


def test_deck_skip_does_not_leak_between_viewers(tmp_path):
    db = tmp_path / "state.db"
    a, b = DeckState(db, context="a"), DeckState(db, context="b")
    a.add_skipped("same-id")
    a.set_order("random")
    assert b.skipped() == set()
    assert b.get_order() == "score"


def test_ai_scores_require_same_context_and_fresh_fingerprint(tmp_path):
    db = tmp_path / "state.db"
    s = AIScoreStore(db, context="a", fingerprint="old-profile")
    s.save("same-id", .9)
    assert AIScoreStore(db, context="b", fingerprint="old-profile").get("same-id") is None
    assert AIScoreStore(db, context="a", fingerprint="changed-profile").get_many(["same-id"]) == {}
    assert AIScoreStore(db, context="a", fingerprint="old-profile").get("same-id") == .9


def test_ai_taste_requires_fresh_profile_and_context(tmp_path):
    db = tmp_path / "state.db"
    s = AIProfileStore(db, context="a", fingerprint="old")
    s.save({("actor", "ActorA")}, set())
    assert AIProfileStore(db, context="b", fingerprint="old").load() is None
    assert AIProfileStore(db, context="a", fingerprint="new").load() is None


def test_stale_ai_state_can_be_detected_for_a_complete_rollback(tmp_path):
    db = tmp_path / "state.db"
    AIProfileStore(db, context="a", fingerprint="old").save({("tag", "x")}, set())
    AIScoreStore(db, context="a", fingerprint="old").save("id", .8)
    assert AIProfileStore(db, context="a", fingerprint="new").load() is None
    assert AIProfileStore(db, context="a").has_saved()
    assert AIScoreStore(db, context="a").has_saved()


def test_context_and_cache_fingerprint_change_on_meaningful_inputs():
    from utils.state_context import state_context, score_fingerprint
    from utils.library import Item
    cfg = SimpleNamespace(jellyfin_server="example.invalid:8096", jellyfin_user_id="a",
                          jellyfin_db_path="", ai_model="model-a", ai_base_url="http://llm.invalid/v1")
    a = state_context(cfg)
    cfg.jellyfin_user_id = "b"
    assert state_context(cfg) != a
    cfg.jellyfin_user_id = "a"
    item = Item("id", "title", "Z:/synthetic.mp4", "Movie", 2024, tags=["tag"])
    base = score_fingerprint([item], {}, cfg)
    assert score_fingerprint([item], {"id": "PLAYED_LIKE"}, cfg) != base
    item.tags.append("changed")
    assert score_fingerprint([item], {}, cfg) != base
    item.tags.pop()
    cfg.ai_model = "model-b"
    assert score_fingerprint([item], {}, cfg) != base


def test_reverse_proxy_path_case_is_part_of_server_scope():
    from utils.state_context import state_context
    a = SimpleNamespace(library_source="api", jellyfin_server="HTTP://EXAMPLE.INVALID/Jellyfin",
                        jellyfin_user_id="viewer", jellyfin_db_path="")
    b = SimpleNamespace(library_source="api", jellyfin_server="http://example.invalid/jellyfin",
                        jellyfin_user_id="viewer", jellyfin_db_path="")
    assert state_context(a) != state_context(b)
    a.jellyfin_server = "http://example.invalid/Jellyfin"
    c = SimpleNamespace(library_source="api", jellyfin_server="HTTP://EXAMPLE.INVALID/Jellyfin",
                        jellyfin_user_id="viewer", jellyfin_db_path="")
    assert state_context(a) == state_context(c)


def test_clear_ai_rolls_back_all_ai_state_in_scope(tmp_path):
    from utils.state_context import clear_ai_state
    db = tmp_path / "state.db"
    AIScoreStore(db, context="a", fingerprint="v1").save("id", .9)
    AIProfileStore(db, context="a", fingerprint="v1").save({("tag", "x")}, set())
    AIScoreStore(db, context="b", fingerprint="v1").save("id", .7)
    clear_ai_state(db, context="a")
    assert AIScoreStore(db, context="a", fingerprint="v1").get("id") is None
    assert AIProfileStore(db, context="a", fingerprint="v1").load() is None
    assert AIScoreStore(db, context="b", fingerprint="v1").get("id") == .7


@pytest.mark.parametrize("mappings,path,want", [
    ([{"remote":"/media", "local":"X:/local"}], "/media/ABC/movie.mkv", "X:/local/ABC/movie.mkv"),
    ([{"remote":"/media", "local":"X:/local"}], "/media-other/movie.mkv", "/media-other/movie.mkv"),
    ([{"remote":"/media", "local":"X:/local"}], "/media/../secret.mkv", "/media/../secret.mkv"),
])
def test_remote_mapping_matches_path_segments_without_traversal(mappings, path, want):
    from utils.library_source import map_media_path
    assert map_media_path(path, mappings).replace("\\", "/") == want


def test_legacy_adoption_requires_actual_identity_and_never_overwrites(tmp_path):
    from utils.labels import preview_legacy_labels, adopt_legacy_labels
    from utils.library import Item
    from utils.state_context import state_context
    db = tmp_path / "state.db"
    old = LabelStore(db)
    old.set("matched", "KEEP")
    old.set("other", "DELETE")
    row = Item("matched", "Matched title", "Z:/matched.mp4", "Movie", 2024)
    cfg = SimpleNamespace(library_source="api", jellyfin_server="http://example.invalid",
                          jellyfin_user_id="viewer", jellyfin_db_path="")
    with pytest.raises(ValueError):
        preview_legacy_labels(db, cfg, [row])
    row.source_identity, row.behavior_user_id = "server:http://example.invalid", "viewer"
    assert preview_legacy_labels(db, cfg, [row]) == [{"item_id":"matched", "name":"Matched title", "label":"KEEP"}]
    assert adopt_legacy_labels(db, cfg, [row], ["other"]) == 0
    scope = LabelStore(db, context=state_context(cfg, [row]))
    scope.set("matched", "REVIEW")
    assert adopt_legacy_labels(db, cfg, [row], ["matched"]) == 0
    assert scope.get("matched") == "REVIEW"
    scope.remove("matched")
    assert adopt_legacy_labels(db, cfg, [row], ["matched"]) == 1
    assert scope.get("matched") == "KEEP"
    assert old.all() == {"matched":"KEEP", "other":"DELETE"}
    cfg.jellyfin_server = "http://another.invalid"
    with pytest.raises(ValueError):
        adopt_legacy_labels(db, cfg, [row], ["matched"])
