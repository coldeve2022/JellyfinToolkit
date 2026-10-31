"""Versioned, context-scoped user state with atomic legacy compatibility.

Reads never create or migrate files. The legacy JSON remains the compatibility
authority: a digest detects writes made by an older application. Only successful
publication creates the indexed projection, in the same SQLite transaction.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from utils.library_scope import normalize_scope_path


@dataclass(frozen=True)
class SnapshotRead:
    status: str
    items: dict = field(default_factory=dict)
    origin: str = ''
    updated_at: str | None = None
    revision: int = 0
    message: str = ''


class _Unsupported(ValueError):
    pass


_FIELDS = ('path', 'item_id', 'user_id', 'remote_path', 'mapped', 'name', 'is_favorite',
           'played', 'play_count', 'playback_ticks', 'has_progress', 'ambiguous', 'tags', 'genres')
_BOOLS = {'mapped', 'is_favorite', 'played', 'has_progress', 'ambiguous'}
_COLUMNS = ','.join({'is_favorite': 'favorite', 'path': 'original_path'}.get(f, f) for f in _FIELDS)
_SCHEMA = (
    'CREATE TABLE IF NOT EXISTS snapshots(context TEXT PRIMARY KEY,payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS user_state_schema(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS user_state_snapshots(context TEXT PRIMARY KEY,revision INTEGER NOT NULL,'
    'updated_at TEXT NOT NULL,legacy_digest TEXT NOT NULL,row_count INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS user_state_items(context TEXT NOT NULL,path TEXT NOT NULL,'
    'original_path TEXT NOT NULL,item_id TEXT NOT NULL,user_id TEXT NOT NULL,remote_path TEXT NOT NULL,mapped INTEGER NOT NULL,'
    'name TEXT,favorite INTEGER,played INTEGER,play_count INTEGER NOT NULL,playback_ticks INTEGER NOT NULL,'
    'has_progress INTEGER,ambiguous INTEGER,tags TEXT NOT NULL,genres TEXT NOT NULL,'
    'PRIMARY KEY(context,path),FOREIGN KEY(context) REFERENCES user_state_snapshots(context) ON DELETE CASCADE)',
    'CREATE INDEX IF NOT EXISTS idx_user_state_favorite ON user_state_items(context,favorite)',
    'CREATE INDEX IF NOT EXISTS idx_user_state_played ON user_state_items(context,played)',
)


def _validate(items, user=None):
    if not isinstance(items, dict):
        raise ValueError('用户状态格式无效')
    users = set()
    for key, item in items.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise ValueError('用户状态格式无效')
        if any(not isinstance(item.get(f), str) for f in ('item_id', 'path', 'remote_path', 'user_id')):
            raise ValueError('用户状态字段无效')
        if not key or normalize_scope_path(item['path']) != key or (user and item['user_id'] != user):
            raise ValueError('用户状态上下文不匹配')
        users.add(item['user_id'])
        if type(item.get('mapped')) is not bool:
            raise ValueError('路径映射状态无效')
        for f in _BOOLS - {'mapped'}:
            if item.get(f) is not None and type(item[f]) is not bool:
                raise ValueError('用户状态字段无效')
        if any(f not in item for f in ('is_favorite', 'played')):
            raise ValueError('用户状态字段缺失')
        if any(type(item.get(f)) is not int or item[f] < 0 for f in ('play_count', 'playback_ticks')):
            raise ValueError('播放计数无效')
        if any(not isinstance(item.get(f), list) or any(not isinstance(v, str) for v in item[f])
               for f in ('tags', 'genres')):
            raise ValueError('标签字段无效')
        if 'name' in item and not isinstance(item['name'], str):
            raise ValueError('名称字段无效')
        if not item['mapped'] and any(item[f] is not None for f in ('is_favorite', 'played')):
            raise ValueError('未匹配条目的状态必须保持未知')
    if len(users) > 1:
        raise ValueError('不能混合多个观看账号')


def _digest(raw):
    if not isinstance(raw, str):
        raise ValueError('用户状态格式无效')
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class UserStateStore:
    def __init__(self, path):
        self.path = Path(path).resolve()

    @staticmethod
    def _check_version(conn):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='user_state_schema'").fetchone()
        row = conn.execute('SELECT version FROM user_state_schema WHERE id=1').fetchone() if exists else None
        if row and row[0] != 1:
            raise _Unsupported('用户状态库结构版本不受支持，请使用匹配版本程序')

    def read(self, context, user):
        if not user:
            return SnapshotRead('no_user', message='请选择观看账号')
        if not context:
            return SnapshotRead('unresolved', message='数据来源尚未确定，请同步用户状态')
        if not self.path.is_file():
            return SnapshotRead('missing', message='尚无此账号和范围的完整快照')
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=3)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                self._check_version(conn)
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                row = conn.execute('SELECT payload FROM snapshots WHERE context=?', (context,)).fetchone() \
                    if 'snapshots' in tables else None
                if row is None:
                    return SnapshotRead('missing', message='尚无此账号和范围的完整快照')
                raw = row[0]
                header = conn.execute('SELECT revision,updated_at,legacy_digest,row_count '
                    'FROM user_state_snapshots WHERE context=?', (context,)).fetchone() \
                    if 'user_state_snapshots' in tables else None
                if not header or header[2] != _digest(raw):
                    items = json.loads(raw)
                    _validate(items, user)
                    return SnapshotRead('ready', items, origin='legacy')
                items = {}
                for path, *values in conn.execute(f'SELECT path,{_COLUMNS} FROM user_state_items WHERE context=?',
                                                  (context,)):
                    item = {'path': path}
                    for f, value in zip(_FIELDS, values):
                        if f in ('name', 'ambiguous') and value is None:
                            continue
                        if f in _BOOLS and value is not None:
                            if value not in (0, 1):
                                raise ValueError('用户状态字段无效')
                            value = bool(value)
                        item[f] = json.loads(value) if f in ('tags', 'genres') else value
                    items[path] = item
                if len(items) != header[3]:
                    raise ValueError('用户状态快照不完整')
                _validate(items, user)
                return SnapshotRead('ready', items, 'relational', header[1], header[0])
        except _Unsupported as exc:
            return SnapshotRead('unsupported', message=str(exc))
        except (ValueError, TypeError, KeyError):
            return SnapshotRead('invalid', message='用户状态缓存无效或账号不匹配，请重新同步')
        except (OSError, sqlite3.Error):
            return SnapshotRead('unavailable', message='用户状态库暂不可读，请检查资料库位置和访问权限')

    def publish(self, context, items, *, should_stop=None):
        if not isinstance(context, str) or not context:
            raise ValueError('用户状态必须有明确上下文')
        _validate(items)
        raw = json.dumps(items, ensure_ascii=False)
        def check():
            if should_stop and should_stop():
                raise InterruptedError('用户状态同步已取消')
        check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            self._check_version(conn)
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute('INSERT OR IGNORE INTO user_state_schema VALUES(1,1)')
            check()
            conn.execute('INSERT INTO user_state_snapshots VALUES(?,1,?,?,?) '
                'ON CONFLICT(context) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at,'
                'legacy_digest=excluded.legacy_digest,row_count=excluded.row_count',
                (context, datetime.now(timezone.utc).isoformat(), _digest(raw), len(items)))
            conn.execute('DELETE FROM user_state_items WHERE context=?', (context,))
            for path, item in items.items():
                check()
                values = [json.dumps(item[f], ensure_ascii=False) if f in ('tags', 'genres') else item.get(f)
                          for f in _FIELDS]
                conn.execute(f'INSERT INTO user_state_items(context,path,{_COLUMNS}) VALUES '
                             f'({",".join("?" for _ in range(2 + len(values)))})', (context, path, *values))
            conn.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)', (context, raw))
            check()
