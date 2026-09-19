"""整体自动化编排器的纯函数测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import orchestrator as orch  # noqa: E402


def test_find_missing_subtitles(tmp_path):
    a = tmp_path / "with.mp4"
    a.write_bytes(b"a")
    (tmp_path / "with.srt").write_bytes(b"s")
    b = tmp_path / "without.mp4"
    b.write_bytes(b"b")
    missing = orch.find_missing_subtitles([str(a), str(b)], [".srt", ".vtt", ".ass"])
    assert missing == [str(b)]


def test_fix_nfo_directory(tmp_path):
    nfo = tmp_path / "movie.ts.nfo"
    nfo.write_bytes("hello .ts file".encode("utf-8"))
    poster = tmp_path / "movie.ts-poster.jpg"
    poster.write_bytes(b"img")
    fixed, renamed = orch.fix_nfo_directory(str(tmp_path), log=lambda _: None)
    assert fixed == 1
    assert renamed == 2
    assert (tmp_path / "movie.mp4.nfo").exists()
    assert (tmp_path / "movie.mp4-poster.jpg").exists()


def test_replace_restored_success(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    target = lib / "Movie.mp4"
    target.write_bytes(b"original")
    restored = tmp_path / "restored"
    restored.mkdir()
    source = restored / "Movie.restored.mp4"
    source.write_bytes(b"new")
    replaced, not_found = orch.replace_restored(
        [str(source)], [str(lib)], "restored", log=lambda _: None)
    assert replaced == 1
    assert not_found == 0
    assert target.read_bytes() == b"new"
    assert not source.exists()
