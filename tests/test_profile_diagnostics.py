"""Profile diagnosis must inspect the selected files without rebinding or writing."""
import hashlib
import json
import sqlite3


def test_missing_profile_diagnosis_does_not_create_directories(tmp_path):
    from utils.data_profile import profile_diagnostics, format_profile_diagnostics
    root = tmp_path / 'absent'
    report = profile_diagnostics(root)
    assert not report['database_exists']
    assert '未找到' in format_profile_diagnostics(report)
    assert not root.exists()


def test_current_and_registered_next_profile_are_explicit_and_readonly(tmp_path):
    from censor.store import VerdictStore
    from censor.models import VideoVerdict
    from utils.data_profile import profile_diagnostics, format_profile_diagnostics
    current, next_root = tmp_path / 'current', tmp_path / 'other'
    store = VerdictStore(current / 'data/censor.db')
    store.put(VideoVerdict(path='C:/synthetic/one.mp4'))
    registry = tmp_path / 'data-profile.json'
    registry.write_text(json.dumps(dict(schema_version=1, root=str(next_root), database_required=True)))
    (current / 'config.json').write_text(json.dumps(dict(jellyfin_api_key='secret-not-for-diagnostics')))
    before = hashlib.sha256(store.db_path.read_bytes()).hexdigest()
    report = profile_diagnostics(current, registry)
    text = format_profile_diagnostics(report)
    assert report['scans'] == 1 and report['registered_root'] == next_root.resolve()
    assert str(current.resolve()) in text and str(next_root.resolve()) in text
    assert '下次' in text and '重启' in text
    assert 'secret-not-for-diagnostics' not in text
    assert hashlib.sha256(store.db_path.read_bytes()).hexdigest() == before
    assert not next_root.exists()


def test_snapshot_failure_is_separate_from_scan_database_health(tmp_path):
    from censor.store import VerdictStore
    from utils.data_profile import profile_diagnostics, format_profile_diagnostics
    VerdictStore(tmp_path / 'data/censor.db')
    snapshot = tmp_path / 'data/jellyfin_review.sqlite3'
    snapshot.write_bytes(b'not-a-database')
    report = profile_diagnostics(tmp_path)
    assert report['database_exists'] and report['scans'] == 0
    assert report['snapshot_status'] == 'unavailable'
    assert '用户状态库不可读' in format_profile_diagnostics(report)
    assert snapshot.read_bytes() == b'not-a-database'


def test_legacy_snapshot_count_does_not_parse_private_payloads(tmp_path):
    from censor.store import VerdictStore
    from utils.data_profile import profile_diagnostics, format_profile_diagnostics
    VerdictStore(tmp_path / 'data/censor.db')
    with sqlite3.connect(tmp_path / 'data/jellyfin_review.sqlite3') as conn:
        conn.execute('CREATE TABLE snapshots(context TEXT PRIMARY KEY,payload TEXT)')
        conn.execute('INSERT INTO snapshots VALUES (?,?)', ('private-user', 'private-payload'))
    report = profile_diagnostics(tmp_path)
    assert report['snapshot_contexts'] == 1
    assert report['snapshot_status'] == 'legacy'
    text = format_profile_diagnostics(report)
    assert 'private-user' not in text and 'private-payload' not in text
    assert '旧格式' in text
