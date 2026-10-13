from pathlib import Path
import json
import sys
import zipfile

import pytest
from scripts import release_flow as rf, version_manage as vm
from tools.dev import smoke_exe


def make_zip(path, payload=b"MZ-test"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("JellyfinToolkit/JellyfinToolkit.exe", payload)
    return path


def test_version_bump_also_updates_fixed_resource_tuple():
    text = '__version__ = "3.9.0"\nVERSION_TUPLE = (3, 7, 0, 0)\n'
    bumped = vm.bump_version_py(text, "3.10.0")
    assert "VERSION_TUPLE = (3, 10, 0, 0)" in bumped


def test_repository_version_tuple_consistency():
    import version

    assert version.VERSION_TUPLE == tuple(map(int, version.__version__.split("."))) + (0,)


def test_archive_repeat_preserves_every_file_and_portable_user_data(tmp_path):
    source = make_zip(tmp_path / "app.zip")
    root = tmp_path / "archive"
    dest = rf.archive_version("9.1.0", source, root)
    data = dest / "运行" / "AppData" / "config.json"
    data.parent.mkdir()
    data.write_text("user edited this")
    before = {
        str(p.relative_to(dest)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in dest.rglob("*")
        if p.is_file()
    }
    rf.archive_version("9.1.0", source, root, notes="different notes")
    after = {
        str(p.relative_to(dest)): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in dest.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_archive_rejects_same_version_changed_payload(tmp_path):
    source = make_zip(tmp_path / "app.zip")
    root = tmp_path / "archive"
    rf.archive_version("9.1.0", source, root, extract=False)
    make_zip(source, b"MZ-different-size-and-content")
    with pytest.raises(ValueError, match="归档"):
        rf.archive_version("9.1.0", source, root, extract=False)


def test_archive_does_not_attribute_unknown_historical_tag_to_current_head(tmp_path, monkeypatch):
    monkeypatch.setattr(rf, "git", lambda *args: "current-head" if args[-1] == "HEAD" else "")
    dest = rf.archive_version(
        "0.0.1", make_zip(tmp_path / "app.zip"), tmp_path / "archive", extract=False
    )
    meta = json.loads((dest / "版本信息.json").read_text(encoding="utf-8"))
    assert meta["git_commit"] != "current-head"


def test_index_paths_are_relative_to_archive_root(tmp_path):
    root = tmp_path / "archive"
    rf.archive_version("9.1.0", make_zip(tmp_path / "app.zip"), root)
    text = rf.rebuild_index(root).read_text(encoding="utf-8")
    line = next(row for row in text.splitlines() if "直接打开用最新版" in row and "`" in row)
    assert (root / line.split("`")[1]).is_file()


def test_smoke_requires_an_actual_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["smoke_exe", "--exe", str(tmp_path / "missing.exe")])
    assert smoke_exe.main() != 0


def test_smoke_checks_actual_data_subdirectory_and_full_duration():
    assert smoke_exe.error_log_path(Path("isolated")) == Path("isolated") / "data" / "app-error.log"
    assert not smoke_exe.alive_is_sufficient(3, 6)
    assert smoke_exe.alive_is_sufficient(6, 6)


def test_build_cannot_skip_validation_without_matching_receipt(monkeypatch):
    from scripts import build_release

    monkeypatch.setattr(sys, "argv", ["build_release", "--skip-tests"])
    calls = []
    monkeypatch.setattr(build_release, "_run", lambda *a, **kw: calls.append(a) or 0)
    assert build_release.main() != 0
    assert calls == []


def test_windows_launchers_use_native_redirection_and_split_interpreter_arguments():
    root = Path(__file__).resolve().parents[1]
    for name in ("start.bat", "build.bat"):
        text = (root / name).read_text(encoding="ascii")
        assert "/dev/null" not in text
        assert "PY=py -3" not in text


def test_verification_receipt_rejects_changed_source(tmp_path, monkeypatch):
    from scripts import verify_release as verify

    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("first")
    monkeypatch.setattr(verify, "source_files", lambda _root: ["main.py"])
    monkeypatch.setattr(verify, "git_commit", lambda _root: "test-commit")
    receipt = tmp_path / "receipt.json"
    verify.write_receipt(root, receipt, {"pytest": 0, "ruff": 0, "compileall": 0, "version": 0})
    assert verify.validate_receipt(root, receipt)
    (root / "main.py").write_text("changed")
    with pytest.raises(ValueError, match="变化"):
        verify.validate_receipt(root, receipt)


def test_release_source_digest_handles_chinese_tracked_filenames(monkeypatch):
    from scripts import verify_release as verify

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.quotepath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    root = Path(__file__).resolve().parents[1]
    assert "docs/版本管理.md" in verify.source_files(root)
    assert len(verify.source_digest(root)) == 64


def test_demo_seed_refuses_to_overwrite_an_existing_database(tmp_path):
    from tools.dev.seed_demo import seed

    directory = tmp_path / "demo"
    directory.mkdir()
    database = directory / "jellyfin.db"
    database.write_bytes(b"user database")
    with pytest.raises(FileExistsError):
        seed(directory, 10)
    assert database.read_bytes() == b"user database"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:\\outside", "nested/app"])
def test_build_refuses_names_that_escape_artifact_directory(monkeypatch, name, capsys):
    from scripts import build_release

    monkeypatch.setattr(sys, "argv", ["build_release", "--name", name])
    calls = []
    monkeypatch.setattr(build_release, "_run", lambda *a, **kw: calls.append(a) or 0)
    assert build_release.main() == 1
    assert calls == []
    assert "--name" in capsys.readouterr().out


def test_build_refuses_source_change_after_smoke_before_manifest(tmp_path, monkeypatch):
    from scripts import build_release, verify_release

    monkeypatch.setattr(build_release, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_release", "--skip-tests", "--verification", "receipt"])
    validations = []

    def validate(*args):
        validations.append(args)
        if len(validations) > 1:
            raise ValueError("源码发生变化")
        return {"commit": "test", "source_digest": "digest"}

    monkeypatch.setattr(verify_release, "validate_receipt", validate)

    def run(cmd, **kwargs):
        if "PyInstaller" in cmd:
            exe = tmp_path / "dist" / build_release.APP_NAME / f"{build_release.APP_NAME}.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"test artifact")
        return 0

    monkeypatch.setattr(build_release, "_run", run)
    version = build_release.__version__
    monkeypatch.setattr(
        build_release,
        "_read_exe_version",
        lambda _: {
            "FileVersion": version,
            "ProductVersion": version,
            "FixedFileVersion": version + ".0",
            "FixedProductVersion": version + ".0",
        },
    )
    assert build_release.main() == 1
    assert len(validations) == 2
    assert not list(tmp_path.rglob("build-manifest.json"))
    assert not list(tmp_path.rglob("*.zip"))
