"""Preplanned local version upgrades with durable, private inheritance snapshots.

No retrieval, server database edits, guessed ItemIds, or playback session reporting.
The REST adapter deliberately reports metadata/UserData drift for manual review:
Jellyfin versions do not share a reliable writable all-user resume-state API.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlencode, quote, urlsplit, urlunsplit

from utils.file_ops import OperationJournal, file_hash, path_key
from utils.replacement import preview_release_replacements, execute_replacements, validate_restored_output

# Documented editable BaseItemDto fields. Do not persist credentials or read-only streams.
METADATA_FIELDS = ('Name', 'OriginalTitle', 'SortName', 'Overview', 'PremiereDate',
                   'ProductionYear', 'OfficialRating', 'CommunityRating', 'CriticRating',
                   'Genres', 'Tags', 'Studios', 'People', 'ProviderIds', 'Taglines',
                   'LockData', 'LockedFields', 'DateCreated', 'IndexNumber',
                   'ParentIndexNumber', 'EndDate', 'PreferredMetadataLanguage',
                   'PreferredMetadataCountryCode')
USER_FIELDS = ('Played', 'PlaybackPositionTicks', 'IsFavorite', 'PlayCount',
               'LastPlayedDate', 'Likes', 'Rating', 'UnplayedItemCount', 'PlayedPercentage', 'Key', 'ItemId')


def _identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('需要普通文件，拒绝符号链接或不存在的文件')
    before = path.stat()
    digest = file_hash(path)
    after = path.stat()
    attrs = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, key) != getattr(after, key) for key in attrs):
        raise ValueError('读取期间文件发生变化，请重新规划')
    return dict(path=path_key(path), sha256=digest,
                **{key: getattr(after, key) for key in attrs})


def _sidecars(target):
    target = Path(target)
    from utils.media_types import is_video
    found = []
    for path in sorted(target.parent.iterdir()):
        name = path.name.casefold()
        stem = target.stem.casefold()
        if path == target or not path.is_file() or is_video(path):
            continue
        if name == 'movie.nfo' or name.startswith(stem + '.') or name.startswith(stem + '-'):
            identity = _identity(path)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != identity['sha256']:
                raise ValueError('读取期间附属文件变化')
            found.append(dict(path=str(path), identity=identity,
                              bytes_b64=base64.b64encode(raw).decode('ascii')))
    return found


def _same_sidecars(row):
    current = _sidecars(row['target'])
    if [(v['path'], v['identity']) for v in current] != [
            (v['path'], v['identity']) for v in row['sidecars']]:
        raise ValueError('NFO/字幕/附属文件已变化，请重新审阅；未覆盖其他程序的修改')


def _stop(cancel):
    if cancel and cancel():
        raise ValueError('已取消')


def _write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # No shared temp directory or server credentials. Restrictive mode on POSIX;
    # Windows inherits the user's private application data directory ACL.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())


class _SnapshotJournal:
    def __init__(self, journal, snapshot_id):
        self.journal = journal
        self.snapshot_id = snapshot_id
        self.state = None

    def begin(self, *args, **kwargs):
        return self.journal.begin(*args, inheritance_snapshot_id=self.snapshot_id, **kwargs)

    def update(self, operation_id, state, **fields):
        result = self.journal.update(operation_id, state, **fields)
        self.state = state
        return result

    def __getattr__(self, name):
        return getattr(self.journal, name)


class JellyfinInheritanceAdapter:
    """Official REST only; every user's UserData is fetched with that user's ID.

    Sources: Jellyfin.Api/Controllers/{Items,UserLibrary,ItemRefresh}Controller.cs.
    Refresh returns 204 when queued, never when processing is finished.
    """
    def __init__(self, cfg):
        from utils.jellyfin_api import _base
        self.base = _base(cfg.jellyfin_server)
        self.key = cfg.jellyfin_api_key
        self.mappings = deepcopy(getattr(cfg, 'library_path_mappings', []) or [])

    def _request(self, method, path, body=None):
        from utils.jellyfin_api import _req, JellyfinError
        try:
            return _req(method, self.base + path, self.key, body=body)
        except JellyfinError:
            raise ValueError('服务器传输失败；连接细节未写入快照') from None

    def _local_path(self, remote):
        from utils.library_source import map_media_path
        from utils.library_scope import path_in_scope, normalize_scope_path
        matching = [m for m in self.mappings if isinstance(m, dict)
                    and m.get('remote') and m.get('local') and path_in_scope(remote, m['remote'])]
        if matching:
            longest = max(len(normalize_scope_path(m['remote'])) for m in matching)
            mapped = {path_key(map_media_path(remote, [m])) for m in matching
                      if len(normalize_scope_path(m['remote'])) == longest}
            if len(mapped) != 1:
                raise ValueError('服务器路径映射冲突，请先修正配置')
            return next(iter(mapped))
        return path_key(remote)

    def capture(self, path, cancel=None):
        _stop(cancel)
        matches = []
        start = 0
        while True:
            _stop(cancel)
            query = urlencode(dict(Recursive='true', Fields='Path', StartIndex=start, Limit=500))
            code, data = self._request('GET', '/Items?' + query)
            if code != 200 or not isinstance(data, dict):
                raise ValueError('无法读取服务器精确路径索引')
            items = data.get('Items') or []
            matches.extend(v for v in items if v.get('Path') and self._local_path(v['Path']) == path_key(path))
            start += len(items)
            if not items or start >= data.get('TotalRecordCount', start):
                break
        matches = {v.get('Id'): v for v in matches if v.get('Id')}
        if len(matches) != 1:
            raise ValueError('服务器路径未唯一匹配；需要手动路径映射，禁止按标识猜测 ItemId')
        item_id = next(iter(matches))
        remote_path = matches[item_id]['Path']
        code, users = self._request('GET', '/Users')
        if code != 200 or not isinstance(users, list) or not users:
            raise ValueError('无法枚举用户，不能保证用户状态快照完整')
        metadata = None
        states, inaccessible = {}, []
        for user in users:
            _stop(cancel)
            uid = user.get('Id')
            if not uid:
                raise ValueError('用户列表缺少 Id')
            code, item = self._request('GET', '/Users/' + quote(str(uid), safe='') + '/Items/' + quote(str(item_id), safe=''))
            if code in (403, 404):
                inaccessible.append(str(uid))
                continue
            if code != 200 or not isinstance(item, dict) or item.get('Id') != item_id:
                raise ValueError('无法快照某个用户的条目状态')
            if item.get('Path') != remote_path or self._local_path(item['Path']) != path_key(path):
                raise ValueError('服务器条目路径发生变化')
            if not isinstance(item.get('UserData'), dict):
                raise ValueError('服务器未提供该用户 UserData')
            states[str(uid)] = {k: deepcopy(item['UserData'][k]) for k in USER_FIELDS if k in item['UserData']}
            candidate_metadata = {k: deepcopy(item[k]) for k in METADATA_FIELDS if k in item}
            if metadata is None:
                metadata = candidate_metadata
            elif metadata != candidate_metadata:
                raise ValueError('读取期间服务器元数据变化，请重新快照')
        if metadata is None:
            raise ValueError('没有可读取条目的用户')
        return dict(item_id=item_id, path=remote_path, metadata=metadata,
                    users=states, inaccessible_users=inaccessible)

    def refresh(self, item_id):
        query = urlencode(dict(metadataRefreshMode='None', imageRefreshMode='None',
                               replaceAllMetadata='false', replaceAllImages='false'))
        code, _ = self._request('POST', '/Items/' + quote(str(item_id), safe='') + '/Refresh?' + query)
        if code not in (200, 204):
            raise ValueError('服务器受控刷新请求失败')
        return {'queued': True}


class MediaUpgradeService:
    def __init__(self, cfg, data_dir=None, *, server=None, journal=None):
        if data_dir is None:
            import config
            data_dir = Path(config.DATA_DIR) / 'media_upgrades'
        self.data_dir = Path(data_dir).resolve()
        # TaskCenterPage reads OperationJournal() at DATA_DIR/media_operations.db.
        # Explicit snapshot roots keep tests isolated using the same sibling layout.
        self.journal = journal if journal is not None else OperationJournal(self.data_dir.parent / 'media_operations.db')
        self.server = server
        from utils.media_server import server_identity
        origin = urlsplit(server_identity(getattr(cfg, 'jellyfin_server', '')))
        # Credentials and URL query parameters never appear in snapshot plaintext.
        endpoint = urlunsplit((origin.scheme, origin.netloc, origin.path, '', ''))
        mappings = [dict(remote=str(m.get('remote', '')), local=str(m.get('local', '')))
                    for m in (getattr(cfg, 'library_path_mappings', []) or []) if isinstance(m, dict)]
        def digest(value):
            return hashlib.sha256(value.encode('utf-8')).hexdigest()
        self.server_context = dict(
            origin=endpoint,
            endpoint_fingerprint=digest(server_identity(getattr(cfg, 'jellyfin_server', ''))),
            credential_fingerprint=digest(str(getattr(cfg, 'jellyfin_api_key', ''))),
            mapping_fingerprint=digest(json.dumps(mappings, sort_keys=True, ensure_ascii=False)),
            server_type=str(getattr(cfg, 'server_type', 'auto')),
            inheritance_enabled=self.server is not None or bool(getattr(cfg, 'jellyfin_api_key', '')),
        )
        self.configuration_error = ''
        if server is None and getattr(cfg, 'jellyfin_server', '') and not getattr(cfg, 'jellyfin_api_key', ''):
            self.configuration_error = '已配置服务器但缺少 API Key；无法快照和验证继承，请先完善配置'
        if server is None and getattr(cfg, 'jellyfin_api_key', ''):
            self.server = JellyfinInheritanceAdapter(cfg)

    def preview(self, candidates, originals, cancel=None):
        candidates, originals = [str(p) for p in candidates], [str(p) for p in originals]
        rows = preview_release_replacements(candidates, originals, should_stop=cancel)
        for row in rows:
            _stop(cancel)
            row['identifier'] = self._identifier(row['source'])
            row['inheritance_summary'] = '保留原路径及 NFO；执行前保存用户状态快照' if self.server else '本地保留原路径、NFO/字幕；未配置服务器验证'
            row['source_identity'] = _identity(row['source'])
            row['target_identity'] = _identity(row['target']) if row['target'] else None
            if row['target'] and os.path.samefile(row['source'], row['target']):
                row.update(status='conflict', error='候选与原文件是同一个文件身份（含硬链接）')
        return dict(schema_version=1, rows=rows, candidate_inputs=candidates, original_inputs=originals)

    @staticmethod
    def _identifier(path):
        from censor.numbering import parse_from_path
        from censor.releases.models import normalize_number
        return normalize_number(parse_from_path(path).number)

    def _check_pairing(self, plan, cancel=None):
        fresh = self.preview(plan['candidate_inputs'], plan['original_inputs'], cancel)
        index = {path_key(r['source']): r for r in fresh['rows']}
        for row in plan['rows']:
            other = index.get(path_key(row['source']))
            if not other or any(row.get(k) != other.get(k) for k in (
                    'source', 'target', 'candidates', 'source_identity', 'target_identity')):
                raise ValueError('原始配对计划已变化，请重新预览')
            if row['status'] == 'ready' and other['status'] != 'ready':
                raise ValueError('原始配对已不再唯一，请重新预览')

    def prepare(self, plan, cancel=None):
        _stop(cancel)
        self._check_pairing(plan, cancel)
        snapshot_id = uuid.uuid4().hex
        snapshot = deepcopy(plan)
        snapshot.update(snapshot_id=snapshot_id, status='prepared', server_context=deepcopy(self.server_context))
        for row in snapshot['rows']:
            _stop(cancel)
            if row['status'] != 'ready':
                continue
            try:
                row['sidecars'] = _sidecars(row['target'])
                if self.configuration_error:
                    raise ValueError(self.configuration_error)
                row['server'] = deepcopy(self.server.capture(row['target'], cancel)) if self.server else None
                self._check_files(row)
            except (OSError, ValueError) as exc:
                row.update(status='blocked', error=str(exc))
        path = self.data_dir / 'snapshots' / (snapshot_id + '.json')
        _write_private(path, snapshot)
        snapshot['snapshot_path'] = str(path)
        snapshot['snapshot_sha256'] = file_hash(path)
        return snapshot

    def _load(self, prepared):
        snapshot_id = prepared.get('snapshot_id', '')
        if not snapshot_id or any(v not in '0123456789abcdef' for v in snapshot_id):
            raise ValueError('无效快照标识')
        path = self.data_dir / 'snapshots' / (snapshot_id + '.json')
        if path_key(prepared['snapshot_path']) != path_key(path) or file_hash(path) != prepared['snapshot_sha256']:
            raise ValueError('快照文件变化或位置错误')
        saved = json.loads(path.read_text(encoding='utf-8'))
        if any(saved.get(k) != prepared.get(k) for k in saved):
            raise ValueError('已准备计划被修改，请重新创建快照')
        if saved.get('server_context') != self.server_context:
            raise ValueError('快照的服务器来源、凭据或路径映射配置已变化；请重新快照，禁止跨服务器执行或验证')
        return saved

    @staticmethod
    def _check_files(row):
        if _identity(row['source']) != row['source_identity'] or _identity(row['target']) != row['target_identity']:
            raise ValueError('文件身份/内容已变化，请重新审阅')
        if Path(row['source']).suffix.lower() != Path(row['target']).suffix.lower():
            raise ValueError('封装格式不一致')
        _same_sidecars(row)

    def execute(self, prepared, cancel=None, log=print, validator=None):
        results = dict(ok=0, fail=0, needs_attention=0, cancelled=False, rows=[], operations=[])
        try:
            saved = self._load(prepared)
            self._check_pairing(saved, cancel)
        except (OSError, ValueError, KeyError) as exc:
            if cancel and cancel():
                results.update(cancelled=True, error='已取消')
            else:
                results.update(fail=max(1, len(prepared.get('rows', []))), error=str(exc))
            return results
        for row in saved['rows']:
            if cancel and cancel():
                results['cancelled'] = True
                break
            outcome = dict(source=row['source'], target=row['target'], status='failed', error='')
            try:
                if row['status'] != 'ready':
                    raise ValueError(row.get('error') or '计划未就绪')
                self._check_files(row)
                if row['server'] is not None:
                    if self.server is None or self.server.capture(row['target'], cancel) != row['server']:
                        raise ValueError('服务器身份、元数据或用户状态已变化，请重新快照')
                operation_journal = _SnapshotJournal(self.journal, saved['snapshot_id'])
                def check_server_state():
                    if row['server'] is not None and (self.server is None or self.server.capture(row['target'], cancel) != row['server']):
                        raise ValueError('服务器身份、元数据或用户状态在暂存/备份期间变化，拒绝替换')
                def commit_guard():
                    if cancel and cancel():
                        return True
                    # safe_replace calls this after copying the original backup,
                    # immediately before installation. Never compare old file IDs
                    # after state=committed: cancellation then uses journal rollback.
                    if operation_journal.state == 'staged':
                        self._check_files(row)
                        check_server_state()
                    return False
                def staged_check(output):
                    self._check_files(row)
                    staged_identity = _identity(output)
                    if staged_identity['sha256'] != row['source_hash']:
                        raise ValueError('暂存文件与候选内容不符')
                    check = validator(output) if validator else validate_restored_output(row['target'], output)
                    self._check_files(row)
                    if _identity(output) != staged_identity:
                        raise ValueError('校验期间暂存文件发生变化，拒绝落位')
                    check_server_state()
                    return check
                execution = execute_replacements([row], journal=operation_journal,
                    should_stop=commit_guard, log=log, validator=staged_check)
                results['operations'].extend(execution['operations'])
                if execution['cancelled']:
                    results['cancelled'] = True
                    outcome.update(status='cancelled', error='已停止；原件保留或已回滚')
                    results['rows'].append(outcome)
                    break
                if not execution['ok']:
                    raise ValueError(str(execution['fails']) if execution['fails'] else '替换取消')
                outcome['status'] = 'replaced'
                # Media changed now; any subsequent issue requires attention, not a false file failure.
                try:
                    _same_sidecars(row)
                    if row['server'] is not None:
                        self.server.refresh(row['server']['item_id'])
                        outcome.update(status='needs_attention', error='刷新已排队；完成后须手动执行继承验证，尚未确认继承')
                    else:
                        outcome['inheritance_summary'] = '原路径及附属文件字节保留；未验证服务器状态'
                except (OSError, ValueError) as exc:
                    outcome.update(status='needs_attention', error=str(exc))
                if outcome['status'] == 'needs_attention':
                    results['needs_attention'] += 1
                else:
                    results['ok'] += 1
            except (OSError, ValueError, KeyError, RuntimeError) as exc:
                outcome['error'] = str(exc)
                if cancel and cancel():
                    results['cancelled'] = True
                    outcome['status'] = 'cancelled'
                else:
                    results['fail'] += 1
            results['rows'].append(outcome)
        _write_private(self.data_dir / 'results' / (uuid.uuid4().hex + '.json'), results)
        return results

    def verify(self, prepared, cancel=None):
        """Read-only verification after the operator observes server refresh completion.

        Does not restore via undocumented fields or overwrite newer playback progress.
        The original snapshots remain available for a reviewed manual restoration.
        """
        saved = self._load(prepared)
        rows = []
        for row in saved['rows']:
            _stop(cancel)
            result = dict(source=row['source'], target=row['target'], status='verified', error='')
            try:
                if row['status'] != 'ready':
                    raise ValueError('快照未就绪')
                if file_hash(row['target']) != row['source_hash']:
                    raise ValueError('目标视频与升级候选不一致')
                _same_sidecars(row)
                before = row['server']
                if before is None:
                    raise ValueError('未配置服务器，只能确认本地文件与附属文件')
                if self.server is None:
                    raise ValueError('服务器连接不可用')
                after = self.server.capture(row['target'], cancel)
                if after['item_id'] != before['item_id']:
                    raise ValueError('ItemId 已变化，禁止猜测恢复目标；需要人工恢复用户状态')
                if before.get('inaccessible_users') or after.get('inaccessible_users'):
                    raise ValueError('存在无法读取状态的用户；仅已快照用户可核对，完整继承仍需人工验证')
                for key in ('path', 'metadata', 'users', 'inaccessible_users'):
                    if before.get(key) != after.get(key):
                        raise ValueError(key + ' 已变化；快照已保留，需要人工审阅恢复；未伪造播放进度')
            except (OSError, ValueError, KeyError) as exc:
                result.update(status='needs_attention', error=str(exc))
            rows.append(result)
        return dict(rows=rows)

