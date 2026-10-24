"""Generic history correctness, bounded I/O, and confirmation provenance."""
from contextlib import contextmanager

import pytest

from censor import CensorService, CensorOptions
from censor.models import (CensorState, Evidence, EvidenceSource, VideoVerdict,
                           Verdict, FrameCandidate, ProcessingStatus)
from censor.store import VerdictStore


def scanned(tmp_path):
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'original synthetic file')
    svc = CensorService({'censor_visual_enabled': False}, data_dir=tmp_path / 'data')
    opts = CensorOptions(detect_visual=False)
    return svc, path, opts, svc.probe(path, opts)


@pytest.mark.parametrize('column,value', [('state', 'broken'), ('status', 'broken'),
                                         ('sampled_frames', 'bad')])
def test_corrupt_row_is_a_cache_miss_not_a_permanent_scan_failure(tmp_path, column, value):
    svc, path, opts, _ = scanned(tmp_path)
    with svc.store._connect() as conn:
        conn.execute(f'UPDATE censor_probe SET {column}=? WHERE path=?', (value, str(path)))
    assert svc.store.get(str(path)) is None
    result = svc.probe(path, opts)
    assert result.status is ProcessingStatus.COMPLETED
    assert not result.error


def test_review_queue_reads_in_batches_not_one_connection_per_video(tmp_path, monkeypatch):
    store = VerdictStore(tmp_path / 'history.db')
    for index in range(40):
        store.put(VideoVerdict(path=f'generic-{index}',
                               verdict=Verdict(needs_review=True)))
    connections = []
    connect = store._connect
    @contextmanager
    def counted():
        connections.append(1)
        with connect() as conn:
            yield conn
    monkeypatch.setattr(store, '_connect', counted)
    assert len(store.review_queue(25)) == 25
    assert len(connections) <= 3


def test_corrupt_history_does_not_prevent_loading_other_reviews(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    for key in ('good', 'bad'):
        store.put(VideoVerdict(path=key, verdict=Verdict(needs_review=True)))
    with store._connect() as conn:
        conn.execute("UPDATE censor_probe SET state='broken' WHERE path='bad'")
    assert [result.path for result in store.review_queue()] == ['good']


def test_label_prefers_the_current_explicit_analysis_to_older_saved_analysis(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    current = svc.probe(path, CensorOptions(detect_visual=False, force=True, use_cache=False))
    current.candidates = [FrameCandidate(123.5, 12)]
    labeled = svc.label(path, CensorState.CENSORED, base=current)
    assert [c.timestamp for c in labeled.candidates] == [123.5]


def test_withdrawing_on_replaced_file_does_not_return_old_evidence(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    first.candidates = [FrameCandidate(25., 50)]
    svc.label(path, CensorState.CENSORED, base=first)
    path.write_bytes(b'replaced synthetic file with another identity')
    result = svc.remove_label(path)
    assert result is None or (result.status is not ProcessingStatus.COMPLETED
                              and not result.candidates)
    assert svc.store.label_history(str(path))


def test_timeout_change_does_not_invalidate_completed_analysis(tmp_path):
    svc, path, opts, _ = scanned(tmp_path)
    svc.cfg['censor_sampling_timeout'] = 1200.
    assert svc.cached_result(path, opts) is not None


def test_unreadable_label_state_cannot_block_rescanning(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    svc.label(path, CensorState.CENSORED, base=first)
    with svc.store._connect() as conn:
        conn.execute("UPDATE censor_label_history SET state='broken'")
    assert svc.probe(path, CensorOptions(force=True, detect_visual=False)).status is ProcessingStatus.COMPLETED


def test_new_confirmation_reuses_completed_analysis_without_losing_evidence(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    first.candidates = [FrameCandidate(35., 12)]
    svc.store.put(first)
    svc.label(path, CensorState.CENSORED, base=first)
    assert svc.cached_result(path, opts) is not None
    result = svc.probe(path, opts)
    assert result.cache_hit
    assert [c.timestamp for c in result.candidates] == [35.]


@pytest.mark.parametrize('raw', [
    '[{"timestamp": NaN, "score": 2}]',
    '[{"timestamp": 1, "score": Infinity}]',
    '[{"timestamp": 1, "score": 2, "box": [1,2]}]',
    '[{"timestamp": -1, "score": 2}]',
])
def test_malformed_candidate_cannot_break_review_rendering(tmp_path, raw):
    store = VerdictStore(tmp_path / 'history.db')
    store.put(VideoVerdict(path='generic'))
    with store._connect() as conn:
        conn.execute('UPDATE censor_probe SET candidates=?', (raw,))
    assert store.get('generic').candidates == []


@pytest.mark.parametrize('status', [ProcessingStatus.FAILED, ProcessingStatus.PARTIAL,
                                    ProcessingStatus.CANCELLED])
def test_confirming_incomplete_recheck_retains_successful_automatic_evidence(tmp_path, status):
    svc, path, opts, first = scanned(tmp_path)
    first.candidates = [FrameCandidate(35., 12)]
    svc.store.put(first)
    failed = VideoVerdict(path=str(path), file_identity=first.file_identity,
                          analysis_signature=first.analysis_signature, status=status,
                          error='synthetic recheck failure')
    result = svc.label(path, CensorState.CENSORED, base=failed)
    assert [c.timestamp for c in result.candidates] == [35.]
    assert not result.error
    assert svc.cached_result(path, opts) is not None


def test_confirming_failure_without_success_does_not_cache_failed_analysis(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    svc.store.clear_probes()
    first.status = ProcessingStatus.FAILED
    first.error = 'synthetic failure'
    first.candidates = [FrameCandidate(35., 12)]
    result = svc.label(path, CensorState.CENSORED, base=first)
    assert not result.analysis_signature
    assert not result.candidates and not result.error
    assert svc.cached_result(path, opts) is None
    assert svc.store.get_label(str(path), file_identity=first.file_identity)


def test_history_cursor_advances_over_a_page_of_corrupt_rows(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    for index in range(7):
        store.put(VideoVerdict(path=f'{index:03}'))
    with store._connect() as conn:
        conn.execute("UPDATE censor_probe SET updated_at='2026-10-23'")
        conn.execute("UPDATE censor_probe SET state='broken' WHERE path < '005'")
    first = store.history_page(5, 0)
    assert first['records'] == []
    assert first['next_offset'] == 5 and first['has_more']
    second = store.history_page(5, first['next_offset'])
    assert [r.path for r in second['records']] == ['005', '006']
    assert second['next_offset'] == 7 and not second['has_more']


def test_legacy_completed_row_with_error_is_not_a_reusable_success(tmp_path):
    svc, path, opts, first = scanned(tmp_path)
    first.error = 'older incorrectly promoted failure'
    svc.store.put(first)
    assert svc.cached_result(path, opts) is None


def test_scan_job_connection_is_closed_on_exit_and_rollback(tmp_path):
    import sqlite3
    from censor.jobs import ScanJobs
    jobs = ScanJobs(tmp_path / 'queue.db')
    with jobs.connect() as connection:
        connection.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    with pytest.raises(RuntimeError):
        with jobs.connect() as failed:
            failed.execute("INSERT INTO pixelation_sources VALUES ('synthetic','pending')")
            raise RuntimeError('rollback')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        failed.execute('SELECT 1')
    assert jobs.unfinished_sources() == []
