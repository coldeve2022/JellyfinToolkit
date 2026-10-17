"""Old manual feedback remains reviewable when a library selection creates a scope."""
from types import SimpleNamespace

from utils.labels import (
    LabelStore, adopt_legacy_labels, legacy_protection_labels, preview_legacy_labels,
)
from utils.library import Item
from utils.profile import build_profile
from utils.state_context import source_viewer_context, state_context


def _snapshot():
    cfg = SimpleNamespace(
        library_source="api", jellyfin_server="http://example.invalid",
        jellyfin_user_id="viewer", jellyfin_db_path="",
        library_scope_ids=["anime"], library_scope_paths=[],
    )
    rows = [Item(key, key, f"Z:/{key}.mp4", "Movie", 2024,
                 source_identity="server:http://example.invalid", behavior_user_id="viewer")
            for key in ("matched", "other")]
    return cfg, rows


def test_old_source_viewer_feedback_only_protects_matching_items(tmp_path):
    cfg, rows = _snapshot()
    db = tmp_path / "smart.db"
    old = LabelStore(db, context=source_viewer_context(cfg, rows))
    old.set("matched", "PLAYED_LIKE")
    old.set("outside-snapshot", "KEEP")
    old.set("other", "DISLIKE")
    current = LabelStore(db, context=state_context(cfg, rows))
    assert current.all() == {}
    assert legacy_protection_labels(db, rows, current.all(), cfg=cfg) == {"matched": "KEEP"}
    assert build_profile(rows, current.all())["n_pos"] == 0
    assert current.all() == {}


def test_explicit_scope_adoption_prefers_previous_scoped_feedback(tmp_path):
    cfg, rows = _snapshot()
    db = tmp_path / "smart.db"
    raw = LabelStore(db)
    raw.set("matched", "DELETE")
    raw.set("other", "KEEP")
    old = LabelStore(db, context=source_viewer_context(cfg, rows))
    old.set("matched", "PLAYED_LIKE")
    old.set("outside-snapshot", "DELETE")
    current = LabelStore(db, context=state_context(cfg, rows))
    assert preview_legacy_labels(db, cfg, rows) == [
        {"item_id": "matched", "name": "matched", "label": "PLAYED_LIKE"},
        {"item_id": "other", "name": "other", "label": "KEEP"},
    ]
    assert current.all() == {}
    assert adopt_legacy_labels(db, cfg, rows, ["matched", "outside-snapshot"]) == 1
    assert current.all() == {"matched": "PLAYED_LIKE"}
    assert raw.all() == {"matched": "DELETE", "other": "KEEP"}
    assert old.get("matched") == "PLAYED_LIKE"


def test_existing_target_feedback_wins_over_old_protection_and_adoption(tmp_path):
    cfg, rows = _snapshot()
    db = tmp_path / "smart.db"
    LabelStore(db, context=source_viewer_context(cfg, rows)).set("matched", "KEEP")
    current = LabelStore(db, context=state_context(cfg, rows))
    current.set("matched", "REVIEW")
    assert legacy_protection_labels(db, rows, current.all(), cfg=cfg) == {}
    assert preview_legacy_labels(db, cfg, rows) == []
    assert adopt_legacy_labels(db, cfg, rows, ["matched"]) == 0
    assert current.get("matched") == "REVIEW"


def test_prior_scoped_negative_overrides_oldest_raw_positive(tmp_path):
    cfg, rows = _snapshot()
    db = tmp_path / "smart.db"
    LabelStore(db).set("matched", "KEEP")
    LabelStore(db, context=source_viewer_context(cfg, rows)).set("matched", "DISLIKE")
    assert legacy_protection_labels(db, rows, cfg=cfg) == {}
    assert LabelStore(db, context=state_context(cfg, rows)).all() == {}

