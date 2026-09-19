"""已损坏旧合并的检测与修复纯函数测试。"""
from pathlib import Path

from utils.merge_repair import archived_merge_items, validate_video_file


def test_archived_merge_items_filters_unavailable_sources(tmp_path):
    a = tmp_path / "ABC-cd1.mp4"
    b = tmp_path / "ABC-cd2.mp4"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    final = tmp_path / "ABC.mp4"
    final.write_bytes(b"merged")

    entries = [
        {"number": "ABC", "kind": "media", "original_path": str(a), "backup_path": ""},
        {"number": "ABC", "kind": "media", "original_path": str(b), "backup_path": ""},
        {"number": "ABC", "kind": "merged_output", "final_path": str(final)},
        {"number": "XYZ", "kind": "media", "original_path": str(tmp_path / "missing.mp4"), "backup_path": ""},
        {"number": "XYZ", "kind": "merged_output", "final_path": str(tmp_path / "missing-final.mp4")},
    ]
    items = archived_merge_items(entries)
    assert len(items) == 1
    assert items[0]["number"] == "ABC"
    assert items[0]["original_count"] == 2
    assert len(items[0]["media_entries"]) == 2


def test_archived_merge_items_deduplicates_same_original(tmp_path):
    original = tmp_path / "ABC-cd1.mp4"
    original.write_bytes(b"a")
    backup = tmp_path / "backup" / "ABC-cd1.mp4"
    backup.parent.mkdir()
    backup.write_bytes(b"a")
    final = tmp_path / "ABC.mp4"
    final.write_bytes(b"merged")

    entries = [
        {"number": "ABC", "kind": "media", "original_path": str(original), "backup_path": ""},
        {"number": "ABC", "kind": "media", "original_path": str(original), "backup_path": str(backup)},
        {"number": "ABC", "kind": "merged_output", "final_path": str(final)},
    ]
    items = archived_merge_items(entries)
    assert len(items[0]["media_entries"]) == 1
    assert items[0]["backup_count"] == 1


def test_validate_video_file_rejects_missing_path():
    ok, detail = validate_video_file("ffmpeg", str(Path("/no/such/video.mp4")))
    assert not ok
    assert "不存在" in detail

