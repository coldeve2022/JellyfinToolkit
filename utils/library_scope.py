"""Explicit media library boundaries. All paths refer to the original server namespace."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
import xml.etree.ElementTree as ET
from pathlib import Path

from utils.library import _check_cancel, _guid_str, _readonly_conn


def normalize_scope_path(value):
    """Canonical server path, without resolving on this machine; traversal is invalid."""
    path = str(value or '').strip().replace('\\', '/')
    if not path or '\x00' in path or '..' in path.split('/'):
        return ''
    windows = bool(re.match(r'^[A-Za-z]:/', path) or path.startswith('//'))
    if not windows and not path.startswith('/'):
        return ''
    head = '//' if path.startswith('//') else ('/' if path.startswith('/') else '')
    path = head + '/'.join(part for part in path.split('/') if part and part != '.')
    path = path.rstrip('/') or '/'
    if re.fullmatch(r'[A-Za-z]:', path):
        path += '/'
    return path.casefold() if windows else path


def path_in_scope(path, root):
    path, root = normalize_scope_path(path), normalize_scope_path(root)
    return bool(path and root and (path == root or path.startswith(root.rstrip('/') + '/')))


def scope_id(source, native_id):
    return 'library:' + hashlib.sha256((source + '\0' + str(native_id)).encode('utf-8')).hexdigest()[:32]


def _paths(value):
    if isinstance(value, (bytes, str)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            value = [value.decode('utf-8') if isinstance(value, bytes) else value]
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for entry in value:
        if isinstance(entry, dict):
            entry = entry.get('Path') or entry.get('path') or ''
        if not isinstance(entry, str):
            continue
        norm = normalize_scope_path(entry)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(entry)
    return result


def discover_db_scopes(db_path, data_dir='', should_stop=None):
    """Read CollectionFolder metadata and server-owned options.xml, never enumerate media."""
    _check_cancel(should_stop)
    path = Path(db_path)
    source = 'db:' + normalize_scope_path(str(path.resolve()))
    descriptors = []
    with closing(_readonly_conn(str(path))) as conn:
        conn.execute('PRAGMA query_only=ON')
        tables = {r[0].lower(): r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        table = tables.get('baseitems') or tables.get('typedbaseitems')
        if table:
            cols = {r[1].lower(): r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            type_col = cols.get('type')
            wanted = [c for c in ('id', 'guid', 'name', 'path', 'physicallocations', 'data') if c in cols]
            if type_col:
                query = 'SELECT ' + ','.join('"' + cols[c] + '"' for c in wanted) + f' FROM "{table}" WHERE "{type_col}" LIKE ?'
                for row in conn.execute(query, ('%CollectionFolder',)):
                    _check_cancel(should_stop)
                    rec = dict(zip(wanted, row))
                    payload = {}
                    try:
                        payload = json.loads(rec.get('data') or '{}')
                    except (ValueError, TypeError, UnicodeError):
                        pass
                    if not isinstance(payload, dict):
                        payload = {}
                    roots = (_paths(rec.get('physicallocations')) or _paths(payload.get('PhysicalLocations'))
                             or _paths(payload.get('PhysicalLocationsList')) or _paths(payload.get('Locations')))
                    virtual_folder = normalize_scope_path(rec.get('path'))
                    roots = [root for root in roots if normalize_scope_path(root) != virtual_folder]
                    descriptors.append({'native': _guid_str(rec.get('id') or rec.get('guid')), 'name': str(rec.get('name') or payload.get('Name') or ''),
                                        'folder': str(rec.get('path') or ''), 'paths': roots})
    # Jellyfin 10.11 stores library paths in root/default/<name>/options.xml.
    candidates = [path.parent.parent / 'root' / 'default']
    if data_dir:
        candidates.insert(0, Path(data_dir) / 'root' / 'default')
    options = {}
    for directory in dict.fromkeys(candidates):
        if not directory.is_dir():
            continue
        for filename in sorted(directory.glob('*/options.xml')):
            _check_cancel(should_stop)
            try:
                tree = ET.parse(filename)
                roots = _paths([node.text or '' for node in tree.findall('./PathInfos/MediaPathInfo/Path')])
            except (OSError, ET.ParseError):
                continue
            if roots:
                options[filename.parent.name] = {'name': filename.parent.name, 'paths': roots, 'folder': str(filename.parent)}
    for desc in descriptors:
        if not desc['paths'] and desc['name'] in options:
            desc['paths'] = options[desc['name']]['paths']
    represented = {desc['name'] for desc in descriptors}
    for name, desc in options.items():
        if name not in represented:
            descriptors.append(dict(desc, native='folder:' + normalize_scope_path(desc['folder'])))
    return [{'id': scope_id(source, desc['native'] or 'folder:' + normalize_scope_path(desc['folder'])),
             'name': desc['name'] or '未命名媒体库', 'paths': desc['paths']}
            for desc in descriptors if desc['paths']]


def _annotate_extra_paths(item, path, roots):
    for root in roots:
        if path_in_scope(path, root):
            identity = scope_id(str(item.source_identity or ''), 'path:' + normalize_scope_path(root))
            if identity not in item.library_ids:
                item.library_ids.append(identity)
                item.library_names.append(root)


def filter_scopes(items, catalog, selected_ids, extra_paths, should_stop=None):
    """Union selected libraries and explicit directories. Caller validates missing ids."""
    selected = set(selected_ids)
    extra = list(extra_paths)
    scoped = bool(selected or extra)
    result = []
    for it in items:
        _check_cancel(should_stop)
        path = it.source_path or it.path
        memberships = [scope for scope in catalog if any(path_in_scope(path, root) for root in scope['paths'])]
        it.library_ids = [scope['id'] for scope in memberships]
        it.library_names = [scope['name'] for scope in memberships]
        _annotate_extra_paths(it, path, extra)
        if not scoped or selected.intersection(it.library_ids) or any(path_in_scope(path, root) for root in extra):
            result.append(it)
    return _retain_collection_stats(items, result)


def _retain_collection_stats(original, result):
    inherited = getattr(original, 'collection_stats', None)
    if inherited is None:
        return result
    from utils.media_filter import LibraryItems
    stats = dict(inherited)
    stats['excluded_scope'] = stats.get('excluded_scope', 0) + len(original) - len(result)
    return LibraryItems(result, stats)


def apply_scope(items, cfg, catalog=None, should_stop=None):
    """Recheck an in-memory collection before scoring. Performs no I/O.

    Existing item memberships are authoritative from collection. A supplied catalog
    may add path evidence, but never causes an unselected item to pass by default.
    """
    get = cfg.get if isinstance(cfg, dict) else lambda key, default=None: getattr(cfg, key, default)
    selected = set(get('library_scope_ids', []) or [])
    roots = get('library_scope_paths', []) or []
    if not selected and not roots:
        return items
    result = []
    for it in items:
        _check_cancel(should_stop)
        path = it.source_path or it.path
        _annotate_extra_paths(it, path, roots)
        memberships = set(it.library_ids)
        for scope in catalog or []:
            if any(path_in_scope(path, root) for root in scope.get('paths', [])):
                memberships.add(scope['id'])
        if memberships.intersection(selected) or any(path_in_scope(path, root) for root in roots):
            result.append(it)
    return _retain_collection_stats(items, result)
