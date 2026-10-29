"""Authoritative versioned confirmations survive concurrent generic analyses."""
from threading import Event, Thread

import pytest

from censor.base import BaseDetector
from censor.models import (CensorState as S, Evidence, EvidenceSource as E, ProcessingStatus,
                           FrameCandidate, VideoVerdict, Verdict)
from censor.registry import detectors
from censor.review_route import bucket, ReviewBucket
from censor.service import CensorOptions, CensorService


@pytest.fixture
def review_service(tmp_path):
    class Visual(BaseDetector):
        name = 'test.v3.review.consistency'
        capabilities = ('visual',)
        entered = None
        resume = None
        fail = False
        def detect(self, ctx):
            if self.entered is not None:
                self.entered.set()
                assert self.resume.wait(10), 'synthetic synchronization timeout'
            if self.fail:
                raise RuntimeError('synthetic visual failure')
            ctx.shared.update(sampled_frames=3, analyzed_frames=3, visual_score=40.)
            ctx.duration = 120.
            ctx.shared['candidates'] = [FrameCandidate(12., 40.)]
            return [Evidence(E.VISUAL, S.CENSORED, .85, 'synthetic grid')]
    detectors.add(Visual.name, Visual, replace=True)
    path = tmp_path / 'generic.mp4'
    path.write_bytes(b'synthetic fixture only')
    service = CensorService({'censor_detectors': [Visual.name],
                            'censor_sync_nfo': False, 'censor_sync_jellyfin': False},
                           data_dir=tmp_path / 'state')
    yield service, path, Visual
    detectors.discard(Visual.name)


def run_interrupted(service, path, visual, action, *, recheck=False):
    visual.entered, visual.resume = Event(), Event()
    outputs, errors = [], []
    def run():
        try:
            outputs.append(service.probe(path, CensorOptions(force=recheck, recheck_manual=recheck)))
        except BaseException as exc:
            errors.append(exc)
    thread = Thread(target=run)
    thread.start()
    try:
        assert visual.entered.wait(10)
        action()
    finally:
        visual.resume.set()
        thread.join(15)
    assert not thread.is_alive() and not errors
    visual.entered = visual.resume = None
    return outputs[0]


def test_confirmation_during_scan_wins_in_result_history_and_cache(review_service):
    svc, path, visual = review_service
    result = run_interrupted(svc, path, visual, lambda: svc.label(path, S.UNCENSORED))
    for item in (result, svc.store.history()[0]):
        assert item.state is S.UNCENSORED
        assert item.verdict.of(E.MANUAL).state is S.UNCENSORED
        assert item.verdict.of(E.VISUAL).state is S.CENSORED
        assert item.analyzed_frames == 3
        assert item.duration == 120.
        assert [candidate.timestamp for candidate in item.candidates] == [12.]
    assert svc.store.get_label(str(path), file_identity=result.file_identity)[0] is S.UNCENSORED


def test_withdrawal_during_recheck_cannot_restore_revoked_confirmation(review_service):
    svc, path, visual = review_service
    first = svc.probe(path)
    svc.label(path, S.UNCENSORED, base=first)
    result = run_interrupted(svc, path, visual, lambda: svc.remove_label(path), recheck=True)
    for item in (result, svc.store.history()[0]):
        assert item.verdict.of(E.MANUAL) is None
        assert item.state is S.CENSORED
        assert item.verdict.of(E.VISUAL) is not None
    assert svc.store.get_label(str(path), file_identity=result.file_identity) is None


@pytest.mark.parametrize('operation', ['confirm', 'withdraw'])
def test_analysis_commit_rechecks_label_inside_write_transaction(review_service, monkeypatch, operation):
    """A non-atomic final service read still loses to a review just before put."""
    svc, path, visual = review_service
    first = svc.probe(path)
    if operation == 'withdraw':
        svc.label(path, S.UNCENSORED, base=first)
    original = svc.store.put
    injected = False
    def reviewed_before_put(result, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            if operation == 'confirm':
                svc.store.set_label(str(path), S.UNCENSORED, file_identity=result.file_identity)
            else:
                svc.store.remove_label(str(path), result.file_identity)
        return original(result, **kwargs)
    monkeypatch.setattr(svc.store, 'put', reviewed_before_put)
    result = svc.probe(path, CensorOptions(force=True, recheck_manual=True))
    expected = S.UNCENSORED if operation == 'confirm' else S.CENSORED
    assert result.state is expected
    assert svc.store.history()[0].state is expected


def test_failed_manual_recheck_preserves_confirmation_and_analysis_failure(review_service):
    svc, path, visual = review_service
    first = svc.probe(path)
    svc.label(path, S.UNCENSORED, base=first)
    visual.fail = True
    result = svc.probe(path, CensorOptions(force=True, recheck_manual=True))
    assert result.status is ProcessingStatus.FAILED
    assert result.state is S.UNCENSORED
    assert result.verdict.of(E.MANUAL).state is S.UNCENSORED
    assert bucket(result) is ReviewBucket.CONFIRMED
    assert not result.needs_review
    assert svc.store.history()[0].state is S.UNCENSORED


def test_unknown_is_not_a_confirmation_and_legacy_unknown_runs_visual(review_service):
    svc, path, visual = review_service
    with pytest.raises(ValueError, match='确认|confirm'):
        svc.label(path, S.UNKNOWN)
    # Legacy lower-level data remains readable, but must not suppress analysis.
    from censor.identity import identify_file
    svc.store.set_label(str(path), S.UNKNOWN, file_identity=identify_file(path).serialize())
    result = svc.probe(path)
    assert result.verdict.of(E.VISUAL) is not None
    assert result.verdict.of(E.MANUAL) is None
    assert result.analyzed_frames == 3
    assert bucket(result) is ReviewBucket.UNREVIEWED
    legacy = VideoVerdict(path=str(path), verdict=Verdict(
        evidence=[Evidence(E.MANUAL, S.UNKNOWN)], needs_review=True))
    assert bucket(legacy) is ReviewBucket.UNREVIEWED


@pytest.mark.parametrize('operation', ['confirm', 'withdraw'])
def test_review_and_probe_projection_roll_back_together(review_service, monkeypatch, operation):
    svc, path, visual = review_service
    first = svc.probe(path)
    if operation == 'withdraw':
        svc.label(path, S.UNCENSORED, base=first)
    def failed_write(*_):
        raise OSError('synthetic storage failure')
    monkeypatch.setattr(svc.store, '_write_result', failed_write)
    with pytest.raises(OSError, match='synthetic storage failure'):
        if operation == 'confirm':
            svc.label(path, S.UNCENSORED, base=first)
        else:
            svc.remove_label(path)
    active = svc.store.get_label(str(path), file_identity=first.file_identity)
    if operation == 'confirm':
        assert active is None
        assert svc.store.history()[0].state is S.CENSORED
        assert not svc.store.label_history(str(path))
    else:
        assert active[0] is S.UNCENSORED
        assert svc.store.history()[0].state is S.UNCENSORED


def test_file_replaced_during_recheck_cannot_keep_old_confirmation(review_service):
    svc, path, visual = review_service
    first = svc.probe(path)
    svc.label(path, S.UNCENSORED, base=first)
    result = run_interrupted(svc, path, visual,
        lambda: path.write_bytes(b'a replacement version of this synthetic file'), recheck=True)
    assert result.status is ProcessingStatus.PARTIAL
    assert result.state is S.UNKNOWN
    assert result.verdict.of(E.MANUAL) is None
    assert bucket(result) is ReviewBucket.FAILED
    assert svc.store.label_history(str(path))
