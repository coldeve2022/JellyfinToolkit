"""Ordinary history projects authoritative review state without rewriting old rows."""
from contextlib import contextmanager

import pytest

from censor.fusion import fuse
from censor.models import (CensorState as S, Evidence, EvidenceSource as E,
                           FrameCandidate, VideoVerdict)
from censor.review_route import bucket, ReviewBucket
from censor.store import VerdictStore


def old_probe(path='generic.mp4', identity='v1', *, manual=False, score=40.):
    evidence = [Evidence(E.VISUAL, S.CENSORED, .85, 'old synthetic analysis')]
    if manual:
        evidence.append(Evidence(E.MANUAL, S.UNCENSORED, 1., 'old review'))
    return VideoVerdict(path=path, file_identity=identity, analysis_signature='old-signature',
                        verdict=fuse(evidence), visual_score=score, duration=120.,
                        sampled_frames=60, analyzed_frames=53,
                        detector_runs={'visual.fixture': {'analyzed_frames': 53}},
                        candidates=[FrameCandidate(12., 40., thumb_path='old-thumb.png')])


def history_records(store, mode):
    if mode == 'page':
        return store.history_page()['records']
    return [item for page in store.iter_history_pages(2) for item in page['records']]


@pytest.mark.parametrize('mode', ['page', 'stream'])
def test_history_restores_confirmation_missing_from_old_probe(tmp_path, mode):
    store = VerdictStore(tmp_path / 'history.db')
    raw = old_probe()
    store.put(raw)
    store.set_label(raw.path, S.UNCENSORED, 'current review', file_identity='v1')
    result = history_records(store, mode)[0]
    assert result.state is S.UNCENSORED
    assert bucket(result) is ReviewBucket.CONFIRMED
    assert result.verdict.of(E.MANUAL).detail == 'current review'
    assert result.verdict.of(E.VISUAL).detail == 'old synthetic analysis'
    assert result.candidates == raw.candidates
    assert result.duration == raw.duration
    assert (result.sampled_frames, result.analyzed_frames) == (60, 53)
    assert result.detector_runs == raw.detector_runs
    assert result.analysis_signature == raw.analysis_signature
    assert store.review_queue() == []


@pytest.mark.parametrize('mode', ['page', 'stream'])
def test_history_removes_revoked_manual_left_in_old_probe(tmp_path, mode):
    store = VerdictStore(tmp_path / 'history.db')
    raw = old_probe(manual=True)
    store.put(raw)
    store.set_label(raw.path, S.UNCENSORED, file_identity='v1')
    store.remove_label(raw.path, 'v1')
    result = history_records(store, mode)[0]
    assert result.verdict.of(E.MANUAL) is None
    assert result.state is S.CENSORED
    assert bucket(result) is ReviewBucket.UNREVIEWED
    assert result.candidates == raw.candidates


def test_review_limit_applies_after_projection_including_old_hidden_withdrawals(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    confirmed = old_probe('confirmed.mp4', score=100.)
    revoked = old_probe('revoked.mp4', manual=True, score=90.)
    automatic = old_probe('automatic.mp4', score=80.)
    for raw in (confirmed, revoked, automatic):
        store.put(raw)
    store.set_label(confirmed.path, S.UNCENSORED, file_identity='v1')
    store.set_label(revoked.path, S.UNCENSORED, file_identity='v1')
    store.remove_label(revoked.path, 'v1')
    queue = store.review_queue(1)
    assert [result.path for result in queue] == [revoked.path]
    assert queue[0].state is S.CENSORED
    assert queue[0].verdict.of(E.MANUAL) is None


@pytest.mark.parametrize('probe_identity,label_identity,state,confirmed', [
    ('v2', 'v1', S.UNCENSORED, False),
    ('v2', '', S.UNCENSORED, False),
    ('', 'v1', S.UNCENSORED, False),
    ('', '', S.UNCENSORED, True),
    ('v1', 'v1', S.UNKNOWN, False),
])
def test_history_requires_exact_version_and_concrete_authoritative_label(
        tmp_path, probe_identity, label_identity, state, confirmed):
    store = VerdictStore(tmp_path / 'history.db')
    raw = old_probe(identity=probe_identity, manual=True)
    store.put(raw)
    store.set_label(raw.path, state, file_identity=label_identity)
    for result in (store.history()[0], next(store.iter_history_pages())['records'][0]):
        assert (bucket(result) is ReviewBucket.CONFIRMED) == confirmed
        assert (result.verdict.of(E.MANUAL) is not None) == confirmed
        assert result.state is (S.UNCENSORED if confirmed else S.CENSORED)


def test_bare_legacy_manual_is_not_authoritative(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    store.put(old_probe(identity='', manual=True))
    result = store.history()[0]
    assert result.verdict.of(E.MANUAL) is None
    assert result.state is S.CENSORED
    assert [item.path for item in store.review_queue()] == [result.path]


def test_history_stream_uses_one_snapshot_for_probe_and_labels(tmp_path):
    store = VerdictStore(tmp_path / 'history.db')
    for path in ('a.mp4', 'b.mp4'):
        store.put(old_probe(path))
        store.set_label(path, S.UNCENSORED, file_identity='v1')
    with store._connect() as conn:
        conn.execute("UPDATE censor_probe SET updated_at='same'")
    pages = store.iter_history_pages(1)
    first = next(pages)['records'][0]
    store.remove_label('b.mp4', 'v1')
    remaining = [item for page in pages for item in page['records']]
    assert first.path == 'a.mp4' and remaining[0].path == 'b.mp4'
    assert all(item.state is S.UNCENSORED for item in [first, *remaining])
    assert store.history()[1].state is S.CENSORED


@pytest.mark.parametrize('mode', ['page', 'stream', 'queue'])
def test_projection_is_batched_and_read_only(tmp_path, monkeypatch, mode):
    store = VerdictStore(tmp_path / 'history.db')
    for index in range(20):
        raw = old_probe(f'{index:02}.mp4')
        store.put(raw)
        store.set_label(raw.path, S.UNCENSORED, file_identity='v1')
    with store._connect() as conn:
        before = list(conn.iterdump())
    connect = store._connect
    connections, statements = [], []
    @contextmanager
    def counted():
        connections.append(1)
        with connect() as conn:
            conn.set_trace_callback(statements.append)
            yield conn
    monkeypatch.setattr(store, '_connect', counted)
    if mode == 'queue':
        assert store.review_queue(5) == []
    else:
        assert all(item.state is S.UNCENSORED for item in history_records(store, mode))
    assert len(connections) == 1
    selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
    assert len(selects) <= 3
    assert not any(sql.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE', 'REPLACE'))
                   for sql in statements)
    with connect() as conn:
        assert list(conn.iterdump()) == before
