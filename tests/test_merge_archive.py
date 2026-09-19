"""分集合并替换归档 / NFO 同步 / 单文件回滚测试。"""
from pathlib import Path
from types import SimpleNamespace

from utils.merge_archive import archive_group_for_merge, restore_entry


def _cfg(backup_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        merge_backup_root=str(backup_root),
        merge_original_subfolder="_merged_originals",
        merge_rename_to_number=True,
    )


def test_archive_renames_output_syncs_nfo_and_moves_originals(tmp_path):
    media = tmp_path / "media"
    backup = tmp_path / "backup"
    media.mkdir()
    (media / "MIAA-743-cd1.mp4").write_bytes(b"V1")
    (media / "MIAA-743-cd2.mp4").write_bytes(b"V2")
    (media / "MIAA-743-cd1.nfo").write_text("<movie><title>Original</title></movie>", encoding="utf-8")
    (media / "MIAA-743 合并.mp4").write_bytes(b"MERGED")

    originals = [str(media / "MIAA-743-cd1.mp4"), str(media / "MIAA-743-cd2.mp4")]
    ok, entries, errors, final = archive_group_for_merge(
        _cfg(backup), "MIAA-743", originals, str(media / "MIAA-743 合并.mp4"))

    assert ok, errors
    assert final == str(media / "MIAA-743.mp4")
    assert not (media / "MIAA-743-cd1.mp4").exists()
    assert not (media / "MIAA-743-cd2.mp4").exists()
    assert (media / "MIAA-743.mp4").exists()
    assert (media / "MIAA-743.nfo").exists()
    assert "Original" in (media / "MIAA-743.nfo").read_text(encoding="utf-8")
    assert {e["kind"] for e in entries} >= {"media", "merged_output", "nfo_final", "sidecar"}


def test_archive_moves_language_subtitles(tmp_path):
    media = tmp_path / "media"
    backup = tmp_path / "backup"
    media.mkdir()
    (media / "MIAA-743-cd1.mp4").write_bytes(b"V1")
    (media / "MIAA-743-cd2.mp4").write_bytes(b"V2")
    (media / "MIAA-743-cd1.zh.srt").write_text("1", encoding="utf-8")
    (media / "MIAA-743-cd1.chi.ass").write_text("2", encoding="utf-8")
    (media / "MIAA-743-cd2.default.srt").write_text("3", encoding="utf-8")
    (media / "MIAA-743 合并.mp4").write_bytes(b"MERGED")

    originals = [str(media / "MIAA-743-cd1.mp4"), str(media / "MIAA-743-cd2.mp4")]
    ok, entries, errors, _ = archive_group_for_merge(
        _cfg(backup), "MIAA-743", originals, str(media / "MIAA-743 合并.mp4"))

    assert ok, errors
    assert not (media / "MIAA-743-cd1.zh.srt").exists()
    assert not (media / "MIAA-743-cd1.chi.ass").exists()
    assert not (media / "MIAA-743-cd2.default.srt").exists()
    backup_dir = backup / "_merged_originals" / "MIAA-743"
    assert (backup_dir / "MIAA-743-cd1.zh.srt").exists()
    assert (backup_dir / "MIAA-743-cd1.chi.ass").exists()
    assert (backup_dir / "MIAA-743-cd2.default.srt").exists()


def test_restore_single_media_entry(tmp_path):
    media = tmp_path / "media"
    backup = tmp_path / "backup"
    media.mkdir()
    (media / "ABC-123-cd1.mp4").write_bytes(b"V1")
    (media / "ABC-123-cd2.mp4").write_bytes(b"V2")
    (media / "ABC-123 合并.mp4").write_bytes(b"MERGED")

    originals = [str(media / "ABC-123-cd1.mp4"), str(media / "ABC-123-cd2.mp4")]
    ok, entries, errors, _ = archive_group_for_merge(
        _cfg(backup), "ABC-123", originals, str(media / "ABC-123 合并.mp4"))
    assert ok, errors

    media_entry = next(e for e in entries if e["kind"] == "media"
                       and e["original_path"].endswith("ABC-123-cd1.mp4"))
    ok2, detail = restore_entry(media_entry)
    assert ok2, detail
    assert (media / "ABC-123-cd1.mp4").exists()
    assert not Path(media_entry["backup_path"]).exists()


def test_archive_keeps_number_nfo_when_it_is_the_source(tmp_path):
    media = tmp_path / "media"
    backup = tmp_path / "backup"
    media.mkdir()
    (media / "MIAA-743.mp4").write_bytes(b"V1")
    (media / "MIAA-743.nfo").write_text("<movie><title>KeepMe</title></movie>", encoding="utf-8")
    (media / "MIAA-743 合并.mp4").write_bytes(b"MERGED")

    ok, entries, errors, final = archive_group_for_merge(
        _cfg(backup), "MIAA-743", [str(media / "MIAA-743.mp4")],
        str(media / "MIAA-743 合并.mp4"))

    assert ok, errors
    assert final == str(media / "MIAA-743.mp4")
    assert (media / "MIAA-743.nfo").exists()
    assert "KeepMe" in (media / "MIAA-743.nfo").read_text(encoding="utf-8")


def test_archive_records_merged_output_when_rename_disabled(tmp_path):
    media = tmp_path / "media"
    backup = tmp_path / "backup"
    media.mkdir()
    (media / "MIAA-743-cd1.mp4").write_bytes(b"V1")
    (media / "MIAA-743-cd2.mp4").write_bytes(b"V2")
    (media / "MIAA-743 合并.mp4").write_bytes(b"MERGED")
    cfg = SimpleNamespace(
        merge_backup_root=str(backup),
        merge_original_subfolder="_merged_originals",
        merge_rename_to_number=False,
    )
    originals = [str(media / "MIAA-743-cd1.mp4"), str(media / "MIAA-743-cd2.mp4")]
    ok, entries, errors, final = archive_group_for_merge(
        cfg, "MIAA-743", originals, str(media / "MIAA-743 合并.mp4"))
    assert ok, errors
    assert final == str(media / "MIAA-743 合并.mp4")
    assert any(e["kind"] == "merged_output" and e["final_path"] == final for e in entries)
