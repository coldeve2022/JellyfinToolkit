from pathlib import Path
from types import SimpleNamespace
import base64
import json
import pytest


def service(tmp_path, server=None):
    from utils.media_upgrade import MediaUpgradeService
    return MediaUpgradeService(SimpleNamespace(jellyfin_server='', jellyfin_api_key=''), tmp_path / 'private', server=server)


def pair(tmp_path):
    old = tmp_path / 'library' / 'ABC-123 original.mp4'
    new = tmp_path / 'downloads' / 'ABC-123 better.mp4'
    old.parent.mkdir(); new.parent.mkdir()
    old.write_bytes(b'old video'); new.write_bytes(b'new video')
    return old, new


def test_preview_identifier_pair_and_snapshot_never_write_media(tmp_path):
    old, new = pair(tmp_path)
    nfo = old.with_suffix('.nfo'); nfo.write_bytes(b'\xff\xfe<movie>private</movie>')
    svc = service(tmp_path)
    plan = svc.preview([str(new)], [str(old)])
    assert plan['rows'][0]['status'] == 'ready'
    prepared = svc.prepare(plan)
    saved = json.loads(Path(prepared['snapshot_path']).read_text(encoding='utf-8'))
    assert base64.b64decode(saved['rows'][0]['sidecars'][0]['bytes_b64']) == nfo.read_bytes()
    assert saved['rows'][0]['source_identity']['sha256']
    assert old.read_bytes() == b'old video' and new.exists()
    assert 'api_key' not in Path(prepared['snapshot_path']).read_text(encoding='utf-8')


def test_ambiguity_needs_choice_and_container_change_needs_convert(tmp_path):
    old, new = pair(tmp_path)
    another = old.with_name('ABC-123 alternate.mp4'); another.write_bytes(b'other')
    svc = service(tmp_path)
    assert svc.preview([new], [old, another])['rows'][0]['status'] == 'ambiguous'
    mkv = new.with_suffix('.mkv'); new.rename(mkv)
    assert svc.preview([mkv], [old])['rows'][0]['status'] == 'container_convert'


def test_execute_checks_race_before_replacing_and_preserves_sidecars(tmp_path):
    old, new = pair(tmp_path)
    nfo = old.with_suffix('.nfo'); nfo.write_bytes(b'<movie/>')
    svc = service(tmp_path)
    prepared = svc.prepare(svc.preview([new], [old]))
    new.write_bytes(b'changed')
    result = svc.execute(prepared, validator=lambda stage: (True, ''))
    assert result['fail'] == 1
    assert old.read_bytes() == b'old video'
    new.write_bytes(b'new video')
    prepared = svc.prepare(svc.preview([new], [old]))
    result = svc.execute(prepared, validator=lambda stage: (True, ''))
    assert result['ok'] == 1
    assert old.read_bytes() == b'new video' and nfo.read_bytes() == b'<movie/>'
    op = svc.journal.get(result['operations'][0]['operation_id'])
    assert Path(op['backup_path']).read_bytes() == b'old video'
    assert svc.journal.restore(op['id'])['ok']
    assert old.read_bytes() == b'old video'


class Server:
    def __init__(self, path):
        self.path = str(path); self.userdata = {'u1': {'Played': True, 'PlaybackPositionTicks': 456, 'IsFavorite': True}, 'u2': {'Played': False, 'PlaybackPositionTicks': 0}}
        self.item_id = 'item1'; self.name = 'Original title'; self.refreshes = []
    def capture(self, path, cancel=None):
        assert str(path) == self.path
        return {'item_id': self.item_id, 'path': self.path, 'metadata': {'Name': self.name}, 'users': self.userdata, 'inaccessible_users': []}
    def refresh(self, item_id):
        self.refreshes.append(item_id)
        return {'queued': True}


def test_userdata_snapshotted_for_each_user_and_refresh_needs_explicit_verification(tmp_path):
    old, new = pair(tmp_path); server = Server(old); svc = service(tmp_path, server)
    prepared = svc.prepare(svc.preview([new], [old]))
    saved = json.loads(Path(prepared['snapshot_path']).read_text(encoding='utf-8'))
    assert set(saved['rows'][0]['server']['users']) == {'u1', 'u2'}
    result = svc.execute(prepared, validator=lambda stage: (True, ''))
    assert result['needs_attention'] == 1 and server.refreshes == ['item1']
    assert svc.verify(prepared)['rows'][0]['status'] == 'verified'
    server.userdata['u2']['PlaybackPositionTicks'] = 23
    assert svc.verify(prepared)['rows'][0]['status'] == 'needs_attention'
    server.item_id = 'different-item'
    assert 'ItemId' in svc.verify(prepared)['rows'][0]['error']


def test_sidecar_edit_or_prepared_tampering_refuses_execution(tmp_path):
    old, new = pair(tmp_path); nfo = old.with_suffix('.nfo'); nfo.write_bytes(b'original')
    svc = service(tmp_path); prepared = svc.prepare(svc.preview([new], [old]))
    nfo.write_bytes(b'edited by another app')
    assert svc.execute(prepared, validator=lambda stage: (True, ''))['fail'] == 1
    assert old.read_bytes() == b'old video'
    prepared = svc.prepare(svc.preview([new], [old]))
    prepared['rows'][0]['target'] = str(new)
    assert svc.execute(prepared, validator=lambda stage: (True, ''))['fail'] == 1


def test_snapshot_server_failure_prevents_file_changes(tmp_path):
    old, new = pair(tmp_path)
    class Failing(Server):
        def capture(self, path, cancel=None):
            raise ValueError('ambiguous server paths')
    svc = service(tmp_path, Failing(old))
    prepared = svc.prepare(svc.preview([new], [old]))
    assert prepared['rows'][0]['status'] == 'blocked'
    assert svc.execute(prepared, validator=lambda stage: (True, ''))['fail'] == 1
    assert old.read_bytes() == b'old video'


def test_rejects_same_bytes_new_file_identity_and_new_ambiguous_original(tmp_path):
    old, new = pair(tmp_path); svc = service(tmp_path)
    prepared = svc.prepare(svc.preview([new], [old.parent]))
    alternate = old.with_name('ABC-123 other.mp4'); alternate.write_bytes(b'copy')
    assert svc.execute(prepared, validator=lambda stage: True)['fail'] == 1
    alternate.unlink()
    prepared = svc.prepare(svc.preview([new], [old]))
    replacement = new.with_suffix('.temp'); replacement.write_bytes(new.read_bytes()); replacement.replace(new)
    assert svc.execute(prepared, validator=lambda stage: True)['fail'] == 1
    assert old.read_bytes() == b'old video'


def test_ffprobe_validation_failure_keeps_original_and_candidate(tmp_path, monkeypatch):
    old, new = pair(tmp_path); svc = service(tmp_path)
    monkeypatch.setattr('utils.media_upgrade.validate_restored_output', lambda a, b: (False, 'invalid duration'))
    result = svc.execute(svc.prepare(svc.preview([new], [old])))
    assert result['fail'] == 1 and old.read_bytes() == b'old video' and new.exists()


def test_default_rest_adapter_snapshots_each_user_without_admin_substitution(tmp_path, monkeypatch):
    from utils.media_upgrade import JellyfinInheritanceAdapter
    old, new = pair(tmp_path)
    requests = []
    def request(method, url, key, body=None):
        requests.append((method, url, body))
        if '/Items?' in url:
            return 200, {'Items': [{'Id': 'exact', 'Path': str(old)}, {'Id': 'same-code-wrong-path', 'Path': str(new)}], 'TotalRecordCount': 2}
        if url.endswith('/Users'):
            return 200, [{'Id': 'admin', 'Policy': {'IsAdministrator': True}}, {'Id': 'other'}, {'Id': 'denied'}]
        if '/Users/denied/' in url:
            return 403, None
        if '/Users/' in url:
            uid = 'admin' if '/admin/' in url else 'other'
            return 200, {'Id': 'exact', 'Path': str(old), 'Name': 'Title', 'UserData': {'Played': uid == 'admin', 'PlaybackPositionTicks': 10 if uid == 'admin' else 30}, 'MediaStreams': ['not metadata']}
        if '/Refresh?' in url:
            return 204, None
        raise AssertionError(url)
    monkeypatch.setattr('utils.jellyfin_api._req', request)
    adapter = JellyfinInheritanceAdapter(SimpleNamespace(jellyfin_server='http://fixture', jellyfin_api_key='secret'))
    snapshot = adapter.capture(old)
    assert snapshot['users']['admin']['PlaybackPositionTicks'] == 10
    assert snapshot['users']['other']['PlaybackPositionTicks'] == 30
    assert snapshot['inaccessible_users'] == ['denied']
    assert 'MediaStreams' not in snapshot['metadata'] and 'secret' not in json.dumps(snapshot)
    assert adapter.refresh('exact') == {'queued': True}
    assert 'replaceAllMetadata=false' in requests[-1][1]


def test_hardlinked_candidate_refused(tmp_path):
    import os
    old, new = pair(tmp_path); new.unlink(); os.link(old, new)
    assert service(tmp_path).preview([new], [old])['rows'][0]['status'] == 'conflict'


def test_rest_transport_error_is_sanitized_and_snapshot_stays_reviewable(tmp_path, monkeypatch):
    from utils.jellyfin_api import JellyfinError
    from utils.media_upgrade import MediaUpgradeService
    old, new = pair(tmp_path)
    def fail(*args, **kwargs):
        raise JellyfinError('request failed, accidental secret-credential')
    monkeypatch.setattr('utils.jellyfin_api._req', fail)
    svc = MediaUpgradeService(SimpleNamespace(jellyfin_server='http://fixture', jellyfin_api_key='secret-credential'), tmp_path / 'private')
    prepared = svc.prepare(svc.preview([new], [old]))
    assert prepared['rows'][0]['status'] == 'blocked'
    assert 'secret-credential' not in Path(prepared['snapshot_path']).read_text(encoding='utf-8')


def test_identity_rechecked_after_staged_validation(tmp_path):
    old, new = pair(tmp_path); svc = service(tmp_path)
    prepared = svc.prepare(svc.preview([new], [old]))
    def race(stage):
        replacement = new.with_suffix('.temp'); replacement.write_bytes(new.read_bytes()); replacement.replace(new)
        return True, ''
    assert svc.execute(prepared, validator=race)['fail'] == 1
    assert old.read_bytes() == b'old video'


def test_server_userdata_changes_after_prepare_refuse_replacement(tmp_path):
    old, new = pair(tmp_path); server = Server(old); svc = service(tmp_path, server)
    prepared = svc.prepare(svc.preview([new], [old]))
    server.userdata['u1']['PlaybackPositionTicks'] += 9
    result = svc.execute(prepared, validator=lambda stage: True)
    assert result['fail'] == 1 and old.read_bytes() == b'old video'
    assert server.refreshes == []


def test_server_metadata_changes_after_prepare_refuse_replacement(tmp_path):
    old, new = pair(tmp_path); server = Server(old); svc = service(tmp_path, server)
    prepared = svc.prepare(svc.preview([new], [old])); server.name = 'edited elsewhere'
    assert svc.execute(prepared, validator=lambda stage: True)['fail'] == 1
    assert old.read_bytes() == b'old video'


def test_execute_cancellation_before_or_during_staging_keeps_both_files(tmp_path):
    old, new = pair(tmp_path); svc = service(tmp_path)
    prepared = svc.prepare(svc.preview([new], [old]))
    result = svc.execute(prepared, cancel=lambda: True, validator=lambda stage: True)
    assert result['cancelled'] and result['fail'] == 0
    stopped = [False]
    def staged(stage):
        stopped[0] = True
        return True
    result = svc.execute(prepared, cancel=lambda: stopped[0], validator=staged)
    assert result['cancelled'] and result['fail'] == 0
    assert old.read_bytes() == b'old video' and new.read_bytes() == b'new video'


def test_journal_intent_links_private_inheritance_snapshot(tmp_path):
    old, new = pair(tmp_path); svc = service(tmp_path)
    prepared = svc.prepare(svc.preview([new], [old]))
    result = svc.execute(prepared, validator=lambda stage: True)
    op = svc.journal.get(result['operations'][0]['operation_id'])
    assert op['inheritance_snapshot_id'] == prepared['snapshot_id']


def test_configured_server_missing_key_degrades_to_local_only(tmp_path):
    """配了服务器但没填 API Key：不当错误拦下来，只降级为"换本地文件"。

    以前这种情况整条链路被堵死 —— 用户明明只是想换掉有码文件，却因为
    一个可选的继承功能填不全而完全动不了。现在照常替换，但必须写清楚
    "本次不继承用户状态"，不能让人误以为进度也搬过去了。
    """
    from utils.media_upgrade import MediaUpgradeService
    old, new = pair(tmp_path)
    svc = MediaUpgradeService(SimpleNamespace(jellyfin_server='http://fixture', jellyfin_api_key=''), tmp_path / 'private')
    prepared = svc.prepare(svc.preview([new], [old]))
    row = prepared['rows'][0]
    assert row['status'] == 'ready'
    assert row['server'] is None
    assert 'API Key' in row.get('inheritance_note', '')
    assert svc.execute(prepared, validator=lambda stage: True)['ok'] == 1
    assert old.read_bytes() != b'old video', "应当真的换掉了文件"


def test_server_remote_path_is_mapped_exactly_and_preserved(tmp_path, monkeypatch):
    from utils.media_upgrade import JellyfinInheritanceAdapter
    old, new = pair(tmp_path); remote = '/media/ABC-123 original.mp4'
    def request(method, url, key, body=None):
        if '/Items?' in url:
            return 200, {'Items': [{'Id': 'exact', 'Path': remote}], 'TotalRecordCount': 1}
        if url.endswith('/Users'):
            return 200, [{'Id': 'u1'}]
        return 200, {'Id': 'exact', 'Path': remote, 'Name': 'title', 'UserData': {'Played': True}}
    monkeypatch.setattr('utils.jellyfin_api._req', request)
    cfg = SimpleNamespace(jellyfin_server='http://fixture', jellyfin_api_key='key', library_path_mappings=[{'remote': '/media', 'local': str(old.parent)}])
    captured = JellyfinInheritanceAdapter(cfg).capture(old)
    assert captured['path'] == remote
    cfg.library_path_mappings.append({'remote': '/media', 'local': str(new.parent)})
    with pytest.raises(ValueError):
        JellyfinInheritanceAdapter(cfg).capture(old)


def test_staged_bytes_changed_by_validator_cannot_be_installed(tmp_path):
    old, new = pair(tmp_path); svc = service(tmp_path)
    def corrupt(stage):
        stage.write_bytes(b'corrupted stage')
        return True
    assert svc.execute(svc.prepare(svc.preview([new], [old])), validator=corrupt)['fail'] == 1
    assert old.read_bytes() == b'old video' and new.exists()


def test_adapter_keeps_documented_userdata_identity_keys(tmp_path, monkeypatch):
    from utils.media_upgrade import JellyfinInheritanceAdapter
    old, _ = pair(tmp_path)
    def request(method, url, key, body=None):
        if '/Items?' in url:
            return 200, {'Items': [{'Id': 'i1', 'Path': str(old)}], 'TotalRecordCount': 1}
        if url.endswith('/Users'):
            return 200, [{'Id': 'u1'}]
        return 200, {'Id': 'i1', 'Path': str(old), 'Name': 'name', 'UserData': {'Played': True, 'Key': 'provider-state-key', 'ItemId': 'i1'}}
    monkeypatch.setattr('utils.jellyfin_api._req', request)
    adapter = JellyfinInheritanceAdapter(SimpleNamespace(jellyfin_server='http://fixture', jellyfin_api_key='secret'))
    state = adapter.capture(old)['users']['u1']
    assert state['Key'] == 'provider-state-key' and state['ItemId'] == 'i1'


def test_server_userdata_race_during_staged_validation_refuses_commit(tmp_path):
    old, new = pair(tmp_path); server = Server(old); svc = service(tmp_path, server)
    prepared = svc.prepare(svc.preview([new], [old]))
    def concurrent_playback(stage):
        server.userdata['u1']['PlaybackPositionTicks'] += 1
        return True
    result = svc.execute(prepared, validator=concurrent_playback)
    assert result['fail'] == 1
    assert old.read_bytes() == b'old video' and new.read_bytes() == b'new video'
    assert server.refreshes == []


@pytest.mark.parametrize('change', ['origin', 'credential', 'mapping'])
def test_snapshot_is_bound_to_original_service_configuration(tmp_path, change):
    from utils.media_upgrade import MediaUpgradeService
    old, new = pair(tmp_path); server = Server(old)
    cfg = SimpleNamespace(jellyfin_server='http://server-a:8096', jellyfin_api_key='private-api-key', library_path_mappings=[])
    svc = MediaUpgradeService(cfg, tmp_path / 'private', server=server)
    prepared = svc.prepare(svc.preview([new], [old]))
    raw = Path(prepared['snapshot_path']).read_text(encoding='utf-8')
    assert 'private-api-key' not in raw
    other_cfg = SimpleNamespace(**vars(cfg))
    if change == 'origin':
        other_cfg.jellyfin_server = 'http://server-b:8096'
    elif change == 'credential':
        other_cfg.jellyfin_api_key = 'different-key'
    else:
        other_cfg.library_path_mappings = [{'remote': '/media', 'local': str(old.parent)}]
    other = MediaUpgradeService(other_cfg, tmp_path / 'private', server=server)
    assert other.execute(prepared, validator=lambda stage: True)['fail'] == 1
    with pytest.raises(ValueError):
        other.verify(prepared)
    assert old.read_bytes() == b'old video' and server.refreshes == []


def test_inaccessible_users_can_never_be_reported_as_fully_verified(tmp_path):
    old, new = pair(tmp_path)
    class Partial(Server):
        def capture(self, path, cancel=None):
            value = super().capture(path, cancel)
            value['inaccessible_users'] = ['denied-user']
            return value
    svc = service(tmp_path, Partial(old))
    prepared = svc.prepare(svc.preview([new], [old]))
    assert svc.execute(prepared, validator=lambda stage: True)['needs_attention'] == 1
    result = svc.verify(prepared)
    assert result['rows'][0]['status'] == 'needs_attention'
    assert '用户' in result['rows'][0]['error']


def test_server_state_race_while_original_backup_is_copied_refuses_commit(tmp_path, monkeypatch):
    import shutil
    old, new = pair(tmp_path); server = Server(old); svc = service(tmp_path, server)
    prepared = svc.prepare(svc.preview([new], [old]))
    copy = shutil.copy2
    def race(source, destination, *args, **kwargs):
        outcome = copy(source, destination, *args, **kwargs)
        if 'media-backups' in str(destination):
            server.userdata['u2']['PlaybackPositionTicks'] = 400
        return outcome
    monkeypatch.setattr('utils.file_ops.shutil.copy2', race)
    assert svc.execute(prepared, validator=lambda stage: True)['fail'] == 1
    assert old.read_bytes() == b'old video' and new.exists()


def test_default_upgrade_journal_is_task_center_journal_and_can_restore(tmp_path):
    import config
    from utils.file_ops import OperationJournal, path_key
    from utils.media_upgrade import MediaUpgradeService
    old, new = pair(tmp_path)
    svc = MediaUpgradeService(SimpleNamespace(jellyfin_server='', jellyfin_api_key=''))
    task_center_journal = OperationJournal()
    assert path_key(svc.journal.path) == path_key(task_center_journal.path)
    assert path_key(svc.journal.path) == path_key(config.DATA_DIR / 'media_operations.db')
    result = svc.execute(svc.prepare(svc.preview([new], [old])), validator=lambda stage: True)
    assert result['ok'] == 1
    operation_id = result['operations'][0]['operation_id']
    entry = next(row for row in task_center_journal.all() if row['id'] == operation_id)
    assert entry['inheritance_snapshot_id']
    assert task_center_journal.restore(operation_id)['ok']
    assert old.read_bytes() == b'old video' and new.read_bytes() == b'new video'


def test_explicit_snapshot_directory_uses_sibling_standard_journal(tmp_path):
    from utils.file_ops import path_key
    svc = service(tmp_path)
    assert path_key(svc.journal.path) == path_key(tmp_path / 'media_operations.db')


def test_upgrade_service_accepts_explicit_isolated_operation_journal(tmp_path):
    from utils.file_ops import OperationJournal
    from utils.media_upgrade import MediaUpgradeService
    journal = OperationJournal(tmp_path / 'isolated-journal.db')
    svc = MediaUpgradeService(SimpleNamespace(jellyfin_server='', jellyfin_api_key=''), tmp_path / 'private', journal=journal)
    assert svc.journal is journal
