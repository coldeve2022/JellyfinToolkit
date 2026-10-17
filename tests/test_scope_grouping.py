"""Selecting all libraries must not turn matching numbers into cross-library writes."""
from utils.library import Item, BUCKET_JAV, BUCKET_UNC
from utils.merge import group_by_number, build_merge_plan
from utils.scoring import dedup_candidate_groups, dedup_groups, dedup_loser_ids


def _item(key, library_ids=(), bucket=BUCKET_JAV, size=100):
    return Item(key, key, f"Z:/{key}.mp4", "Movie", 2024,
                num="ABC-123", bucket=bucket, library_ids=list(library_ids), size=size * 1024 * 1024)


def test_dedup_partitions_known_library_memberships():
    rows = [_item("movie-a", ["movies"], size=200), _item("movie-b", ["movies"]),
            _item("anime-a", ["anime"], size=200), _item("anime-b", ["anime"])]
    groups = dedup_groups(rows)
    assert {frozenset(it.id for it in group["items"]) for group in groups} == {
        frozenset({"movie-a", "movie-b"}), frozenset({"anime-a", "anime-b"})}
    assert {group["num"] for group in groups} == {"ABC-123"}
    assert len({group["group_id"] for group in groups}) == 2
    assert dedup_loser_ids(rows) == {"movie-b", "anime-b"}
    assert {group["group_id"] for group in dedup_candidate_groups(rows)} == {
        group["group_id"] for group in groups}


def test_dedup_keeps_uncensored_versions_and_membership_order_together():
    rows = [_item("a", ["shared", "movies"]),
            _item("b", ["movies", "shared"], bucket=BUCKET_UNC)]
    assert len(dedup_groups(rows)) == 1
    assert dedup_groups(list(reversed(rows)))[0]["group_id"] == dedup_groups(rows)[0]["group_id"]


def test_dedup_unknown_membership_retains_legacy_behavior_but_does_not_bridge_known():
    rows = [_item("unknown-a"), _item("unknown-b"), _item("known", ["movies"])]
    groups = dedup_groups(rows)
    assert len(groups) == 1
    assert {it.id for it in groups[0]["items"]} == {"unknown-a", "unknown-b"}


def test_merge_same_parent_retains_legacy_number_key(tmp_path):
    paths = [str(tmp_path / name) for name in ("ABC-123-CD1.mp4", "ABC-123-CD2.mp4")]
    groups, skipped = group_by_number(paths)
    assert list(groups) == ["ABC123"]
    assert {part.path for part in groups["ABC123"]} == set(paths)
    assert skipped == []


def test_merge_skips_ambiguous_number_across_different_parents(tmp_path):
    paths = [str(tmp_path / "movies" / "ABC-123-CD1.mp4"),
             str(tmp_path / "anime" / "ABC-123-CD2.mp4")]
    groups, skipped = group_by_number(paths)
    assert groups == {}
    assert set(skipped) == set(paths)
    plan = build_merge_plan(paths)
    assert plan.stats["cross_folder_skipped"] == 2
    assert set(plan.stats["cross_folder_skipped_paths"]) == set(paths)
    assert "不同来源目录" in plan.stats["cross_folder_skip_reason"]


def test_merge_does_not_hide_unrelated_safe_number_when_collision_is_skipped(tmp_path):
    paths = [str(tmp_path / "a" / "ABC-123-CD1.mp4"),
             str(tmp_path / "b" / "ABC-123-CD2.mp4"),
             str(tmp_path / "a" / "XYZ-456-CD1.mp4"),
             str(tmp_path / "a" / "XYZ-456-CD2.mp4")]
    groups, skipped = group_by_number(paths)
    assert list(groups) == ["XYZ456"]
    assert set(skipped) == set(paths[:2])
