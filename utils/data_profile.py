"""Persistent library selection; no copying, merging or opening stores for writing."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

PROFILE_FILENAME = 'data-profile.json'


class ProfileError(ValueError):
    """Stop startup rather than silently switch to a new empty library."""


class ProfileChoiceRequired(ProfileError):
    def __init__(self, candidates):
        super().__init__('发现多个资料库，请明确选择；不会自动覆盖或合并')
        self.candidates = candidates


def read_profile(registry):
    path = Path(registry)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProfileError('默认资料库登记无法读取，请通过资料库选择恢复；原数据库未更改') from exc
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or not isinstance(value.get('root'), str)
            or not value['root'].strip() or not Path(value['root']).is_absolute()
            or type(value.get('database_required', False)) is not bool):
        raise ProfileError('默认资料库登记格式无效或来自未来版本；未创建空资料库')
    return value


def registered_root(registry):
    value = read_profile(registry)
    return Path(value['root']).resolve() if value else None


def inspect_root(root):
    root = Path(root).expanduser().resolve()
    database = root / 'data/censor.db'
    result = dict(root=root, database=database, scans=0, labels=0, history=0,
                  database_exists=database.is_file(), config_exists=(root / 'config.json').is_file())
    if not result['database_exists']:
        return result
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'censor_probe', 'censor_label'} <= tables:
                raise ProfileError('所选资料库缺少扫描/人工记录表，未改写数据库')
            required = {
                'censor_probe': {'path', 'size', 'mtime', 'duration', 'number', 'provider',
                    'state', 'confidence', 'needs_review', 'reason', 'evidence', 'candidates',
                    'visual_score', 'elapsed', 'error', 'updated_at'},
                'censor_label': {'path', 'state', 'note', 'labeled_at'},
            }
            for table, names in required.items():
                columns = {row[1] for row in conn.execute('PRAGMA table_info(' + table + ')')}
                if not names <= columns:
                    raise ProfileError('资料库基础字段缺失，未登记或改写：' + table)
            from censor.store import VerdictStore
            try:
                VerdictStore._schema_version(conn)
            except (RuntimeError, ValueError, TypeError) as exc:
                raise ProfileError('资料库结构版本不受当前程序支持；未登记或写入：' + str(exc)) from exc
            for field, table in (('scans', 'censor_probe'), ('labels', 'censor_label'),
                                 ('history', 'censor_label_history')):
                if table in tables:
                    result[field] = conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
    except (sqlite3.Error, OSError) as exc:
        raise ProfileError('资料库不可读或损坏，未改写数据库：' + str(database)) from exc
    return result


def profile_diagnostics(root, registry=None):
    """Read metadata only; never select, repair, create or merge a profile."""
    info = inspect_root(root)
    info.update(registered_root=None, registry_error=False, schema_versions={}, schema_error=False,
                snapshot_status='missing', snapshot_contexts=0,
                snapshot_database=info['root'] / 'data/jellyfin_review.sqlite3',
                evidence_directory=info['root'] / 'data/censor_thumbs')
    if registry is not None:
        try:
            from utils.profile_runtime import read_local_pointer
            info['registered_root'] = read_local_pointer(Path(registry).parent) if Path(registry).name == PROFILE_FILENAME else registered_root(registry)
        except (ProfileError, OSError):
            info['registry_error'] = True
    if info['database_exists']:
        try:
            with closing(sqlite3.connect(info['database'].as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='toolkit_schema_versions'").fetchone():
                    info['schema_versions'] = dict(conn.execute(
                        "SELECT component,version FROM toolkit_schema_versions WHERE component='censor_verdict'"))
        except (OSError, sqlite3.Error):
            info['schema_error'] = True
    db = info['snapshot_database']
    if db.is_file():
        try:
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'snapshots' in tables:
                    info['snapshot_contexts'] = conn.execute('SELECT count(*) FROM snapshots').fetchone()[0]
                    info['snapshot_status'] = 'legacy'
                if 'user_state_schema' in tables:
                    version = conn.execute('SELECT version FROM user_state_schema WHERE id=1').fetchone()
                    info['snapshot_status'] = 'ready' if version and version[0] == 1 else 'unsupported'
        except (OSError, sqlite3.Error):
            info['snapshot_status'] = 'unavailable'
    return info


def format_profile_diagnostics(info):
    lines = ['本次运行资料库：' + str(info['root']), '扫描数据库：' + str(info['database'])]
    if info['database_exists']:
        lines.append(f"扫描记录 {info['scans']} · 当前人工标记 {info['labels']} · 文件版本标记 {info['history']}")
        versions = info['schema_versions']
        version_text = ', '.join(f'{k}={v}' for k, v in versions.items()) or '旧结构，未登记版本'
        lines.append('结构版本：' + ('暂不可读，请刷新重试' if info['schema_error'] else version_text))
    else:
        lines.append('未找到扫描数据库；未创建空库')
    registered = info['registered_root']
    lines.append('下次普通启动登记：' + (str(registered) if registered else '尚未登记'))
    if registered is not None and registered != info['root']:
        lines.append('登记位置与本次不同：重启后才可能切换；本次没有切换或合并。显式目录和便携模式优先。')
    if info['registry_error']:
        lines.append('默认资料库登记不可读；本次未修改登记')
    state = info['snapshot_status']
    description = {'missing': '尚无用户状态库', 'legacy': '旧格式；可兼容读取，快照时间未知',
                   'ready': '关系格式与旧格式兼容保存', 'unsupported': '结构版本不受支持',
                   'unavailable': '用户状态库不可读，扫描记录统计不受影响'}[state]
    lines.extend(['用户状态数据库：' + str(info['snapshot_database']),
                  f"用户状态：{description} · 上下文快照 {info['snapshot_contexts']} 份（不是视频条数）",
                  '证据图片目录：' + str(info['evidence_directory']),
                  '以上为数据库原始条数，不等于当前筛选可见数；诊断不读取密钥、媒体内容或快照明细。'])
    return '\n'.join(lines)


def discover_roots(program_root, shared_root):
    program = Path(program_root).expanduser().resolve()
    locations = [Path(shared_root), program, program / 'AppData']
    # Bounded installation ancestry, never recursively search drives or backups.
    for parent in list(program.parents)[:5]:
        locations.extend([parent, parent / 'AppData'])
    candidates, seen = [], set()
    for location in locations:
        key = os.path.normcase(str(location.resolve()))
        if key in seen:
            continue
        seen.add(key)
        if not (location / 'data/censor.db').is_file():
            continue
        info = inspect_root(location)
        if info['scans'] or info['labels'] or info['history']:
            candidates.append(info)
    return sorted(candidates, key=lambda row: (row['labels'], row['scans']), reverse=True)


def remember_profile(root, registry, *, allow_new=False):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        if not allow_new:
            raise ProfileError('资料库目录不存在；未创建新的空资料库：' + str(root))
        root.mkdir(parents=True, exist_ok=True)
    if not (root / 'data/censor.db').is_file():
        if not allow_new:
            raise ProfileError('资料库数据库缺失，未登记空目录：' + str(root))
        # Finish fresh database creation before publishing the pointer. Even if
        # the first-used DB is lost before a second launch, startup must refuse.
        from censor.store import VerdictStore
        VerdictStore(root / 'data/censor.db')
    info = inspect_root(root)
    path = Path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dict(schema_version=1, root=str(root), database_required=info['database_exists'],
                   registered_at=datetime.now(timezone.utc).isoformat())
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(content, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return info


def prepare_profile(*, default_root, registry, program_root, shared_root,
                    explicit=False, portable=False, choose=None):
    if explicit or portable:
        # A command-line/portable sandbox must never alter the normal profile.
        return Path(default_root).expanduser().resolve()
    saved = read_profile(registry)
    if saved:
        root = Path(saved['root']).resolve()
        if not root.is_dir() or not (root / 'data/censor.db').is_file():
            raise ProfileError('已登记资料库不可访问或数据库缺失，请恢复路径；未新建空库：' + str(root))
        info = inspect_root(root)
        if info['database_exists'] and not saved.get('database_required'):
            remember_profile(root, registry)
        return root
    candidates = discover_roots(program_root, shared_root)
    if len(candidates) > 1:
        if choose is None:
            raise ProfileChoiceRequired(candidates)
        selected = choose(candidates)
        if selected is None:
            raise ProfileError('已取消选择资料库；未更改任何数据库')
        root = Path(selected).expanduser().resolve()
        if root not in {row['root'] for row in candidates}:
            raise ProfileError('所选资料库不在已核验清单中')
    elif candidates:
        root = candidates[0]['root']
    else:
        # Preserve a source user's settings even before their first scan.
        old = Path(default_root).expanduser().resolve()
        root = old if (old / 'config.json').is_file() else Path(shared_root).expanduser().resolve()
    remember_profile(root, registry, allow_new=True)
    return root
