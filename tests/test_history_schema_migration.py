import sqlite3

import pytest

from censor.store import VerdictStore


def legacy(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE censor_label(path TEXT PRIMARY KEY,state TEXT,note TEXT,labeled_at TEXT,custom TEXT)")
        conn.execute("INSERT INTO censor_label VALUES('old.mp4','censored','人工确认','2020','keep')")
        conn.execute("CREATE TABLE other_component(value TEXT)")
        conn.execute("INSERT INTO other_component VALUES('untouched')")
        conn.execute("PRAGMA user_version=73")


def backups(path):
    return list((path.parent / 'backups').glob('*.sqlite3'))


def test_legacy_preserves_manual_rows_extra_columns_and_other_component(tmp_path):
    path = tmp_path / 'history.db'
    legacy(path)
    VerdictStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM censor_label").fetchone() == ('old.mp4', 'censored', '人工确认', '2020', 'keep')
        assert conn.execute("SELECT * FROM censor_label_history").fetchone() == ('old.mp4', '', 'censored', '人工确认', '2020')
        assert conn.execute("SELECT version FROM toolkit_schema_versions WHERE component='censor_verdict'").fetchone() == (1,)
        assert conn.execute("PRAGMA user_version").fetchone() == (73,)
        assert conn.execute("SELECT * FROM other_component").fetchone() == ('untouched',)


def test_backup_is_consistent_pre_upgrade_and_not_repeated(tmp_path):
    path = tmp_path / 'history.db'
    legacy(path)
    # Keep committed data in WAL to catch unsafe raw-file copies.
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute("INSERT INTO other_component VALUES('wal row')")
    writer.commit()
    try:
        VerdictStore(path)
        assert len(backups(path)) == 1
        with sqlite3.connect(backups(path)[0]) as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert conn.execute('SELECT * FROM other_component').fetchall() == [('untouched',), ('wal row',)]
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='toolkit_schema_versions'").fetchone()
        VerdictStore(path)
        assert len(backups(path)) == 1
    finally:
        writer.close()


def test_new_database_needs_no_backup(tmp_path):
    path = tmp_path / 'history.db'
    VerdictStore(path)
    assert not backups(path)


def test_migration_failure_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / 'history.db'
    legacy(path)
    original = VerdictStore._migrate_v1

    def fail(self, conn):
        original(self, conn)
        conn.execute("UPDATE censor_label SET note='damaged'")
        raise RuntimeError('injected migration failure')

    with monkeypatch.context() as patch:
        patch.setattr(VerdictStore, '_migrate_v1', fail)
        with pytest.raises(RuntimeError, match='injected'):
            VerdictStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT note FROM censor_label').fetchone() == ('人工确认',)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='censor_probe'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='toolkit_schema_versions'").fetchone()
    VerdictStore(path)


def test_future_schema_rejects_open_and_existing_store_writes(tmp_path):
    path = tmp_path / 'history.db'
    store = VerdictStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE toolkit_schema_versions SET version=99 WHERE component='censor_verdict'")
    with pytest.raises(RuntimeError, match='censor_verdict.*99'):
        VerdictStore(path)
    with pytest.raises(RuntimeError, match='censor_verdict.*99'):
        with store._connect() as conn:
            conn.execute('DELETE FROM censor_label')


def test_existing_full_schema_preserves_every_row_and_extension(tmp_path):
    path = tmp_path / 'history.db'
    VerdictStore(path)
    tables = ('censor_probe', 'censor_label', 'censor_label_history',
              'censor_label_revocations', 'censor_label_events')
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO censor_probe(path) VALUES('automatic.mp4')")
        conn.execute("INSERT INTO censor_label VALUES('manual.mp4','censored','note','date')")
        conn.execute("INSERT INTO censor_label_history VALUES('manual.mp4','identity','censored','history','date')")
        conn.execute("INSERT INTO censor_label_revocations VALUES('old.mp4','identity','date')")
        conn.execute("INSERT INTO censor_label_events VALUES(7,'event.mp4','identity',NULL,'event','date')")
        for table in tables:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN custom TEXT')
            conn.execute(f"UPDATE {table} SET custom='extension'")
        expected = {table: conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
        conn.execute("DELETE FROM toolkit_schema_versions WHERE component='censor_verdict'")
        conn.execute("INSERT INTO toolkit_schema_versions VALUES('other',42,'date')")
    VerdictStore(path)
    with sqlite3.connect(path) as conn:
        assert {table: conn.execute(f'SELECT * FROM {table}').fetchall() for table in tables} == expected
        assert conn.execute("SELECT version FROM toolkit_schema_versions WHERE component='other'").fetchone() == (42,)


def test_backup_failure_aborts_before_any_schema_change(tmp_path, monkeypatch):
    path = tmp_path / 'history.db'
    legacy(path)

    def fail(self):
        raise OSError('backup unavailable')

    monkeypatch.setattr(VerdictStore, '_backup_before_migration', fail)
    with pytest.raises(OSError, match='backup unavailable'):
        VerdictStore(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT note FROM censor_label').fetchone() == ('人工确认',)
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='censor_probe'").fetchone()
