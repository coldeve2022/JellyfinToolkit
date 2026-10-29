"""Synthetic, offline regressions for metadata ownership and explicit recovery."""
import os
from pathlib import Path

# Must precede config/censor imports, including during pytest collection.
os.environ['JELLYFIN_TOOLKIT_DATA_DIR'] = str(
    Path(__file__).resolve().parents[1] / '.local/v3-metadata-audit/test-runtime')
os.environ['JELLYFIN_TOOLKIT_OFFLINE'] = '1'
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest

from tests.test_metadata_sync import setup as _metadata_setup, confirm, tags, fake_api
from censor.metadata_sync import MetadataSyncService
from censor.models import CensorState as S
from utils import jellyfin_api as api
from urllib.parse import parse_qs, urlsplit

# Register the shared pytest fixture locally without shadowing an imported name.
setup = _metadata_setup


@pytest.mark.parametrize('existing_nfo', [False, True])
def test_nfo_exit_after_write_cannot_silently_retire_withdrawal(setup, monkeypatch, existing_nfo):
    video, cfg, store, sync = setup
    cfg.censor_sync_jellyfin = False
    nfo = video.with_suffix('.nfo')
    if existing_nfo:
        nfo.write_text('<movie><tag>keep</tag></movie>', encoding='utf-8')
    confirm(setup, S.CENSORED)
    finish = sync._finish

    def interrupted(job, endpoint, status, error=''):
        if endpoint == 'nfo' and status == 'done':
            raise SystemExit('simulate process exit after atomic NFO write')
        return finish(job, endpoint, status, error)

    monkeypatch.setattr(sync, '_finish', interrupted)
    with pytest.raises(SystemExit):
        sync.run_pending()
    assert '像素化:有' in tags(nfo)
    if existing_nfo:
        from utils.file_ops import OperationJournal
        journal = OperationJournal(store.db_path.with_name('censor_metadata_operations.db'))
        backup = Path(journal.all()[0]['backup_path'])
        assert backup.read_text(encoding='utf-8') == '<movie><tag>keep</tag></movie>'
    restarted = MetadataSyncService(cfg, store)
    confirm((video, cfg, store, restarted), None)
    report = restarted.run_pending()
    # Without a durable acknowledgement, do not guess tag ownership or report
    # a successful withdrawal. Keep the receipt for explicit manual resolution.
    assert report['failed'] == 1
    assert restarted._receipt(restarted.records()[0], 'nfo') is not None
    assert '像素化:有' in tags(nfo)
    nfo.write_text('<movie><tag>keep</tag></movie>', encoding='utf-8')
    assert restarted.run_pending(retry_failed=True)['done'] == 1
    assert restarted._receipt(restarted.records()[0], 'nfo') is None


def test_failed_api_retry_cannot_claim_independently_added_tag(setup, monkeypatch):
    video, cfg, store, sync = setup
    cfg.censor_sync_nfo = False
    item, calls = fake_api(monkeypatch, setup)
    request = api._req
    failed = False

    def reject_first_post(method, *args, **kwargs):
        nonlocal failed
        if method == 'POST' and not failed:
            failed = True
            return 500, None
        return request(method, *args, **kwargs)

    monkeypatch.setattr(api, '_req', reject_first_post)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert sync.records()[0]['jellyfin'] == 'failed'
    item['Tags'].append('像素化:有')  # user writes it after the rejected POST
    sync.run_pending(retry_failed=True)
    confirm(setup, None)
    report = sync.run_pending()
    assert item['Tags'] == ['keep', '像素化:有']
    assert report['failed'] == 1
    receipt = sync._receipt(sync.records()[0], 'jellyfin')
    assert receipt['confirmed_added'] == []


def test_explicit_retry_requeues_waiting_connection_without_replaying_done_nfo(setup, monkeypatch):
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    assert sync.run_pending()['waiting'] == 1
    stamp = video.with_suffix('.nfo').stat().st_mtime_ns
    item, calls = fake_api(monkeypatch, setup)
    # An unrelated completed endpoint keeps its old tag mapping until an
    # explicit replay/backfill, even though the pending API target adopts it.
    cfg.censor_sync_tag_censored = 'new-positive'
    report = MetadataSyncService(cfg, store).run_pending(retry_failed=True)
    assert report['done'] == 2 and not report['stale']
    assert item['Tags'] == ['keep', 'new-positive']
    assert tags(video.with_suffix('.nfo')) == ['像素化:有']
    assert video.with_suffix('.nfo').stat().st_mtime_ns == stamp


def test_connection_retry_does_not_requeue_replaced_file(setup, monkeypatch):
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    sync.run_pending()
    item, calls = fake_api(monkeypatch, setup)
    video.write_bytes(b'replaced synthetic video')
    report = sync.run_pending(retry_failed=True)
    assert report['stale'] == 1
    assert not [call for call in calls if call[0] == 'POST']


@pytest.mark.parametrize('kind', ['Episode', 'Trailer', 'MusicVideo'])
def test_metadata_sync_indexes_every_supported_video_kind(setup, monkeypatch, kind):
    video, cfg, store, sync = setup
    cfg.censor_sync_nfo = False
    cfg.jellyfin_server, cfg.jellyfin_api_key = 'http://synthetic.invalid', 'fixture-key'
    item = {'Id': 'video', 'Path': str(video), 'Name': 'Synthetic', 'Type': kind, 'Tags': ['keep']}
    requested = []

    def request(method, url, key, body=None, **kwargs):
        parsed = urlsplit(url)
        if parsed.path == '/Users':
            return 200, [{'Id': 'viewer'}]
        if parsed.path == '/Items':
            types = set(parse_qs(parsed.query)['includeItemTypes'][0].split(','))
            requested.append(types)
            rows = [dict(item)] if kind in types else []
            return 200, {'Items': rows, 'TotalRecordCount': len(rows)}
        if method == 'GET':
            return 200, dict(item, Tags=list(item['Tags']))
        item.update(body)
        return 204, None

    monkeypatch.setattr(api, '_req', request)
    confirm(setup, S.CENSORED)
    assert sync.run_pending()['done'] == 1
    assert item['Tags'] == ['keep', '像素化:有']
    assert requested == [{'Movie', 'Video', 'Episode', 'Trailer', 'MusicVideo'}]


def test_legacy_api_iterator_keeps_movie_video_default(monkeypatch):
    requested = []

    def request(method, url, key, **kwargs):
        requested.append(parse_qs(urlsplit(url).query)['includeItemTypes'][0])
        return 200, {'Items': [], 'TotalRecordCount': 0}

    monkeypatch.setattr(api, '_req', request)
    assert list(api._iter_items('http://synthetic.invalid', 'fixture-key')) == [[]]
    assert requested == ['Movie,Video']


@pytest.mark.parametrize('completed_endpoint', ['nfo', 'jellyfin'])
def test_recovery_preserves_endpoint_completed_by_another_connection(setup, monkeypatch, completed_endpoint):
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    old = sync.records()[0]
    other = MetadataSyncService(cfg, store)
    cfg.censor_sync_tag_censored = 'new-positive'
    check = sync._check

    def complete_after_recovery_read(job, should_stop):
        check(job, should_stop)
        # _finish uses another SQLite connection, as another process would,
        # and intentionally does not advance the manual-confirmation revision.
        other._finish(job, completed_endpoint, 'done')

    monkeypatch.setattr(sync, '_check', complete_after_recovery_read)
    sync._requeue_changed_unfinished(None)
    current = sync.records()[0]
    assert current[completed_endpoint] == 'done'
    assert current[completed_endpoint + '_fingerprint'] == old[completed_endpoint + '_fingerprint']
    unfinished = 'jellyfin' if completed_endpoint == 'nfo' else 'nfo'
    assert current[unfinished] == 'pending'
    assert current[unfinished + '_fingerprint'] == sync._fingerprint(unfinished)
