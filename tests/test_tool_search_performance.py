from pathlib import Path


def test_tool_discovery_does_not_stat_every_unrelated_media_file(tmp_path, monkeypatch):
    from utils.tool_search import exe_in_dir

    for i in range(200):
        (tmp_path / f"movie-{i}.mp4").touch()
    calls = []
    original = Path.is_file
    def is_file(path):
        calls.append(str(path))
        return original(path)
    monkeypatch.setattr(Path, "is_file", is_file)
    assert exe_in_dir(tmp_path, ["lada-cli.exe"]) is None
    assert len(calls) <= 2


def test_tool_discovery_nested_case_and_nonfile_candidate(tmp_path):
    from utils.tool_search import exe_in_dir

    (tmp_path / "lada-cli.exe").mkdir()
    inner = tmp_path / "release"
    inner.mkdir()
    executable = inner / "LADA-CLI.EXE"
    executable.touch()
    assert exe_in_dir(tmp_path, ["lada-cli.exe"]) == executable
