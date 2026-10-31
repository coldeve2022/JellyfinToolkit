"""Read-only, explicit-user favorites/watch snapshots; incomplete scans never publish.

Cache absence means unknown. Callers must use ``dict.get`` and must not infer an
unfavorited item from an absent key. Services capture their configuration at creation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from weakref import WeakValueDictionary
from urllib.parse import quote, urlencode

from utils import jellyfin_api as api
from utils import library_source as source
from utils.library import (_readonly_conn, _column_names, _guid_str, _behavior_user,
                           table_names, LibraryCollectionCancelled)
from utils.library_source import _get, map_media_path
from utils.library_scope import normalize_scope_path, path_in_scope, scope_id, _paths
from utils.media_server import server_identity


_review_services = WeakValueDictionary()


def shared_review_service(cfg, data_dir=None):
    """Reuse resolved source identity only for this exact profile and context.

    Owned by views/controllers; this registry neither discovers a source nor
    persists credentials or selects another account's most recent snapshot.
    """
    candidate = JellyfinReviewService(cfg, data_dir=data_dir)
    key = (str(candidate._db_path.resolve()), candidate.context_key)
    previous = _review_services.get(key)
    if previous is not None:
        return previous
    _review_services[key] = candidate
    return candidate


class JellyfinReviewError(Exception):
    """Fixed, safe-to-display error; upstream exception text is never forwarded."""


class _Cancelled(Exception):
    pass


class JellyfinReviewService:
    PAGE_SIZE = 500

    def __init__(self, cfg, data_dir=None):
        self.cfg = deepcopy(cfg)
        self.server = str(_get(cfg, 'jellyfin_server', '') or '').strip()
        self.user_id = str(_get(cfg, 'jellyfin_user_id', '') or '').strip()
        self._api_key = str(_get(cfg, 'jellyfin_api_key', '') or '')
        self._selected = list(_get(cfg, 'library_scope_ids', []) or [])
        self._roots = list(_get(cfg, 'library_scope_paths', []) or [])
        self._mappings = deepcopy(_get(cfg, 'library_path_mappings', []) or [])
        self._automatic_source = (str(_get(cfg, 'library_source', 'auto')) == 'auto'
                                  and not _get(cfg, 'jellyfin_db_path', ''))
        self._resolved_identity = None
        context = {'version': 1, 'server': server_identity(self.server), 'user_id': self.user_id,
                   'credential_fingerprint': hashlib.sha256(self._api_key.encode()).hexdigest(),
                   'source_mode': _get(cfg, 'library_source', 'auto'),
                   'db_path': normalize_scope_path(str(_get(cfg, 'jellyfin_db_path', '') or '')),
                   'scope_ids': sorted(self._selected),
                   'scope_paths': sorted(normalize_scope_path(p) for p in self._roots),
                   'mappings': self._mappings}
        self.context_key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if data_dir is None:
            from config import DATA_DIR
            data_dir = DATA_DIR
        self._db_path = Path(data_dir) / 'jellyfin_review.sqlite3'

    normalize_path = staticmethod(normalize_scope_path)

    @staticmethod
    def _check(should_stop):
        if should_stop and should_stop():
            raise _Cancelled()

    def _read(self, endpoint, should_stop=None):
        self._check(should_stop)
        if not self.server or not self._api_key:
            raise JellyfinReviewError('请先配置服务器地址和 API Key')
        try:
            status, data = api._req('GET', api._base(self.server) + endpoint, self._api_key, timeout=30)
        except Exception:
            raise JellyfinReviewError('读取服务器失败，请检查连接和访问权限') from None
        self._check(should_stop)
        if status != 200:
            raise JellyfinReviewError('读取服务器失败，请检查连接和访问权限')
        return data

    def list_users(self, should_stop=None):
        try:
            local = self._local_source()
            if local is not None:
                self._check(should_stop)
                conn = _readonly_conn(str(local['path']))
                try:
                    table = self._user_table(conn)
                    ids = {_guid_str(r[0]) for r in conn.execute(f'SELECT DISTINCT UserId FROM {table}')
                           if r[0] is not None} if table else set()
                    names = self._account_names(conn)
                    self._check(should_stop)
                    return [dict({'id': uid, 'name': names.get(self._user_key(uid), uid)},
                                 **({'source_db_path': str(local['path'])} if self._automatic_source else {}))
                            for uid in sorted(ids) if uid]
                finally:
                    conn.close()
            rows = self._read('/Users', should_stop)
            if not isinstance(rows, list):
                raise JellyfinReviewError('服务器未返回有效用户列表')
            return [{'id': str(row['Id']), 'name': str(row.get('Name') or row['Id'])}
                    for row in rows if isinstance(row, dict) and row.get('Id')]
        except _Cancelled:
            return []
        except JellyfinReviewError:
            raise
        except Exception:
            raise JellyfinReviewError('无法读取观看用户，请检查数据源') from None

    def _local_source(self):
        # Resolution may discover a local DB; call only in background operations.
        if str(_get(self.cfg, 'library_source', 'auto')) == 'api':
            return None
        kind, opts = source.resolve(self.cfg)
        self._resolved_identity = ('db:' + normalize_scope_path(str(opts['path']))
                                   if kind == source.SOURCE_DB else 'server:' + server_identity(self.server))
        return opts if kind == source.SOURCE_DB else None

    def _snapshot_key(self):
        # An unresolved auto config cannot safely identify any previous source.
        if self._automatic_source:
            if not self._resolved_identity:
                return None
            return hashlib.sha256((self.context_key + '\0' + self._resolved_identity).encode()).hexdigest()
        return self.context_key

    @staticmethod
    def _user_table(conn):
        names = table_names(conn)
        return next((table for table in ('userdata', 'userdatas')
                     if table in names and 'userid' in _column_names(conn, table)), None)

    @staticmethod
    def _user_key(value):
        return _guid_str(value).replace('-', '').lower()

    @classmethod
    def _account_names(cls, conn):
        """Optional display labels from this exact DB; never discover other files."""
        if 'users' not in table_names(conn):
            return {}
        columns = _column_names(conn, 'users')
        labels = [column for column in ('username', 'name') if column in columns]
        if 'id' not in columns or not labels:
            return {}
        names, ambiguous = {}, set()
        for row in conn.execute('SELECT Id, ' + ', '.join(labels) + ' FROM Users'):
            key = cls._user_key(row[0])
            label = next((value.strip() for value in row[1:] if isinstance(value, str) and value.strip()), '')
            if key and label:
                if key in names and names[key] != label:
                    ambiguous.add(key)
                names[key] = label
        return {key: label for key, label in names.items() if key not in ambiguous}

    def _local_rows(self, opts, should_stop):
        ticks = {}
        present_ids = set()
        conn = _readonly_conn(str(opts['path']))
        try:
            table = self._user_table(conn)
            selected, known = _behavior_user(conn, table, self.user_id) if table else (None, False)
            if not known:
                raise JellyfinReviewError('已选择的观看用户不存在或不可访问，请重新选择')
            if 'itemid' in _column_names(conn, table):
                present_ids = {_guid_str(row[0]) for row in conn.execute(
                    f'SELECT ItemId FROM {table} WHERE UserId=?', (selected,))}
            if {'itemid', 'playbackpositionticks'} <= _column_names(conn, table):
                ticks = {_guid_str(row[0]): self._number(row[1]) for row in conn.execute(
                    f'SELECT ItemId, PlaybackPositionTicks FROM {table} WHERE UserId=?', (selected,))}
        finally:
            conn.close()
        # Pin this scan to the DB just resolved; do not rediscover halfway through.
        pinned = deepcopy(self.cfg)
        if isinstance(pinned, dict):
            pinned['jellyfin_db_path'] = str(opts['path'])
        else:
            setattr(pinned, 'jellyfin_db_path', str(opts['path']))
        items = source.collect(pinned, should_stop=should_stop)
        rows, missing = {}, 0
        for item in items:
            self._check(should_stop)
            remote = item.source_path or item.path
            if not normalize_scope_path(remote):
                missing += 1
                continue
            path, mapped = self._mapped_path(remote)
            key = normalize_scope_path(path)
            known = bool(item.behavior_known and _guid_str(item.behavior_user_id).replace('-', '').lower()
                         == self.user_id.replace('-', '').lower() and mapped and str(item.id) in present_ids)
            row = {'item_id': str(item.id), 'path': path, 'remote_path': remote, 'mapped': mapped,
                   'name': str(item.name or ''),
                   'user_id': self.user_id, 'is_favorite': bool(item.is_favorite) if known else None,
                   'played': bool(item.is_played) if known else None,
                   'play_count': self._number(item.play_count) if known else 0,
                   'playback_ticks': ticks.get(str(item.id), 0) if known else 0,
                   'has_progress': bool(item.has_progress) if known else None,
                   'tags': list(item.tags), 'genres': list(item.genres)}
            if key in rows:
                rows[key].update(is_favorite=None, played=None, mapped=False, ambiguous=True,
                                 has_progress=None, playback_ticks=0, play_count=0)
            else:
                rows[key] = row
        return rows, missing

    def _publish(self, collected, summary, should_stop):
        self._check(should_stop)
        from utils.user_state_store import UserStateStore
        try:
            UserStateStore(self._db_path).publish(self._snapshot_key(), collected, should_stop=should_stop)
        except InterruptedError as exc:
            raise _Cancelled() from exc
        summary.update(ok=True, count=len(collected), favorites=sum(r['is_favorite'] is True for r in collected.values()),
                       unknown=summary['unknown'] + sum(r['is_favorite'] is None for r in collected.values()))

    def _scope_roots(self, should_stop):
        if any(not isinstance(p, str) or not normalize_scope_path(p) for p in self._roots):
            raise JellyfinReviewError('媒体库范围无效，请重新选择')
        roots = list(self._roots)
        if self._selected:
            rows = self._read('/Library/VirtualFolders', should_stop)
            if not isinstance(rows, list):
                raise JellyfinReviewError('无法验证媒体库范围，请重新选择')
            catalog = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                native = str(row.get('ItemId') or row.get('Id') or '')
                if native:
                    catalog[scope_id('server:' + server_identity(self.server), native)] = (
                        _paths(row.get('Locations')) or _paths((row.get('LibraryOptions') or {}).get('PathInfos')))
            if any(not catalog.get(key) for key in self._selected):
                raise JellyfinReviewError('无法验证媒体库范围，请重新选择')
            for key in self._selected:
                roots.extend(catalog[key])
        return roots

    def _mapped_path(self, remote):
        """Reject conflicting longest-prefix maps; no basename/substring guesses."""
        matching = [m for m in self._mappings if isinstance(m, dict)
                    and m.get('remote') and m.get('local') and path_in_scope(remote, m['remote'])]
        if matching:
            longest = max(len(normalize_scope_path(m['remote'])) for m in matching)
            candidates = {normalize_scope_path(map_media_path(remote, [m]))
                          for m in matching if len(normalize_scope_path(m['remote'])) == longest}
            if len(candidates) != 1 or not next(iter(candidates)):
                return remote, False
            mapped = map_media_path(remote, matching)
            return mapped, True
        return remote, bool(os.path.isfile(remote))

    @staticmethod
    def _number(value):
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0

    def refresh(self, should_stop=None):
        summary = {'context_key': self.context_key, 'ok': False, 'cancelled': False,
                   'count': 0, 'favorites': 0, 'unknown': 0, 'error': ''}
        try:
            self._check(should_stop)
            if not self.user_id:
                raise JellyfinReviewError('请明确选择观看用户')
            local = self._local_source()
            if local is not None:
                rows, summary['unknown'] = self._local_rows(local, should_stop)
                self._publish(rows, summary, should_stop)
                summary['effective_source_path'] = str(local['path'])
                return summary
            users = self.list_users(should_stop)
            self._check(should_stop)
            if len([u for u in users if u['id'] == self.user_id]) != 1:
                raise JellyfinReviewError('已选择的观看用户不存在或不可访问，请重新选择')
            roots = self._scope_roots(should_stop)
            collected, start, expected = {}, 0, None
            while True:
                params = urlencode({'recursive': 'true', 'includeItemTypes': 'Movie,Video,Episode,Trailer,MusicVideo',
                                    'fields': 'UserData,Path,Tags,Genres', 'enableUserData': 'true',
                                    'startIndex': start, 'limit': self.PAGE_SIZE, 'enableTotalRecordCount': 'true'})
                data = self._read('/Users/' + quote(self.user_id, safe='') + '/Items?' + params, should_stop)
                if not isinstance(data, dict) or not isinstance(data.get('Items'), list):
                    raise JellyfinReviewError('服务器未返回完整条目列表')
                items = data['Items']
                total = data.get('TotalRecordCount')
                if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    raise JellyfinReviewError('服务器未返回完整条目数量')
                if expected is not None and total != expected:
                    raise JellyfinReviewError('扫描期间媒体库发生变化，请重新刷新')
                expected = total
                if not items and start < total:
                    raise JellyfinReviewError('服务器未返回完整条目列表')
                for item in items:
                    self._check(should_stop)
                    if not isinstance(item, dict) or not item.get('Id'):
                        raise JellyfinReviewError('服务器未返回有效条目')
                    remote = item.get('Path')
                    if not isinstance(remote, str) or not normalize_scope_path(remote):
                        summary['unknown'] += 1
                        continue
                    if roots and not any(path_in_scope(remote, root) for root in roots):
                        continue
                    path, mapped = self._mapped_path(remote)
                    key = normalize_scope_path(path)
                    ud = item.get('UserData') if isinstance(item.get('UserData'), dict) else {}
                    row = {'item_id': str(item['Id']), 'path': path, 'remote_path': remote,
                           'name': str(item.get('Name') or ''),
                           'user_id': self.user_id, 'mapped': mapped,
                           'is_favorite': ud.get('IsFavorite') if mapped and type(ud.get('IsFavorite')) is bool else None,
                           'played': ud.get('Played') if mapped and type(ud.get('Played')) is bool else None,
                           'playback_ticks': self._number(ud.get('PlaybackPositionTicks')) if mapped else 0,
                           'play_count': self._number(ud.get('PlayCount')) if mapped else 0,
                           'has_progress': (self._number(ud.get('PlaybackPositionTicks')) > 0) if mapped and ud else None,
                           'tags': [s for s in (item.get('Tags') or []) if isinstance(s, str)],
                           'genres': [s for s in (item.get('Genres') or []) if isinstance(s, str)]}
                    if key in collected:
                        collected[key].update(is_favorite=None, played=None, mapped=False, ambiguous=True,
                                              has_progress=None, playback_ticks=0, play_count=0)
                    else:
                        collected[key] = row
                start += len(items)
                if start >= total:
                    if start != total:
                        raise JellyfinReviewError('服务器未返回完整条目数量')
                    break
            self._check(should_stop)
            # One transaction publishes a whole context; previous context survives any failure.
            self._publish(collected, summary, should_stop)
        except (_Cancelled, LibraryCollectionCancelled):
            summary['cancelled'] = True
        except JellyfinReviewError as exc:
            summary['error'] = str(exc)
        except Exception:
            summary['error'] = '无法完成用户状态快照，已保留上次结果'
        return summary

    def read_snapshot(self):
        """Structured generic read; the legacy cached_items API remains unchanged."""
        from utils.user_state_store import UserStateStore
        return UserStateStore(self._db_path).read(self._snapshot_key(), self.user_id)

    def cached_items(self):
        if not self._db_path.is_file() or not self.user_id or not self._snapshot_key():
            return {}
        try:
            with sqlite3.connect(self._db_path.as_uri() + '?mode=ro', uri=True) as conn:
                row = conn.execute('SELECT payload FROM snapshots WHERE context=?', (self._snapshot_key(),)).fetchone()
            payload = json.loads(row[0]) if row else {}
            if not isinstance(payload, dict):
                return {}
            for key, item in payload.items():
                if not isinstance(key, str) or not isinstance(item, dict):
                    return {}
                if any(not isinstance(item.get(field), str)
                       for field in ('item_id', 'path', 'remote_path', 'user_id')):
                    return {}
                if (normalize_scope_path(item['path']) != key or not key
                        or item['user_id'] != self.user_id or type(item.get('mapped')) is not bool):
                    return {}
                if any(field not in item or (item[field] is not None and type(item[field]) is not bool)
                       for field in ('is_favorite', 'played')):
                    return {}
                if any(item.get(field) is not None and type(item[field]) is not bool
                       for field in ('has_progress', 'ambiguous')):
                    return {}
                if any(type(item.get(field)) is not int or item[field] < 0
                       for field in ('playback_ticks', 'play_count')):
                    return {}
                if any(not isinstance(item.get(field), list)
                       or any(not isinstance(value, str) for value in item[field]) for field in ('tags', 'genres')):
                    return {}
                if 'name' in item and not isinstance(item['name'], str):
                    return {}
                if not item['mapped'] and any(item[field] is not None for field in ('is_favorite', 'played')):
                    return {}
            return payload
        except (sqlite3.Error, ValueError, OSError):
            return {}

    def favorite_paths(self):
        return [row['path'] for row in self.cached_items().values()
                if row.get('mapped') and row.get('is_favorite') is True]
