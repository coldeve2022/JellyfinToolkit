"""A library is persistent user data, independent of a release directory."""
import hashlib
from contextlib import closing
import json
import sqlite3
from pathlib import Path

import pytest


def library(root, probes=2, labels=1):
    database = root / 'data/censor.db'
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as conn, conn:
        from censor.store import _SCHEMA
        conn.executescript(_SCHEMA)
        conn.executemany('INSERT INTO censor_probe(path) VALUES(?)', [(str(i),) for i in range(probes)])
        conn.executemany("INSERT INTO censor_label(path,state) VALUES(?,'censored')", [(str(i),) for i in range(labels)])
    return root


def arguments(tmp_path):
    shared = tmp_path / 'user/JellyfinToolkit'
    program = tmp_path / 'project/dist/v3.22.1/JellyfinToolkit'
    program.mkdir(parents=True)
    return dict(default_root=shared, registry=shared / 'data-profile.json',
                program_root=program, shared_root=shared)


def test_only_legacy_library_is_adopted_without_copy_or_database_write(tmp_path):
    from utils.data_profile import prepare_profile, registered_root
    args = arguments(tmp_path)
    old = library(tmp_path / 'project')
    db = old / 'data/censor.db'
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert prepare_profile(**args) == old.resolve()
    assert registered_root(args['registry']) == old.resolve()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not (args['shared_root'] / 'data/censor.db').exists()


def test_registered_library_survives_program_move_and_version_change(tmp_path):
    from utils.data_profile import prepare_profile, remember_profile
    args = arguments(tmp_path)
    old = library(tmp_path / 'library')
    remember_profile(old, args['registry'])
    args['program_root'] = tmp_path / 'unrelated/new-release'
    args['program_root'].mkdir(parents=True)
    assert prepare_profile(**args, choose=lambda _: pytest.fail('Must use registered library')) == old.resolve()


def test_two_populated_libraries_require_choice_and_never_overwrite(tmp_path):
    from utils.data_profile import prepare_profile, ProfileChoiceRequired
    args = arguments(tmp_path)
    old = library(tmp_path / 'project', 8, 3)
    other = library(args['shared_root'], 4, 0)
    with pytest.raises(ProfileChoiceRequired) as error:
        prepare_profile(**args)
    counts = {str(row['root']): (row['scans'], row['labels']) for row in error.value.candidates}
    assert counts[str(old.resolve())] == (8, 3)
    assert counts[str(other.resolve())] == (4, 0)
    assert not args['registry'].exists()
    assert not list(old.glob('**/*backup*'))


def test_explicit_selection_is_remembered_once(tmp_path):
    from utils.data_profile import prepare_profile, registered_root
    args = arguments(tmp_path)
    old = library(tmp_path / 'project', 8, 3)
    library(args['shared_root'], 4, 0)
    assert prepare_profile(**args, choose=lambda rows: old) == old.resolve()
    assert registered_root(args['registry']) == old.resolve()


def test_missing_registered_library_blocks_instead_of_starting_empty(tmp_path):
    from utils.data_profile import prepare_profile, remember_profile, ProfileError
    args = arguments(tmp_path)
    old = library(tmp_path / 'library')
    remember_profile(old, args['registry'])
    db = old / 'data/censor.db'
    db.unlink()
    with pytest.raises(ProfileError, match='资料库'):
        prepare_profile(**args)
    assert not db.exists()
    assert not (args['shared_root'] / 'data/censor.db').exists()


@pytest.mark.parametrize('option', ['explicit', 'portable'])
def test_explicit_and_portable_launches_never_change_shared_profile(tmp_path, option):
    from utils.data_profile import prepare_profile, remember_profile
    args = arguments(tmp_path)
    old = library(tmp_path / 'library')
    remember_profile(old, args['registry'])
    before = args['registry'].read_bytes()
    override = tmp_path / 'isolated'
    args['default_root'] = override
    assert prepare_profile(**args, **{option: True}) == override.resolve()
    assert args['registry'].read_bytes() == before
    assert not override.exists()


def test_bad_or_future_registry_blocks_without_replacing_it(tmp_path):
    from utils.data_profile import prepare_profile, ProfileError
    args = arguments(tmp_path)
    args['registry'].parent.mkdir(parents=True)
    for content in ('{broken', json.dumps({'schema_version': 99, 'root': str(tmp_path)})):
        args['registry'].write_text(content, encoding='utf-8')
        with pytest.raises(ProfileError):
            prepare_profile(**args)
        assert args['registry'].read_text(encoding='utf-8') == content


def test_failed_atomic_save_keeps_previous_profile(tmp_path, monkeypatch):
    from utils import data_profile
    args = arguments(tmp_path)
    old = library(tmp_path / 'library')
    data_profile.remember_profile(old, args['registry'])
    before = args['registry'].read_bytes()
    new = library(tmp_path / 'another')
    monkeypatch.setattr(data_profile.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('fixture')))
    with pytest.raises(OSError):
        data_profile.remember_profile(new, args['registry'])
    assert args['registry'].read_bytes() == before
    assert not list(args['registry'].parent.glob('*.tmp'))


def test_new_install_uses_shared_version_independent_root(tmp_path):
    from utils.data_profile import prepare_profile, registered_root
    args = arguments(tmp_path)
    assert prepare_profile(**args) == args['shared_root'].resolve()
    assert registered_root(args['registry']) == args['shared_root'].resolve()


def test_cancel_or_foreign_selection_does_not_create_profile(tmp_path):
    from utils.data_profile import prepare_profile, ProfileError
    args = arguments(tmp_path)
    library(tmp_path / 'project')
    library(args['shared_root'], 4, 0)
    for choice in (None, tmp_path / 'not-listed'):
        with pytest.raises(ProfileError):
            prepare_profile(**args, choose=lambda rows: choice)
        assert not args['registry'].exists()


def test_config_requires_local_selection_for_source_and_frozen(tmp_path, monkeypatch):
    import config
    from utils.data_profile import remember_profile
    shared = tmp_path / 'user'
    old = library(tmp_path / 'library')
    remember_profile(old, shared / 'data-profile.json')
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(config.PORTABLE_ENV, raising=False)
    monkeypatch.setattr(config, 'portable_mode', lambda: False)
    monkeypatch.setattr(config, 'user_data_root', lambda: shared)
    monkeypatch.setattr(config, 'program_dir', lambda: tmp_path / 'release')
    for frozen in (False, True):
        monkeypatch.setattr(config.sys, 'frozen', frozen, raising=False)
        assert config._resolve_base_dir() == tmp_path / 'release'
    from utils.profile_runtime import write_local_pointer
    write_local_pointer(tmp_path / 'release', old)
    assert config._resolve_base_dir() == old.resolve()


def test_broken_database_is_not_registered(tmp_path):
    from utils.data_profile import remember_profile, ProfileError
    root = tmp_path / 'broken'
    (root / 'data').mkdir(parents=True)
    (root / 'data/censor.db').write_bytes(b'not sqlite')
    registry = tmp_path / 'user/data-profile.json'
    with pytest.raises(ProfileError):
        remember_profile(root, registry)
    assert not registry.exists()


def test_future_schema_and_invalid_base_columns_are_rejected_before_registration(tmp_path):
    from utils.data_profile import remember_profile, ProfileError
    root = library(tmp_path / 'future')
    registry = tmp_path / 'user/data-profile.json'
    with closing(sqlite3.connect(root / 'data/censor.db')) as db, db:
        db.execute('CREATE TABLE toolkit_schema_versions(component TEXT PRIMARY KEY,version INTEGER)')
        db.execute("INSERT INTO toolkit_schema_versions VALUES('censor_verdict',99)")
    with pytest.raises(ProfileError):
        remember_profile(root, registry)
    assert not registry.exists()
    with closing(sqlite3.connect(root / 'data/censor.db')) as db, db:
        db.execute('DROP TABLE toolkit_schema_versions')
        db.execute('ALTER TABLE censor_probe RENAME TO bad_probe')
        db.execute('CREATE TABLE censor_probe(path TEXT PRIMARY KEY)')
    with pytest.raises(ProfileError):
        remember_profile(root, registry)
    assert not registry.exists()


def test_first_install_marks_database_required_before_returning(tmp_path):
    from utils.data_profile import prepare_profile, ProfileError, read_profile
    args = arguments(tmp_path)
    root = prepare_profile(**args)
    assert read_profile(args['registry'])['database_required'] is True
    database = root / 'data/censor.db'
    assert database.exists()
    database.unlink()
    with pytest.raises(ProfileError):
        prepare_profile(**args)
    assert not database.exists()
