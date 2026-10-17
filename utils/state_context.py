"""Pure state identity and scoped SQLite storage. No Qt, credentials, or implicit migration."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


def _get(cfg, key, default=""):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


def _source_signature(cfg):
    return [_get(cfg, key) for key in ("library_source", "jellyfin_server", "jellyfin_db_path")]


def _configured_source(cfg):
    from utils.media_server import server_identity
    if isinstance(cfg, (str, Path)):
        return "db:" + os.path.normcase(str(Path(cfg).resolve()))
    binding = _get(cfg, "_resolved_source_binding", None)
    if binding and binding.get("signature") == _source_signature(cfg):
        return binding["identity"]
    server = server_identity(_get(cfg, "jellyfin_server"))
    db = str(_get(cfg, "jellyfin_db_path") or "")
    mode = str(_get(cfg, "library_source", "auto"))
    if mode == "db" or mode == "auto" and db and Path(db).is_file():
        return "db:" + os.path.normcase(str(Path(db).resolve())) if db else "db:auto"
    return "server:" + server if server else "source:unknown"


def resolved_identity(items, cfg):
    """Return actual collection identity; no synchronous server query or UI mutation."""
    users = {str(getattr(it, "behavior_user_id", "") or "") for it in items}
    users.discard("")
    sources = {str(getattr(it, "source_identity", "") or "") for it in items}
    sources.discard("")
    configured_user = str(_get(cfg, "jellyfin_user_id") or "")
    user = next(iter(users)) if len(users) == 1 else configured_user
    source = next(iter(sources)) if len(sources) == 1 else _configured_source(cfg)
    if len(users) > 1 or len(sources) > 1:
        raise ValueError("采集结果包含多个观看用户或库来源，已拒绝混合画像")
    if configured_user and user and configured_user.replace("-", "").lower() != user.replace("-", "").lower():
        raise ValueError("采集观看用户与任务配置不符，请重新采集")
    return user, source


def _viewer_identity(cfg, items=None):
    user, source = resolved_identity(items, cfg) if items else (
        str(_get(cfg, "jellyfin_user_id") or ""), _configured_source(cfg))
    user = user.replace("-", "").lower()
    return [source, user]


def source_viewer_context(cfg, items=None):
    """Previous namespace for explicit feedback migration, never AI fallback."""
    return hashlib.sha256(json.dumps(_viewer_identity(cfg, items), ensure_ascii=False).encode()).hexdigest()


def state_context(cfg, items=None) -> str:
    """Stable source/viewer/selection namespace; no network or credential serialization."""
    from utils.library_scope import normalize_scope_path

    ids = sorted({str(value) for value in (_get(cfg, "library_scope_ids", []) or [])})
    paths = sorted({normalize_scope_path(value) for value in (_get(cfg, "library_scope_paths", []) or [])})
    identity = _viewer_identity(cfg, items)
    if ids or paths:
        identity.append({"library_ids": ids, "paths": paths})
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def bind_resolved_context(cfg, snapshot):
    """Main-thread binding; the UI persists an unambiguously resolved watching user."""
    user = str(snapshot.get("resolved_user_id") or "")
    existing = str(_get(cfg, "jellyfin_user_id") or "")
    if user and user.replace("-", "").lower() != existing.replace("-", "").lower():
        if isinstance(cfg, dict):
            cfg["jellyfin_user_id"] = user
        else:
            cfg.jellyfin_user_id = user
    source = snapshot.get("resolved_source_identity")
    if source:
        binding = {"identity": source, "signature": _source_signature(cfg)}
        if isinstance(cfg, dict):
            cfg["_resolved_source_binding"] = binding
        else:
            cfg._resolved_source_binding = binding
    return state_context(cfg)


def score_fingerprint(items, labels, cfg) -> str:
    """Invalidate semantic answers whenever the evidence, model, or feedback changes."""
    records = []
    for it in items:
        record = {key: getattr(it, key, None) for key in (
            "id", "name", "path", "source_path", "year", "genres", "tags", "studios", "actors",
            "size", "num", "is_cd", "cd_index", "bucket", "is_uncensored", "is_favorite",
            "play_count", "is_played", "has_progress", "rating", "behavior_known",
            "width", "height", "duration", "bitrate", "behavior_user_id", "source_identity",
            "library_ids", "library_names")}
        try:
            stat = Path(it.path).stat()
            record["file"] = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            record["file"] = None
        records.append(record)
    records.sort(key=lambda r: str(r["id"]))
    payload = {"context": state_context(cfg, items), "items": records, "labels": labels,
               "model": _get(cfg, "ai_model"), "endpoint": _get(cfg, "ai_base_url"),
               "engine": _get(cfg, "engine", "v2"), "version": 1}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class ScopedState:
    """Explicit namespace table; legacy tables are left intact and never guessed into a user."""

    def __init__(self, path, kind, context):
        self.path, self.kind, self.context = Path(path), kind, str(context)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS scoped_state(
                context TEXT NOT NULL, kind TEXT NOT NULL, k TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(context,kind,k))""")

    def _conn(self):
        return sqlite3.connect(str(self.path), timeout=15)

    def set(self, key, value):
        with closing(self._conn()) as conn, conn:
            conn.execute("""INSERT INTO scoped_state VALUES(?,?,?,?,?)
                ON CONFLICT(context,kind,k) DO UPDATE SET payload=excluded.payload,
                updated_at=excluded.updated_at""",
                (self.context, self.kind, key, json.dumps(value, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")))

    def all(self):
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT k,payload FROM scoped_state WHERE context=? AND kind=?",
                                (self.context, self.kind)).fetchall()
        return {key: json.loads(payload) for key, payload in rows}

    def set_if_absent(self, key, value):
        with closing(self._conn()) as conn, conn:
            cursor = conn.execute("INSERT OR IGNORE INTO scoped_state VALUES(?,?,?,?,?)",
                                  (self.context, self.kind, key, json.dumps(value, ensure_ascii=False),
                                   datetime.now().isoformat(timespec="seconds")))
            return cursor.rowcount == 1

    def get(self, key):
        with closing(self._conn()) as conn:
            row = conn.execute("SELECT payload FROM scoped_state WHERE context=? AND kind=? AND k=?",
                               (self.context, self.kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def has_any(self):
        with closing(self._conn()) as conn:
            return conn.execute("SELECT 1 FROM scoped_state WHERE context=? AND kind=? LIMIT 1",
                                (self.context, self.kind)).fetchone() is not None

    def remove(self, key):
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM scoped_state WHERE context=? AND kind=? AND k=?",
                         (self.context, self.kind, key))

    def clear(self):
        with closing(self._conn()) as conn, conn:
            conn.execute("DELETE FROM scoped_state WHERE context=? AND kind=?", (self.context, self.kind))


def clear_ai_state(path, context=""):
    """Remove both AI feature suggestions and semantic answers, preserving manual feedback."""
    from utils.ai_profile import AIProfileStore
    from utils.ai_scoring import AIScoreStore
    AIProfileStore(path, context=context).clear()
    AIScoreStore(path, context=context).clear()
    if not context:
        ScopedState(path, "ai_profile", "").clear()
        ScopedState(path, "ai_score", "").clear()
