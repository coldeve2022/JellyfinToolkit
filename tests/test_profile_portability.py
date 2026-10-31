"""Portable launches and explicit development profiles never select a legacy root silently."""
from pathlib import Path
import json
import shutil
import sqlite3

import pytest


def test_default_root_is_program_even_with_old_global_registry(tmp_path, monkeypatch):
    import config
    from tests.test_data_profile import library
    from utils.data_profile import remember_profile
    program = tmp_path / 'new-program'
    program.mkdir()
    shared = tmp_path / 'user'
    remember_profile(library(tmp_path / 'v2'), shared / 'data-profile.json')
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(config.PORTABLE_ENV, raising=False)
    monkeypatch.setattr(config, 'program_dir', lambda: program)
    monkeypatch.setattr(config, 'user_data_root', lambda: shared)
    assert config._resolve_base_dir() == program


def test_local_pointer_uses_program_relative_path_from_any_cwd(tmp_path, monkeypatch):
    import config
    from utils.profile_runtime import write_local_pointer
    from tests.test_data_profile import library
    program = tmp_path / 'v4'
    program.mkdir()
    target = library(tmp_path / 'JellyfinToolkit-Data')
    write_local_pointer(program, target)
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(config, 'program_dir', lambda: program)
    monkeypatch.chdir(tmp_path.parent)
    assert config._resolve_base_dir() == target
    assert json.loads((program / 'data-profile.json').read_text())['root'] == '../JellyfinToolkit-Data'


def test_open_existing_missing_root_never_creates_it(tmp_path):
    from utils.profile_runtime import open_profile
    from utils.data_profile import ProfileError
    missing = tmp_path / 'typo'
    with pytest.raises(ProfileError):
        open_profile(missing)
    assert not missing.exists()


def test_missing_database_with_identity_does_not_reinitialize(tmp_path):
    from utils.profile_runtime import open_profile
    from utils.data_profile import ProfileError
    open_profile(tmp_path, create=True)
    (tmp_path / 'data/censor.db').unlink()
    with pytest.raises(ProfileError):
        open_profile(tmp_path, create=True)
    assert not (tmp_path / 'data/censor.db').exists()


def test_store_relative_assets_survive_whole_profile_move(tmp_path):
    from censor.store import VerdictStore
    from censor.models import FrameCandidate, VideoVerdict
    root = tmp_path / 'old'
    image = root / 'data/censor_thumbs/one.png'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'synthetic-picture')
    store = VerdictStore(root / 'data/censor.db')
    store.put(VideoVerdict('C:/media/file.mp4', candidates=[FrameCandidate(1., 2., thumb_path=str(image))]))
    with sqlite3.connect(store.db_path) as conn:
        value = json.loads(conn.execute('SELECT candidates FROM censor_probe').fetchone()[0])[0]['thumb_path']
    assert value == 'censor_thumbs/one.png'
    moved = tmp_path / 'moved'
    shutil.copytree(root, moved)
    other = VerdictStore(moved / 'data/censor.db')
    result = other.get('C:/media/file.mp4')
    assert Path(result.candidates[0].thumb_path) == moved / 'data/censor_thumbs/one.png'
    assert str(moved / 'data/censor_thumbs/one.png') in other.referenced_thumbnails()


def test_migration_keeps_source_and_rewrites_only_internal_assets(tmp_path):
    from utils.profile_migration import migrate_profile
    from tests.test_data_profile import library
    source = library(tmp_path / 'v2')
    image = source / 'data/censor_thumbs/example.png'
    image.parent.mkdir()
    image.write_bytes(b'synthetic-picture')
    (source / 'config.json').write_text('{"onboarding_done": true}', encoding='utf-8')
    with sqlite3.connect(source / 'data/censor.db') as conn:
        conn.execute('UPDATE censor_probe SET candidates=? WHERE path=?',
                     (json.dumps([dict(timestamp=1,score=2,thumb_path=str(image),crop_path='C:/external/image.png')]),'0'))
    before = (source / 'data/censor.db').read_bytes()
    target = tmp_path / 'JellyfinToolkit-Data'
    report = migrate_profile(source, target)
    assert (source / 'data/censor.db').read_bytes() == before
    assert report['tables']['censor.db']['censor_probe'] == 2
    assert report['rewritten_assets'] == 1
    assert (target / 'data/censor_thumbs/example.png').read_bytes() == image.read_bytes()
    with sqlite3.connect(target / 'data/censor.db') as conn:
        candidate = json.loads(conn.execute('SELECT candidates FROM censor_probe WHERE path=?',('0',)).fetchone()[0])[0]
    assert candidate['thumb_path'] == 'censor_thumbs/example.png'
    assert candidate['crop_path'] == 'C:/external/image.png'
    with pytest.raises(FileExistsError):
        migrate_profile(source, target)


def test_same_profile_cannot_be_opened_by_two_writers(tmp_path):
    from utils.profile_lock import ProfileLock
    from utils.data_profile import ProfileError
    first = ProfileLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(ProfileError):
            ProfileLock(tmp_path).acquire()
    finally:
        first.release()
    other = ProfileLock(tmp_path)
    other.acquire()
    other.release()


def test_old_global_profile_is_only_a_first_use_choice(tmp_path):
    from utils.profile_runtime import startup_selection
    from utils.data_profile import remember_profile, ProfileChoiceRequired
    from tests.test_data_profile import library
    program = tmp_path / 'program'
    program.mkdir()
    old = library(tmp_path / 'v2')
    registry = tmp_path / 'user/data-profile.json'
    remember_profile(old, registry)
    with pytest.raises(ProfileChoiceRequired):
        startup_selection(program, legacy_registry=registry)
    assert not (program / 'data').exists()
    root, create = startup_selection(program, legacy_registry=registry, choose=lambda rows: old)
    assert root == old and not create
    assert not (program / 'data-profile.json').exists()


def test_initialized_local_profile_ignores_stale_global_pointer(tmp_path):
    from utils.profile_runtime import open_profile, startup_selection
    program = tmp_path / 'program'
    open_profile(program, create=True)
    registry = tmp_path / 'user/data-profile.json'
    registry.parent.mkdir()
    registry.write_text('{broken')
    assert startup_selection(program, legacy_registry=registry) == (program, False)


def test_whole_profile_move_repairs_rollback_paths_without_changing_media(tmp_path):
    from utils.profile_runtime import open_profile
    old = tmp_path / 'old'
    open_profile(old, create=True)
    backup = old / 'data/quarantine/backup.bin'
    backup.parent.mkdir()
    backup.write_bytes(b'backup')
    with sqlite3.connect(old / 'data/operations.db') as db:
        db.execute('CREATE TABLE ops(id TEXT PRIMARY KEY,backup TEXT,original TEXT,payload TEXT)')
        db.execute('INSERT INTO ops VALUES(?,?,?,?)', ('one', str(backup), 'C:/library/video.mp4',
            json.dumps({'backup':str(backup),'original':'C:/library/video.mp4'})))
    moved = tmp_path / 'moved'
    shutil.copytree(old, moved)
    identity = json.loads((old / 'profile.json').read_text())['profile_id']
    open_profile(moved)
    with sqlite3.connect(moved / 'data/operations.db') as db:
        backup_path, media, raw = db.execute('SELECT backup,original,payload FROM ops').fetchone()
    assert Path(backup_path) == moved / 'data/quarantine/backup.bin'
    assert media == json.loads(raw)['original'] == 'C:/library/video.mp4'
    assert Path(json.loads(raw)['backup']).is_file()
    assert json.loads((moved / 'profile.json').read_text())['profile_id'] == identity


def test_external_assets_and_relative_traversal_are_not_redirected(tmp_path):
    from utils.profile_assets import store_asset, resolve_asset
    outside = tmp_path / 'outside.png'
    assert store_asset(str(outside), tmp_path / 'data') == str(outside)
    assert resolve_asset('../outside.png', tmp_path / 'data') == ''
    assert resolve_asset('censor_thumbs/../../outside.png', tmp_path / 'data') == ''


def test_future_snapshot_schema_blocks_before_profile_is_adopted(tmp_path):
    from tests.test_data_profile import library
    from utils.profile_runtime import open_profile
    from utils.data_profile import ProfileError
    root = library(tmp_path / 'profile')
    with sqlite3.connect(root / 'data/jellyfin_review.sqlite3') as db:
        db.execute('CREATE TABLE user_state_schema(id INTEGER PRIMARY KEY,version INTEGER)')
        db.execute('INSERT INTO user_state_schema VALUES(1,99)')
    before = (root / 'data/censor.db').read_bytes()
    with pytest.raises(ProfileError):
        open_profile(root)
    assert not (root / 'profile.json').exists()
    assert (root / 'data/censor.db').read_bytes() == before


def test_source_change_aborts_publication_and_leaves_source_intact(tmp_path):
    from tests.test_data_profile import library
    from utils.profile_migration import migrate_profile
    from utils.data_profile import ProfileError
    root = library(tmp_path / 'source')
    config = root / 'config.json'
    config.write_text('{}')
    target = tmp_path / 'target'
    def mutate(state):
        if state['phase'] == 'copy':
            config.write_text('{"changed":true}')
    with pytest.raises(ProfileError, match='变化'):
        migrate_profile(root, target, progress=mutate)
    assert not target.exists()
    assert (root / 'data/censor.db').exists()


def test_os_lock_excludes_a_separate_process(tmp_path):
    import subprocess
    import sys
    from utils.profile_lock import ProfileLock
    with ProfileLock(tmp_path):
        code = ('from utils.profile_lock import ProfileLock; '
                'from utils.data_profile import ProfileError; '
                'import sys\ntry: ProfileLock(sys.argv[1]).acquire()\n'
                'except ProfileError: raise SystemExit(7)\n')
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, timeout=20)
    assert result.returncode == 7, result.stderr.decode(errors='replace')


def test_wal_only_commit_aborts_migration_publication(tmp_path):
    from tests.test_data_profile import library
    from utils.profile_migration import migrate_profile
    from utils.data_profile import ProfileError
    root = library(tmp_path / 'source')
    with sqlite3.connect(root / 'data/censor.db') as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        before = (root / 'data/censor.db').stat()
        changed = []
        def mutate(state):
            if state['phase'] == 'copy' and not changed:
                writer.execute("UPDATE censor_probe SET reason='new WAL-only commit'")
                writer.commit()
                changed.append(True)
        with pytest.raises(ProfileError, match='变化'):
            migrate_profile(root, tmp_path / 'target', progress=mutate)
        after = (root / 'data/censor.db').stat()
        assert before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    assert not (tmp_path / 'target').exists()


def test_release_package_rejects_profile_and_development_artifacts(tmp_path):
    from scripts.build_release import assert_clean_package
    (tmp_path / 'JellyfinToolkit.exe').write_bytes(b'fixture')
    assert_clean_package(tmp_path)
    for name in ('config.json', 'data-profile.json', 'profile.json', 'dev-start.bat'):
        path = tmp_path / name
        path.write_text('fixture')
        with pytest.raises(ValueError):
            assert_clean_package(tmp_path)
        path.unlink()


def test_relocation_repairs_internal_backup_configuration_only(tmp_path):
    from utils.profile_runtime import open_profile
    old = tmp_path / 'old'
    open_profile(old, create=True)
    config = {'merge_backup_root':str(old / 'backups'),
              'outside':str(tmp_path / 'external-tool.exe'), 'server':'http://server.invalid'}
    (old / 'config.json').write_text(json.dumps(config))
    moved = tmp_path / 'moved'
    shutil.copytree(old, moved)
    open_profile(moved)
    revised = json.loads((moved / 'config.json').read_text())
    assert revised['merge_backup_root'] == str(moved / 'backups')
    assert revised['outside'] == config['outside'] and revised['server'] == config['server']


def test_clean_package_smoke_requires_initialized_profile(tmp_path):
    from tools.dev.smoke_exe import profile_was_initialized
    assert not profile_was_initialized(tmp_path)
    (tmp_path / 'profile.json').write_text('{}')
    assert not profile_was_initialized(tmp_path)
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/censor.db').write_bytes(b'fixture')
    assert profile_was_initialized(tmp_path)


def test_source_digest_excludes_an_intentionally_deleted_tracked_module(tmp_path, monkeypatch):
    from scripts import verify_release
    def git(root, *args):
        return 'removed.py\0' if '--deleted' in args else 'main.py\0removed.py\0'
    monkeypatch.setattr(verify_release, '_git', git)
    (tmp_path / 'main.py').write_text('pass')
    assert verify_release.source_files(tmp_path) == ['main.py']
    assert len(verify_release.source_digest(tmp_path)) == 64


def test_rebasing_external_paths_does_not_probe_their_filesystem(tmp_path, monkeypatch):
    from utils.profile_migration import _rebase
    original = Path.resolve
    external = str(tmp_path / 'outside/video.mp4')
    def guarded(path, *args, **kwargs):
        assert str(path) != external
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', guarded)
    assert _rebase(external, tmp_path / 'profile', tmp_path / 'target') == external


def test_owned_asset_resolution_does_not_stat_every_thumbnail(tmp_path, monkeypatch):
    from utils.profile_assets import resolve_asset
    def forbidden(*args, **kwargs):
        raise AssertionError('relative evidence decoding must not stat the filesystem')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    assert Path(resolve_asset('censor_thumbs/a.png', tmp_path / 'data')) == tmp_path / 'data/censor_thumbs/a.png'
