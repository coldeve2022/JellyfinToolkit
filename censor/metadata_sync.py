"""Durable, version-checked manual confirmation metadata synchronization.

Each endpoint has its own status. Receipts contain only owned tag deltas and
target identities; credentials and remote exception text never enter this DB.
An intent is committed before side effects so interrupted writes are retryable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
import xml.etree.ElementTree as ET

from censor.identity import identify_file
from censor.models import CensorState


_STATES = ("pending", "done", "failed", "waiting", "stale", "skipped")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class _Stale(Exception):
    pass


class _Stopped(Exception):
    pass


class _SyncError(Exception):
    """Only these fixed, credential-free messages may be persisted."""


def _get(cfg, key, default=None):
    return cfg.get(key, default) if isinstance(cfg, dict) else getattr(cfg, key, default)


class MetadataSyncService:
    def __init__(self, cfg, verdict_store):
        self.cfg, self.store = cfg, verdict_store
        self.db_path = Path(verdict_store.db_path)
        with _LOCKS_GUARD:
            self._run_lock = _LOCKS.setdefault(str(self.db_path.resolve()), threading.RLock())
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS censor_metadata_sync (
                  path TEXT PRIMARY KEY, file_identity TEXT NOT NULL, revision INTEGER NOT NULL,
                  desired TEXT, nfo TEXT NOT NULL, jellyfin TEXT NOT NULL,
                  nfo_error TEXT NOT NULL DEFAULT '', jellyfin_error TEXT NOT NULL DEFAULT '',
                  nfo_fingerprint TEXT NOT NULL DEFAULT '', jellyfin_fingerprint TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS censor_metadata_receipt (
                  path TEXT NOT NULL, file_identity TEXT NOT NULL, endpoint TEXT NOT NULL,
                  target TEXT NOT NULL, baseline TEXT NOT NULL, managed TEXT NOT NULL,
                  added TEXT NOT NULL, removed TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                  attempted INTEGER NOT NULL DEFAULT 0, confirmed_added TEXT NOT NULL DEFAULT '[]',
                  PRIMARY KEY(path,file_identity,endpoint));
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(censor_metadata_receipt)")}
            if "applied" not in columns:
                db.execute("ALTER TABLE censor_metadata_receipt ADD COLUMN applied INTEGER NOT NULL DEFAULT 0")
            for name in ("nfo_fingerprint", "jellyfin_fingerprint"):
                job_columns = {r[1] for r in db.execute("PRAGMA table_info(censor_metadata_sync)")}
                if name not in job_columns:
                    db.execute(f"ALTER TABLE censor_metadata_sync ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if "attempted" not in columns:
                db.execute("ALTER TABLE censor_metadata_receipt ADD COLUMN attempted INTEGER NOT NULL DEFAULT 0")
            if "confirmed_added" not in columns:
                db.execute("ALTER TABLE censor_metadata_receipt ADD COLUMN confirmed_added TEXT NOT NULL DEFAULT '[]'")
                # Old receipts did not distinguish a sent request from a prepared
                # intent. Only a completed endpoint proves ownership after upgrade.
                for receipt in db.execute("SELECT * FROM censor_metadata_receipt").fetchall():
                    job = db.execute("SELECT * FROM censor_metadata_sync WHERE path=? AND file_identity=?",
                                     (receipt["path"], receipt["file_identity"])).fetchone()
                    known = bool(receipt["applied"] and job and job[receipt["endpoint"]] == "done")
                    db.execute("UPDATE censor_metadata_receipt SET confirmed_added=?,attempted=? "
                               "WHERE path=? AND file_identity=? AND endpoint=?",
                               (receipt["added"] if known else "[]",
                                int(not known and (receipt["applied"] or receipt["endpoint"] == "jellyfin")),
                                receipt["path"], receipt["file_identity"], receipt["endpoint"]))

    def _fingerprint(self, endpoint):
        """Persist only a digest, including credentials so changing them permits retry."""
        payload = {key: _get(self.cfg, key, default) for key, default in (
            ("censor_sync_" + endpoint, True),
            ("censor_sync_tag_censored", "有码"),
            ("censor_sync_tag_uncensored", "无码"))}
        keys = (("censor_sync_create_nfo", True), ("video_extensions", [])) if endpoint == "nfo" else (
            ("jellyfin_server", ""), ("jellyfin_api_key", ""),
            ("library_path_mappings", []), ("library_scope_ids", []), ("library_scope_paths", []))
        payload.update({key: _get(self.cfg, key, default) for key, default in keys})
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, path, file_identity, state):
        """Return a persistent revision; UNKNOWN and unversioned input are ignored."""
        if state is not None:
            try:
                state = CensorState(state)
            except (ValueError, TypeError):
                return 0
            if state == CensorState.UNKNOWN:
                return 0
        identity = file_identity.serialize() if hasattr(file_identity, "serialize") else str(file_identity or "")
        if not identity:
            return 0
        desired = state.value if state else None
        fingerprints = {ep: self._fingerprint(ep) for ep in ("nfo", "jellyfin")}
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM censor_metadata_sync WHERE path=?", (str(path),)).fetchone()
            same_intent = bool(old and old["file_identity"] == identity and old["desired"] == desired)
            if same_intent and all(old[ep + "_fingerprint"] == fingerprints[ep] for ep in fingerprints):
                return old["revision"]
            revision = old["revision"] + 1 if old else 1
            statuses, errors = [], []
            for ep in ("nfo", "jellyfin"):
                unchanged = same_intent and old[ep + "_fingerprint"] == fingerprints[ep]
                statuses.append(old[ep] if unchanged else
                                "pending" if _get(self.cfg, "censor_sync_" + ep, True) else "skipped")
                errors.append(old[ep + "_error"] if unchanged else "")
            db.execute("INSERT OR REPLACE INTO censor_metadata_sync "
                       "(path,file_identity,revision,desired,nfo,jellyfin,nfo_error,jellyfin_error,"
                       "nfo_fingerprint,jellyfin_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (str(path), identity, revision, desired, *statuses, *errors,
                        fingerprints["nfo"], fingerprints["jellyfin"]))
        return revision

    def records(self):
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM censor_metadata_sync ORDER BY path")]

    def status(self):
        counts = dict.fromkeys(_STATES, 0)
        for row in self.records():
            for endpoint in ("nfo", "jellyfin"):
                counts[row[endpoint]] += 1
        return counts

    def enqueue_confirmed(self, paths=None):
        labels = self.store.labels()
        selected = set(map(str, paths)) if paths is not None else None
        count = 0
        for path in labels:
            if selected is not None and path not in selected:
                continue
            try:
                identity = identify_file(path).serialize()
                label = self.store.get_label(path, file_identity=identity)
            except OSError:
                continue
            if label and label[0] != CensorState.UNKNOWN:
                count += bool(self.enqueue(path, identity, label[0]))
        return count

    def _check(self, job, should_stop):
        if should_stop and should_stop():
            raise _Stopped()
        with self._connect() as db:
            current = db.execute("SELECT revision,file_identity FROM censor_metadata_sync WHERE path=?",
                                 (job["path"],)).fetchone()
        if not current or current["revision"] != job["revision"] or current["file_identity"] != job["file_identity"]:
            raise _Stale()
        try:
            identity = identify_file(job["path"]).serialize()
        except OSError:
            raise _Stale() from None
        if identity != job["file_identity"]:
            raise _Stale()
        label = self.store.get_label(job["path"], file_identity=identity)
        if job["desired"] is None:
            if label is not None:
                raise _Stale()
        elif not label or label[0].value != job["desired"]:
            raise _Stale()

    def _finish(self, job, endpoint, status, error=""):
        with self._connect() as db:
            if status == "done" and job["desired"] is not None:
                # Success belongs to this file version even if a newer confirmation
                # arrived while POST was in flight. Its job status still uses CAS.
                db.execute("UPDATE censor_metadata_receipt SET applied=1,attempted=0,confirmed_added=added "
                           "WHERE path=? AND file_identity=? AND endpoint=?",
                           (job["path"], job["file_identity"], endpoint))
            elif status == "done" and job["desired"] is None:
                db.execute("DELETE FROM censor_metadata_receipt WHERE path=? AND file_identity=? AND endpoint=?",
                           (job["path"], job["file_identity"], endpoint))
            elif status == "skipped" and job["desired"] is None:
                # A prepared intent that was never sent also ends its ownership
                # cycle. Disabled endpoints with actual writes retain their receipt.
                db.execute("DELETE FROM censor_metadata_receipt WHERE path=? AND file_identity=? AND endpoint=? "
                           "AND applied=0 AND attempted=0",
                           (job["path"], job["file_identity"], endpoint))
            db.execute(f"UPDATE censor_metadata_sync SET {endpoint}=?,{endpoint}_error=? "
                       "WHERE path=? AND file_identity=? AND revision=?",
                       (status, error, job["path"], job["file_identity"], job["revision"]))

    def _receipt(self, job, endpoint):
        with self._connect() as db:
            row = db.execute("SELECT * FROM censor_metadata_receipt WHERE path=? AND file_identity=? AND endpoint=?",
                             (job["path"], job["file_identity"], endpoint)).fetchone()
        if not row:
            return None
        row = dict(row)
        for key in ("baseline", "managed", "added", "removed", "confirmed_added"):
            row[key] = json.loads(row[key])
        return row

    def _mark_attempted(self, job, endpoint, should_stop):
        self._check(job, should_stop)
        if job[endpoint + "_fingerprint"] and job[endpoint + "_fingerprint"] != self._fingerprint(endpoint):
            raise _Stale()
        with self._connect() as db:
            db.execute("UPDATE censor_metadata_receipt SET attempted=1 "
                       "WHERE path=? AND file_identity=? AND endpoint=?",
                       (job["path"], job["file_identity"], endpoint))

    def _transform(self, job, endpoint, target, tags, should_stop):
        self._check(job, should_stop)
        if job[endpoint + "_fingerprint"] and job[endpoint + "_fingerprint"] != self._fingerprint(endpoint):
            raise _Stale()
        receipt = self._receipt(job, endpoint)
        if receipt and receipt["target"] != target:
            raise _SyncError("同步目标发生变化，已拒绝使用旧回执")
        pair = [str(_get(self.cfg, "censor_sync_tag_censored", "有码")).strip(),
                str(_get(self.cfg, "censor_sync_tag_uncensored", "无码")).strip()]
        if not all(pair) or pair[0] == pair[1]:
            raise _SyncError("同步标签配置无效")
        if job["desired"] is None:
            if not receipt:
                return list(tags)
            if receipt["attempted"]:
                unknown = set(receipt["added"]) - set(receipt["confirmed_added"])
                if unknown.intersection(tags):
                    raise _SyncError("上次元数据写入结果不明，现存标签归属需人工核验；已保留标签，核验移除后可重试撤回")
            elif not receipt["applied"]:
                return list(tags)
            result = [v for v in tags if v not in receipt["confirmed_added"]]
            result += [v for v in receipt["removed"] if v not in result]
            # Keep the original receipt until the endpoint acknowledges success.
            return result
        managed = list(dict.fromkeys((receipt["managed"] if receipt else []) + pair))
        baseline = receipt["baseline"] if receipt else [v for v in tags if v in managed]
        # On a later configuration change also remember preexisting new managed tags.
        if receipt:
            baseline += [v for v in tags if v in pair and v not in receipt["managed"] and v not in baseline]
        wanted = pair[0 if job["desired"] == "censored" else 1]
        result = [v for v in tags if v not in managed] + [wanted]
        added = list(dict.fromkeys((receipt["added"] if receipt else []) +
                                  ([wanted] if wanted not in baseline else [])))
        removed = list(baseline)
        # Durable intent: a crash or uncertain POST cannot lose the original delta.
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO censor_metadata_receipt "
                       "(path,file_identity,endpoint,target,baseline,managed,added,removed,applied,attempted,confirmed_added) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (job["path"], job["file_identity"], endpoint, target,
                        *[json.dumps(v, ensure_ascii=False) for v in (baseline, managed, added, removed)],
                        receipt["applied"] if receipt else 0,
                        receipt["attempted"] if receipt else 0,
                        json.dumps(receipt["confirmed_added"] if receipt else [], ensure_ascii=False)))
        return result

    def _nfo_target(self, job):
        video = Path(job["path"])
        same = video.with_suffix(".nfo")
        from utils.media_types import DEFAULT_VIDEO_EXTENSIONS
        suffixes = {"." + str(v).lower().lstrip(".") for v in
                    list(DEFAULT_VIDEO_EXTENSIONS) + list(_get(self.cfg, "video_extensions", []) or [])}
        videos = {str(p.resolve()) for p in video.parent.iterdir()
                  if p.is_file() and p.suffix.lower() in suffixes}
        same_stem = {str(p.resolve()) for p in video.parent.iterdir()
                     if p.is_file() and p.suffix.lower() in suffixes and p.stem.casefold() == video.stem.casefold()}
        if len(same_stem) != 1:
            raise _SyncError("同名 NFO 对应多个视频版本，归属不唯一，未修改")
        if same.exists():
            return same
        fallback = [p for p in (video.parent / "movie.nfo", video.parent / "tvshow.nfo") if p.exists()]
        if fallback:
            if len(fallback) != 1 or len(videos) != 1:
                raise _SyncError("共享 NFO 归属不唯一，未修改")
            return fallback[0]
        if not _get(self.cfg, "censor_sync_create_nfo", True):
            return None
        return same

    def _sync_nfo(self, job, should_stop):
        from utils import file_ops
        if job["desired"] is None:
            receipt = self._receipt(job, "nfo")
            if not receipt or not (receipt["applied"] or receipt["attempted"]):
                return "skipped"
        target = self._nfo_target(job)
        if target is None:
            return "skipped"
        existed = target.exists()
        raw = target.read_bytes() if existed else b"<movie />"
        try:
            root = ET.fromstring(raw, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        except ET.ParseError:
            # Some existing sidecars use GBK without an XML declaration. Use
            # the project's shared strict reader while retaining the raw-byte
            # digest for the optimistic write check and original backup.
            from utils.nfo import read_text_any_encoding
            text = read_text_any_encoding(str(target)) if existed else None
            if text is None:
                raise _SyncError('NFO 编码或 XML 无法解析，原件保留') from None
            root = ET.fromstring(text, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        if root.tag not in ("movie", "tvshow", "episodedetails"):
            raise _SyncError("NFO 根节点不支持")
        nodes = root.findall("tag")
        before = [node.text or "" for node in nodes]
        after = self._transform(job, "nfo", str(target.resolve()), before, should_stop)
        if before == after:
            return "done"
        for node in nodes:
            root.remove(node)
        for tag in after:
            ET.SubElement(root, "tag").text = tag
        self._check(job, should_stop)
        options = dict(journal=file_ops.OperationJournal(self.db_path.with_name("censor_metadata_operations.db")),
                       should_stop=lambda: self._obsolete(job, should_stop))
        content = ET.tostring(root, encoding="unicode")
        if existed:
            result = file_ops.safe_write_text(target, content,
                expected_target_hash=hashlib.sha256(raw).hexdigest(), **options)
        else:
            # Creation must explicitly refuse a concurrently created target.
            # safe_write_text's default overwrite policy is appropriate only for edits.
            import tempfile
            with tempfile.TemporaryDirectory(prefix="censor-nfo-") as directory:
                source = Path(directory) / target.name
                source.write_text(content, encoding="utf-8")
                result = file_ops.safe_replace(source, target, allow_overwrite=False, kind="text_edit", **options)
        if not result["ok"]:
            if result.get("state") == "cancelled":
                raise _Stopped()
            raise _SyncError("NFO 写入失败或文件已被外部修改；原件及恢复备份已保留")
        return "done"

    def _obsolete(self, job, should_stop):
        try:
            self._check(job, should_stop)
            return False
        except (_Stale, _Stopped):
            return True

    def _matches(self, local, remote):
        from utils.library_source import map_media_path
        from utils.library_scope import normalize_scope_path, path_in_scope
        mappings = _get(self.cfg, "library_path_mappings", []) or []
        applicable = [m for m in mappings if isinstance(m, dict)
                      and m.get("local") and path_in_scope(remote, m.get("remote", ""))]
        if applicable:
            longest = max(len(normalize_scope_path(m["remote"])) for m in applicable)
            outputs = {normalize_scope_path(map_media_path(remote, [m])) for m in applicable
                       if len(normalize_scope_path(m["remote"])) == longest}
            if len(outputs) != 1:
                raise _SyncError("同一服务器路径存在冲突映射，未修改")
        mapped = map_media_path(remote, mappings)
        return bool(normalize_scope_path(local) and normalize_scope_path(local) == normalize_scope_path(mapped))

    def _index(self, should_stop):
        """One fresh API index per batch; optional scope checked in server namespace."""
        from utils import jellyfin_api as api
        from utils.library_scope import path_in_scope, scope_id
        from utils.media_server import server_identity
        server, key = _get(self.cfg, "jellyfin_server", ""), _get(self.cfg, "jellyfin_api_key", "")
        selected, roots = _get(self.cfg, "library_scope_ids", []), _get(self.cfg, "library_scope_paths", [])
        if selected:
            from utils.library_api import list_virtual_folders
            catalog = list_virtual_folders(server, key, should_stop=should_stop)
            found = set()
            roots = list(roots or [])
            for folder in catalog:
                sid = scope_id("server:" + server_identity(server), folder["id"])
                if sid in selected:
                    found.add(sid)
                    roots.extend(folder["paths"])
            if found != set(selected):
                raise _SyncError("媒体库范围与当前 API 不匹配，请重新选择")
        result = []
        for page in api._iter_items(server, key, should_stop=should_stop):
            for item in page:
                if (not roots or any(path_in_scope(item.get("Path", ""), root) for root in roots)) and item.get("Id"):
                    result.append(item)
        return result

    def _sync_jellyfin(self, job, index, should_stop):
        from utils import jellyfin_api as api
        from utils.media_server import server_identity
        receipt = self._receipt(job, "jellyfin")
        if job["desired"] is None and (receipt is None or not (receipt["applied"] or receipt["attempted"])):
            return "skipped"
        matches = [item for item in index if self._matches(job["path"], item.get("Path", ""))]
        if len(matches) != 1:
            raise _SyncError("API 路径匹配不唯一或未找到，未修改服务器")
        item = matches[0]
        server, key = _get(self.cfg, "jellyfin_server", ""), _get(self.cfg, "jellyfin_api_key", "")
        target = hashlib.sha256((server_identity(server) + "\0" + str(item["Id"])).encode("utf-8")).hexdigest()
        def validate(fresh):
            from utils.library_scope import normalize_scope_path
            self._check(job, should_stop)
            return (str(fresh.get("Id")) == str(item["Id"])
                    and normalize_scope_path(fresh.get("Path")) == normalize_scope_path(item.get("Path"))
                    and self._matches(job["path"], fresh.get("Path", "")))
        result = api.update_tags_genres(server, key, str(item["Id"]), [], [],
            validate_item=validate,
            tag_transform=lambda tags: self._transform(job, "jellyfin", target, tags, should_stop),
            before_write=lambda *args: self._check(job, should_stop),
            before_post=lambda: self._mark_attempted(job, "jellyfin", should_stop),
            should_stop=lambda: self._obsolete(job, should_stop))
        if not result:
            self._check(job, should_stop)
            raise _SyncError("API 更新失败；可重试此目标")
        return "done"

    def run_pending(self, should_stop=None, log=None, retry_failed=False):
        errors, cancelled = [], False
        with self._run_lock:
            jobs = self.records()
            index, index_error = None, None
            for job in jobs:
                for endpoint in ("nfo", "jellyfin"):
                    previous = job[endpoint]
                    if previous not in ("pending", "waiting") and not (retry_failed and previous == "failed"):
                        continue
                    if (previous == "waiting" and endpoint == "jellyfin"
                            and not (_get(self.cfg, "jellyfin_server", "") and _get(self.cfg, "jellyfin_api_key", ""))):
                        continue
                    try:
                        self._check(job, should_stop)
                        if not _get(self.cfg, "censor_sync_" + endpoint, True):
                            status = "skipped"
                        elif endpoint == "nfo":
                            status = self._sync_nfo(job, should_stop)
                        elif job["desired"] is None and not any((self._receipt(job, endpoint) or {}).get(key)
                                                                  for key in ("applied", "attempted")):
                            status = "skipped"
                        elif not (_get(self.cfg, "jellyfin_server", "") and _get(self.cfg, "jellyfin_api_key", "")):
                            status = "waiting"
                        else:
                            if index is None and index_error is None:
                                try:
                                    index = self._index(should_stop)
                                except Exception:
                                    index_error = "API 条目索引失败或媒体库范围无效"
                            if index_error:
                                raise _SyncError(index_error)
                            self._check(job, should_stop)
                            status = self._sync_jellyfin(job, index, should_stop)
                        self._finish(job, endpoint, status)
                    except _Stopped:
                        cancelled = True
                        break
                    except _Stale:
                        self._finish(job, endpoint, "stale", "文件版本或人工结论已改变")
                    except Exception as exc:
                        # Only our own fixed messages may be persisted; urllib errors
                        # may embed a token in the server URL or exception payload.
                        message = str(exc) if isinstance(exc, _SyncError) else "同步目标失败；可安全重试"
                        self._finish(job, endpoint, "failed", message)
                        errors.append({"path": job["path"], "endpoint": endpoint, "error": message})
                        if log:
                            log(f"元数据同步 {endpoint}：{message}")
                if cancelled:
                    break
        return dict(self.status(), errors=errors, cancelled=cancelled or bool(should_stop and should_stop()))
