"""Verified, non-destructive profile copy. Publication never overwrites a target."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid

from utils.data_profile import ProfileError
from utils.profile_assets import store_asset
from utils.profile_runtime import atomic_json, check_profile


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _inside(path, parent):
    try:
        return path.relative_to(parent)
    except ValueError:
        return None


def _rebase(value, old_root, new_root, key=''):
    if isinstance(value, list):
        return [_rebase(x, old_root, new_root, key) for x in value]
    if isinstance(value, dict):
        return {_rebase(k, old_root, new_root): _rebase(v, old_root, new_root, str(k))
                for k, v in value.items()}
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return value
    # Classify stored references lexically. Resolving an unrelated NAS/media
    # path here would contact its filesystem for every JSON value in the cache.
    path = Path(os.path.normpath(value))
    relative = _inside(path, old_root)
    if relative is None or not relative.parts or relative.parts[0] not in ('data', 'backups'):
        return value
    if key in ('thumb_path', 'crop_path'):
        return store_asset(value, old_root / 'data')
    return str(new_root / relative)


def _rewrite_text(value, old_root, new_root):
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return _rebase(value, old_root, new_root)
    revised = _rebase(parsed, old_root, new_root)
    return json.dumps(revised, ensure_ascii=False) if revised != parsed else value


def rebase_profile_references(root, old_root, new_root):
    """Rewrite exact owned paths/JSON values, never arbitrary substrings or media paths."""
    root, old_root, new_root = (Path(p).resolve() for p in (root, old_root, new_root))
    rewritten = assets = 0
    config_path = root / 'config.json'
    if config_path.is_file():
        original = config_path.read_text(encoding='utf-8')
        revised = _rewrite_text(original, old_root, new_root)
        if revised != original:
            atomic_json(config_path, json.loads(revised))
            rewritten += 1
    for path in sorted((root / 'data').glob('*')):
        if not path.is_file() or path.suffix.lower() not in ('.db', '.sqlite', '.sqlite3', '.json'):
            continue
        if path.suffix.lower() == '.json':
            original = path.read_text(encoding='utf-8')
            revised = _rewrite_text(original, old_root, new_root)
            if revised != original:
                atomic_json(path, json.loads(revised))
                rewritten += 1
            continue
        with closing(sqlite3.connect(path)) as db, db:
            # All data tables, including operation journals. Backups remain immutable.
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                columns = db.execute('PRAGMA table_info(' + _quoted(table) + ')').fetchall()
                names = [col[1] for col in columns]
                pks = [col[1] for col in sorted(columns, key=lambda c: c[5]) if col[5]]
                if not pks:
                    pks = ['rowid']
                select = ','.join(_quoted(k) for k in pks + names)
                rows = db.execute('SELECT ' + select + ' FROM ' + _quoted(table)).fetchall()
                for row in rows:
                    values = list(row[len(pks):])
                    revised = [_rewrite_text(v, old_root, new_root) for v in values]
                    changes = [(name, new) for name, old, new in zip(names, values, revised) if new != old]
                    if not changes:
                        continue
                    if table == 'censor_probe' and 'candidates' in names:
                        index = names.index('candidates')
                        try:
                            before, after = json.loads(values[index]), json.loads(revised[index])
                            assets += sum(a.get(k) != b.get(k) for a, b in zip(before, after)
                                          for k in ('thumb_path', 'crop_path'))
                        except (ValueError, TypeError, AttributeError):
                            pass
                    db.execute('UPDATE ' + _quoted(table) + ' SET ' + ','.join(_quoted(k) + '=?' for k, _ in changes)
                               + ' WHERE ' + ' AND '.join(_quoted(k) + ' IS ?' for k in pks),
                               [v for _, v in changes] + list(row[:len(pks)]))
                    rewritten += len(changes)
    for path in sorted((root / 'data/media_upgrades').rglob('*.json')):
        original = path.read_text(encoding='utf-8')
        revised = _rewrite_text(original, old_root, new_root)
        if revised != original:
            atomic_json(path, json.loads(revised))
            rewritten += 1
    return dict(rewritten_values=rewritten, rewritten_assets=assets)


def _sqlite(path):
    if not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\0'


def database_counts(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        result = db.execute('PRAGMA integrity_check').fetchall()
        if result != [('ok',)]:
            raise ProfileError('数据库完整性校验失败：' + str(path))
        return {name: db.execute('SELECT count(*) FROM ' + _quoted(name)).fetchone()[0]
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _wal_state(databases):
    state = {}
    for database in databases:
        path = Path(str(database) + '-wal')
        # An absent/empty WAL has no committed frames. Ignore SHM: readers may
        # change its bookkeeping without changing application data.
        state[database] = (path.stat().st_size, _hash(path)) if path.is_file() and path.stat().st_size else None
    return state


def _source_files(source):
    files = []
    for name in ('config.json', 'data', 'backups'):
        entry = source / name
        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
            raise ProfileError('资料库含符号链接，拒绝不明确的复制：' + str(entry))
        if entry.is_file():
            files.append(entry)
        elif entry.is_dir():
            for base, dirs, names in os.walk(entry, followlinks=False):
                for child in dirs + names:
                    checked = Path(base) / child
                    if checked.is_symlink() or (hasattr(checked, 'is_junction') and checked.is_junction()):
                        raise ProfileError('资料库含符号链接，拒绝不明确的复制')
                files.extend(Path(base) / name for name in names)
    return sorted(files)


def migrate_profile(source, target, *, progress=None):
    from utils.profile_lock import ProfileLock
    # New application versions and this migrator cooperate on the same lock.
    # WAL/fingerprint validation additionally detects writes by legacy versions.
    with ProfileLock(source):
        return _migrate_profile_locked(source, target, progress=progress)


def _migrate_profile_locked(source, target, *, progress=None):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or _inside(target, source) is not None or _inside(source, target) is not None:
        raise ProfileError('迁移源和目标不能相同或互相包含')
    if target.exists():
        raise FileExistsError('目标已存在，未覆盖：' + str(target))
    check_profile(source)
    files = _source_files(source)
    databases = {p for p in files if _sqlite(p)}
    ignored = {Path(str(db) + suffix) for db in databases for suffix in ('-wal', '-shm', '-journal')}
    files = [p for p in files if p not in ignored and p.name != '.profile.lock']
    fingerprint = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
    wal_before = _wal_state(databases)
    required = sum(v[0] for v in fingerprint.values())
    target.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(target.parent).free < required + 256 * 1024 * 1024:
        raise ProfileError('目标磁盘可用空间不足')
    staging = target.with_name(target.name + '.migrating-' + uuid.uuid4().hex)
    staging.mkdir()
    report = dict(source=str(source), target=str(target), started_at=datetime.now(timezone.utc).isoformat(),
                  tables={}, files={}, copied_bytes=0, copied_files=0, source_preserved=True)
    last_progress = 0.
    try:
        for path in files:
            relative = path.relative_to(source)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path in databases:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as src:
                    with closing(sqlite3.connect(destination)) as dst:
                        src.backup(dst)
                if len(relative.parts) == 2 and relative.parts[0] == 'data':
                    report['tables'][path.name] = database_counts(path)
                    if database_counts(destination) != report['tables'][path.name]:
                        raise ProfileError('数据库复制记录数量不一致：' + path.name)
                digest = _hash(destination)
            else:
                digestor = hashlib.sha256()
                with path.open('rb') as src, destination.open('xb') as dst:
                    current_bytes = 0
                    for block in iter(lambda: src.read(8 * 1024 * 1024), b''):
                        dst.write(block)
                        digestor.update(block)
                        current_bytes += len(block)
                        if progress and time.monotonic() - last_progress >= 5:
                            progress(dict(phase='copy', files=report['copied_files'],
                                          bytes=report['copied_bytes'] + current_bytes, total_bytes=required))
                            last_progress = time.monotonic()
                shutil.copystat(path, destination)
                digest = digestor.hexdigest()
                if _hash(destination) != digest:
                    raise ProfileError('文件复制校验失败')
            report['files'][relative.as_posix()] = digest
            report['copied_files'] += 1
            report['copied_bytes'] += destination.stat().st_size
            if progress and time.monotonic() - last_progress >= 5:
                progress(dict(phase='copy', files=report['copied_files'], bytes=report['copied_bytes'], total_bytes=required))
                last_progress = time.monotonic()
        report.update(rebase_profile_references(staging, source, target))
        if (staging / 'config.json').is_file():
            report['files']['config.json'] = _hash(staging / 'config.json')
        for path in [*(staging / 'data').glob('*.json'), *(staging / 'data/media_upgrades').rglob('*.json')]:
            report['files'][path.relative_to(staging).as_posix()] = _hash(path)
        for name, counts in report['tables'].items():
            path = staging / 'data' / name
            if database_counts(path) != counts:
                raise ProfileError('引用迁移后记录数量不一致：' + name)
            report['files']['data/' + name] = _hash(path)
        if any(not p.is_file() or (p.stat().st_size, p.stat().st_mtime_ns) != stat for p, stat in fingerprint.items()):
            raise ProfileError('迁移期间源资料库发生变化；未切换，临时副本保留')
        if _wal_state(databases) != wal_before:
            raise ProfileError('迁移期间源资料库 WAL 发生变化；未切换，临时副本保留')
        remaining = set(p for p in _source_files(source) if p not in ignored and p.name != '.profile.lock')
        if remaining != set(files):
            raise ProfileError('迁移期间源资料库文件清单变化；未切换')
        atomic_json(staging / 'profile.json', dict(schema_version=1, profile_id=uuid.uuid4().hex,
                    created_at=datetime.now(timezone.utc).isoformat(), last_root=str(target)))
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(staging / 'migration-report.json', report)
        # rename refuses an existing destination on Windows. Explicit check also
        # protects portable POSIX use when another process created an empty dir.
        if target.exists():
            raise FileExistsError(str(target))
        staging.rename(target)
        return {k: v for k, v in report.items() if k != 'files'}
    except BaseException:
        # Keep the failed copy inspectable; never recursively delete user data.
        if progress:
            progress(dict(phase='failed', staging=str(staging)))
        raise
