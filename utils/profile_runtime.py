"""Explicit per-install profile selection and a movable profile identity."""
from pathlib import Path
import json
import os
import uuid
from datetime import datetime, timezone

from utils.data_profile import ProfileError, inspect_root

MANIFEST = 'profile.json'
POINTER = 'data-profile.json'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_local_pointer(program_root):
    path = Path(program_root) / POINTER
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('root'), str)
                or not value['root'].strip()):
            raise ValueError()
        return (path.parent / value['root']).expanduser().resolve()
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError('程序目录内资料库指针无效，请重新选择资料库：' + str(path)) from exc


def write_local_pointer(program_root, root):
    program = Path(program_root).resolve()
    root = Path(root).resolve()
    info = inspect_root(root)
    if not info['database_exists']:
        raise ProfileError('所选已有资料库数据库缺失：' + str(root))
    try:
        reference = Path(os.path.relpath(root, program)).as_posix()
    except ValueError:
        reference = str(root)
    atomic_json(program / POINTER, dict(schema_version=1, root=reference, database_required=True))


def read_manifest(root):
    path = Path(root) / MANIFEST
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or not isinstance(value.get('profile_id'), str)
                or not value['profile_id'] or not isinstance(value.get('last_root'), str)
                or not Path(value['last_root']).is_absolute()):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise ProfileError('资料库身份文件无效或结构不受支持：' + str(path)) from exc


def check_profile(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ProfileError('资料库目录不存在，未新建空库：' + str(root))
    info = inspect_root(root)
    if not info['database_exists']:
        raise ProfileError('资料库数据库缺失，未新建空库：' + str(root))
    read_manifest(root)
    # Validate the second versioned component before any service can migrate it.
    import sqlite3
    from contextlib import closing
    snapshot = root / 'data/jellyfin_review.sqlite3'
    if snapshot.is_file():
        try:
            with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)) as db:
                from utils.user_state_store import UserStateStore
                UserStateStore._check_version(db)
        except (ValueError, sqlite3.Error) as exc:
            raise ProfileError('用户状态库结构不受支持或文件损坏：' + str(snapshot)) from exc
    return info


def open_profile(root, *, create=False):
    root = Path(root).expanduser().resolve()
    manifest = read_manifest(root)
    if not (root / 'data/censor.db').is_file():
        if manifest or not create:
            raise ProfileError('资料库数据库缺失，未新建空库：' + str(root))
        root.mkdir(parents=True, exist_ok=True)
        from censor.store import VerdictStore
        VerdictStore(root / 'data/censor.db')
    check_profile(root)
    if manifest and Path(manifest['last_root']).resolve() != root:
        from utils.profile_migration import rebase_profile_references
        rebase_profile_references(root, Path(manifest['last_root']), root)
    manifest = manifest or dict(schema_version=1, profile_id=uuid.uuid4().hex,
                                created_at=datetime.now(timezone.utc).isoformat())
    manifest['last_root'] = str(root)
    if read_manifest(root) != manifest:
        atomic_json(root / MANIFEST, manifest)
    return root


def startup_selection(program_root, *, explicit_root=None, create=False, legacy_registry=None, choose=None):
    """Return (root, may_create); global registration is only a first-use choice."""
    program = Path(program_root).resolve()
    selected = Path(explicit_root).expanduser().resolve() if explicit_root else read_local_pointer(program)
    if selected is not None:
        if not create:
            check_profile(selected)
        return selected, create
    if (program / MANIFEST).exists():
        check_profile(program)
        return program, False
    candidates = []
    for path in (program, program / 'AppData'):
        if (path / 'data/censor.db').is_file():
            candidates.append(inspect_root(path))
    if legacy_registry and Path(legacy_registry).exists() and not create:
        from utils.data_profile import read_profile
        legacy = read_profile(legacy_registry)
        if legacy:
            old = Path(legacy['root']).resolve()
            if old != program and old not in {r['root'] for r in candidates}:
                candidates.append(check_profile(old))
    # Any external legacy candidate requires an explicit one-time choice.
    if not create and any(r['root'] != program for r in candidates):
        if choose is None:
            from utils.data_profile import ProfileChoiceRequired
            raise ProfileChoiceRequired(candidates)
        chosen = choose(candidates)
        if chosen is None:
            raise ProfileError('已取消旧资料库选择，未新建空库')
        selected = Path(chosen).resolve()
        if selected not in {r['root'] for r in candidates}:
            raise ProfileError('选择不在已校验资料库清单中')
        return selected, False
    return program, not (program / 'data/censor.db').is_file()
