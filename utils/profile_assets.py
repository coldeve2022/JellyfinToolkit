"""Portable paths for generated evidence; media paths are never interpreted here."""
from pathlib import Path
import os


def store_asset(value, data_dir):
    if not value:
        return ''
    path = Path(value)
    root = Path(os.path.abspath(data_dir))
    if path.is_absolute():
        try:
            relative = Path(os.path.normpath(path)).relative_to(root / 'censor_thumbs')
        except ValueError:
            return str(value)
        return (Path('censor_thumbs') / relative).as_posix()
    return str(value)


def resolve_asset(value, data_dir):
    if not value:
        return ''
    path = Path(value)
    if path.is_absolute():
        return str(value)
    parts = str(value).replace('\\', '/').split('/')
    if '..' in parts:
        return ''
    if parts[0] != 'censor_thumbs':
        # Preserve opaque legacy relative names. Only the new owned-path
        # namespace is interpreted against the data directory; never erase old
        # evidence metadata merely because it predates that namespace.
        return str(value)
    root = Path(os.path.abspath(data_dir))
    resolved = Path(os.path.normpath(root / path))
    try:
        resolved.relative_to(root / 'censor_thumbs')
    except ValueError:
        # Never resolve an arbitrary stored relative path against process cwd.
        return ''
    return str(resolved)
