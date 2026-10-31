"""Generic user-state storage preserves legacy clients and context isolation."""
import json
import sqlite3

import pytest


def item(path='C:/synthetic/one.mp4', user='viewer', favorite=True):
    return dict(item_id='one', path=path, remote_path='/media/one.mp4', mapped=True,
                user_id=user, name='Synthetic', is_favorite=favorite, played=False,
                play_count=0, playback_ticks=10, has_progress=True, tags=['Keep'], genres=[])


def rows(**kwargs):
    from utils.library_scope import normalize_scope_path
    row = item(**kwargs)
    return {normalize_scope_path(row['path']): row}


def legacy(path, data, context='context'):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS snapshots(context TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (context, json.dumps(data)))


def test_read_does_not_create_missing_database(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'missing/snapshots.db'
    assert UserStateStore(db).read('context', 'viewer').status == 'missing'
    assert not db.parent.exists()


def test_old_snapshot_reads_without_migration_or_invented_time(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    legacy(db, rows())
    before = db.read_bytes()
    result = UserStateStore(db).read('context', 'viewer')
    assert result.status == 'ready' and result.items == rows()
    assert result.origin == 'legacy' and result.updated_at is None and result.revision == 0
    assert db.read_bytes() == before


def test_publish_indexes_rows_and_keeps_old_json_in_same_transaction(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    result = store.read('context', 'viewer')
    assert result.items == rows() and result.origin == 'relational'
    assert result.revision == 1 and result.updated_at
    with sqlite3.connect(db) as conn:
        assert json.loads(conn.execute('SELECT payload FROM snapshots').fetchone()[0]) == rows()
        assert conn.execute('SELECT favorite,played FROM user_state_items').fetchall() == [(1, 0)]
        assert conn.execute('PRAGMA foreign_key_list(user_state_items)').fetchall()
    store.publish('context', {})
    result = store.read('context', 'viewer')
    assert result.status == 'ready' and result.items == {} and result.revision == 2


def test_old_writer_cannot_leave_relational_results_stale(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    legacy(db, rows(favorite=False))
    result = store.read('context', 'viewer')
    assert result.items == rows(favorite=False) and result.origin == 'legacy'
    assert result.updated_at is None
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM snapshots')
    assert store.read('context', 'viewer').status == 'missing'


def test_unknown_user_source_missing_and_invalid_have_distinct_results(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    legacy(db, rows())
    store = UserStateStore(db)
    assert store.read('context', '').status == 'no_user'
    assert store.read(None, 'viewer').status == 'unresolved'
    assert store.read('another-context', 'viewer').status == 'missing'
    assert store.read('context', 'other-user').status == 'invalid'
    legacy(db, {'wrong-key': item()})
    result = store.read('context', 'viewer')
    assert result.status == 'invalid' and not result.items
    assert 'synthetic' not in result.message and 'viewer' not in result.message


def test_cancelled_publication_keeps_both_views_and_revision(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    calls = []
    def stopped():
        calls.append(1)
        return len(calls) >= 3
    with pytest.raises(InterruptedError):
        store.publish('context', rows(favorite=False), should_stop=stopped)
    result = store.read('context', 'viewer')
    assert result.items == rows() and result.revision == 1
    with sqlite3.connect(db) as conn:
        assert json.loads(conn.execute('SELECT payload FROM snapshots').fetchone()[0]) == rows()


def test_future_schema_refuses_read_and_write_without_damaging_legacy(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE user_state_schema SET version=999')
    assert store.read('context', 'viewer').status == 'unsupported'
    with pytest.raises(ValueError):
        store.publish('context', {})
    with sqlite3.connect(db) as conn:
        assert json.loads(conn.execute('SELECT payload FROM snapshots').fetchone()[0]) == rows()


def test_invalid_or_mixed_user_publication_does_not_replace_valid_snapshot(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    broken = rows()
    broken[next(iter(broken))]['play_count'] = True
    with pytest.raises(ValueError):
        store.publish('context', broken)
    mixed = rows()
    mixed.update(rows(path='C:/synthetic/two.mp4', user='other'))
    with pytest.raises(ValueError):
        store.publish('context', mixed)
    assert store.read('context', 'viewer').items == rows()


def test_binary_legacy_payload_is_reported_invalid_without_exception(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    UserStateStore(db).publish('context', rows())
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE snapshots SET payload=?', (b'not-json',))
    assert UserStateStore(db).read('context', 'viewer').status == 'invalid'


def test_partial_relational_projection_is_not_mistaken_for_empty_snapshot(tmp_path):
    from utils.user_state_store import UserStateStore
    db = tmp_path / 'state.db'
    store = UserStateStore(db)
    store.publish('context', rows())
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM user_state_items')
    result = store.read('context', 'viewer')
    assert result.status == 'invalid' and not result.items
