"""Application merge originals must not become a new processing queue."""

import pytest

from utils.file_scan import iter_media_files


@pytest.mark.parametrize("selection", ["root", "archive", "file"])
def test_merge_original_backup_is_not_rediscovered(tmp_path, selection):
    main = tmp_path / "MAIN-101.mp4"
    main.write_bytes(b"synthetic main")
    archive = tmp_path / "_merged_originals" / "session"
    archive.mkdir(parents=True)
    old = archive / "MAIN-101-cd1.mp4"
    old.write_bytes(b"synthetic archived original")
    similar = tmp_path / "_merged_originals_notes"
    similar.mkdir()
    unrelated = similar / "MAIN-102.mp4"
    unrelated.write_bytes(b"synthetic unrelated folder")
    selected = {"root": tmp_path, "archive": archive, "file": old}[selection]
    expected = {str(main), str(unrelated)} if selection == "root" else set()
    assert set(iter_media_files([selected])) == expected
    assert old.read_bytes() == b"synthetic archived original"
