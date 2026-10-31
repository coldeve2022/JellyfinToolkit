"""Version-aware SQLite analysis cache and manual-label history.

Strict service reads require file identity, analysis signature and completed
processing. Legacy rows remain browsable; legacy unversioned labels cannot
confirm a new file. Schema upgrades preserve both automatic and manual records.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from contextlib import closing, contextmanager

from censor.models import (
    CensorState,
    Evidence,
    EvidenceSource,
    FrameCandidate,
    Verdict,
    VideoVerdict,
    ProcessingStatus,
)

__all__ = ["VerdictStore"]

_SCHEMA_COMPONENT = "censor_verdict"
_SCHEMA_VERSION = 1

# Keep the decoder's positional contract identical on all read paths.
_RESULT_COLUMNS = (
    "size,mtime,duration,number,provider,state,confidence,needs_review,reason,"
    "evidence,candidates,visual_score,elapsed,error,file_identity,analysis_signature,"
    "status,sampled_frames,analyzed_frames,detector_runs"
)

# Ordinary history projects the review authority in the same SQLite snapshot as
# its probe row. An empty identity matches only a legacy empty-identity label.
_HISTORY_COLUMNS = 'p.path,' + ','.join('p.' + name for name in _RESULT_COLUMNS.split(',')) + ',h.state,h.note'
_HISTORY_FROM = (
    'FROM censor_probe p LEFT JOIN censor_label_history h '
    'ON h.path=p.path AND h.file_identity=p.file_identity '
    'AND NOT EXISTS (SELECT 1 FROM censor_label_revocations r '
    'WHERE r.path=h.path AND r.file_identity=h.file_identity)'
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS censor_probe (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL DEFAULT 0,
    mtime       REAL    NOT NULL DEFAULT 0,
    duration    REAL    NOT NULL DEFAULT 0,
    number      TEXT    NOT NULL DEFAULT '',
    provider    TEXT    NOT NULL DEFAULT '',
    state       TEXT    NOT NULL DEFAULT 'unknown',
    confidence  REAL    NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 1,
    reason      TEXT    NOT NULL DEFAULT '',
    evidence    TEXT    NOT NULL DEFAULT '[]',
    candidates  TEXT    NOT NULL DEFAULT '[]',
    visual_score REAL   NOT NULL DEFAULT 0,
    elapsed     REAL    NOT NULL DEFAULT 0,
    error       TEXT    NOT NULL DEFAULT '',
    updated_at  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_censor_probe_state ON censor_probe(state);

CREATE TABLE IF NOT EXISTS censor_label (
    path        TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    labeled_at  TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dump_evidence(evidence: Iterable[Evidence]) -> str:
    return json.dumps(
        [
            {
                "source": ev.source.value,
                "state": ev.state.value,
                "confidence": ev.confidence,
                "detail": ev.detail,
                "review_required": ev.review_required,
            }
            for ev in evidence
        ],
        ensure_ascii=False,
    )


def _load_evidence(raw: str) -> list[Evidence]:
    out: list[Evidence] = []
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return out
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                Evidence(
                    source=EvidenceSource(item["source"]),
                    state=CensorState(item["state"]),
                    confidence=float(item.get("confidence", 0.0)),
                    detail=str(item.get("detail", "")),
                review_required=item.get('review_required') if isinstance(item.get('review_required'), bool) else None,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _dump_candidates(candidates: Iterable[FrameCandidate], data_dir=None) -> str:
    from utils.profile_assets import store_asset
    def asset(value):
        return store_asset(value, data_dir) if data_dir is not None else value
    return json.dumps(
        [
            {
                "timestamp": c.timestamp,
                "score": c.score,
                "block_size": c.block_size,
                "box": list(c.box) if c.box else None,
                "skin_ratio": c.skin_ratio,
                "thumb_path": asset(c.thumb_path),
                "frame_hash": c.frame_hash,
                "crop_path": asset(c.crop_path),
            }
            for c in candidates
        ],
        ensure_ascii=False,
    )


def _load_candidates(raw: str) -> list[FrameCandidate]:
    out: list[FrameCandidate] = []
    try:
        items = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return out
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            box = item.get("box")
            timestamp = float(item.get("timestamp", 0.0))
            score = float(item.get("score", 0.0))
            if not math.isfinite(timestamp) or not math.isfinite(score) or timestamp < 0 or score < 0:
                continue
            if box is not None:
                if (not isinstance(box, (list, tuple)) or len(box) != 4
                        or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)
                        or not (0 <= box[0] < box[2] and 0 <= box[1] < box[3])):
                    continue
            out.append(
                FrameCandidate(
                    timestamp=timestamp,
                    score=score,
                    block_size=int(item.get("block_size", 0) or 0),
                    box=tuple(box) if box else None,
                    skin_ratio=float(item.get("skin_ratio", 0.0) or 0.0),
                    thumb_path=str(item.get("thumb_path", "") or ""),
                    frame_hash=str(item.get("frame_hash", "") or ""),
                    crop_path=str(item.get('crop_path', '') or ''),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class StoreStats:
    """给界面显示"扫了多少、定了多少"。"""

    total: int = 0
    censored: int = 0
    uncensored: int = 0
    unknown: int = 0
    review: int = 0
    labeled: int = 0
    #: 库里存在、但最近一次分析没跑完（partial / failed / cancelled）的行数。
    #: 与 total 分开报，避免"已扫 N"把跑失败的行也算成扫完了。
    incomplete: int = 0

    def as_text(self) -> str:
        return (
            f"已入库 {self.total}（完整 {self.total - self.incomplete} · 未完成 {self.incomplete}）"
            f" · 检出像素化 {self.censored} · 人工未见 {self.uncensored}"
            f" · 待确认 {self.unknown} · 待复核 {self.review} · 人工记录 {self.labeled}"
        )


def open_sqlite(db_path, *, timeout: float = 15.0, attempts: int = 3,
                probe: bool = True):
    """打开 SQLite 连接，并对 Windows 上的"瞬时可写性"做兜底。

    为什么需要它：杀毒软件/备份工具的实时扫描会短暂锁住 db 与 -wal 文件，
    SQLite 会把这种状态报成 ``attempt to write a readonly database``。
    对使用者来说就是"偶尔有几个视频保存失败"，其实重试一次就好。

    两件事：
    1. ``PRAGMA journal_mode=WAL`` 失败（网络盘、部分安全软件）时退回默认日志模式；
    2. 连接阶段用一次 ``BEGIN IMMEDIATE`` 探真实写权限 —— 只读会在这里暴露，
       而不是等用户代码写到一半才炸。探针失败只重试"打开"这一步，不重放事务体，
       所以不会造成重复写入。
    """
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                try:
                    conn.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.OperationalError:
                    pass
            if probe:
                # 只在初始化时探一次写权限。**必须零等待**：BEGIN IMMEDIATE 会抢
                # 写锁，若此时别的连接持锁，默认 busy_timeout=15s 会把这里卡死
                # （全量测试里表现为"扫描 20 秒都填不出行"）。
                conn.execute("PRAGMA busy_timeout=0")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
                except sqlite3.OperationalError as exc:
                    if "readonly" in str(exc).lower():
                        raise            # 真只读：交给外层重试
                    # 只是被别人锁着，不是只读 —— 放行，让正常流程按 busy_timeout 等
                finally:
                    conn.execute("PRAGMA busy_timeout=15000")
            return conn
        except sqlite3.OperationalError as exc:
            last = exc
            conn.close()
            if attempt + 1 >= max(1, attempts):
                break
            time.sleep(0.08 * (attempt + 1))
    raise last if last is not None else sqlite3.OperationalError("无法打开数据库")


class VerdictStore:
    """SQLite 结果缓存。线程安全靠"每次操作新建连接"，简单且够用。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # 初始化时探一次真实写权限（瞬时只读会在这里被重试掉）
        open_sqlite(self.db_path).close()
        self._ensure_schema()

    # ── 基础设施 ──
    @contextmanager
    def _connect(self):
        conn = open_sqlite(self.db_path, probe=False)
        try:
            self._schema_version(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _schema_version(conn) -> int:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='toolkit_schema_versions'").fetchone():
            return 0
        row = conn.execute("SELECT version FROM toolkit_schema_versions WHERE component=?",
                           (_SCHEMA_COMPONENT,)).fetchone()
        version = int(row[0]) if row else 0
        if version > _SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库组件 {_SCHEMA_COMPONENT} 版本 {version} 高于当前支持的 "
                f"{_SCHEMA_VERSION}；请使用较新版本程序，已拒绝写入。")
        return version

    def _backup_before_migration(self) -> None:
        backup_dir = self.db_path.parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        filename = (f'{self.db_path.name}.{_SCHEMA_COMPONENT}.pre-v{_SCHEMA_VERSION}.'
                    f'{datetime.now():%Y%m%dT%H%M%S}.{uuid.uuid4().hex}.sqlite3')
        target = backup_dir / filename
        partial = target.with_suffix('.partial')
        # Use a separate reader: backing up a connection holding a write
        # transaction would wait for itself. BEGIN IMMEDIATE on the migrator
        # prevents any writer from changing this snapshot until migration ends.
        try:
            with closing(sqlite3.connect(str(self.db_path), timeout=15)) as source:
                with closing(sqlite3.connect(str(partial))) as destination:
                    source.backup(destination)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            self._schema_version(conn)
            conn.execute('BEGIN IMMEDIATE')
            version = self._schema_version(conn)
            if version == _SCHEMA_VERSION:
                conn.commit()
                return
            existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
            if existing:
                self._backup_before_migration()
            conn.execute("CREATE TABLE IF NOT EXISTS toolkit_schema_versions ("
                         "component TEXT PRIMARY KEY,version INTEGER NOT NULL,"
                         "updated_at TEXT NOT NULL)")
            self._migrate_v1(conn)
            conn.execute("INSERT INTO toolkit_schema_versions(component,version,updated_at) "
                         "VALUES(?,?,?) ON CONFLICT(component) DO UPDATE SET "
                         "version=excluded.version,updated_at=excluded.updated_at",
                         (_SCHEMA_COMPONENT, _SCHEMA_VERSION, _now()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_v1(self, conn) -> None:
        # executescript implicitly commits: individual statements keep DDL,
        # legacy import and version registration in one rollback boundary.
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(censor_probe)")}
        added_status = False
        for name, definition in {
            "file_identity": "TEXT NOT NULL DEFAULT ''",
            "analysis_signature": "TEXT NOT NULL DEFAULT ''",
            # 默认 'partial' 对"刚插进去还没分析的行"是对的，但对**升级上来的历史库**
            # 是错的：老版本的 censor_probe 只在分析跑完后才写入，存量行都是完成的。
            # 所以下面会针对"本次新建该列"的情况回填成 completed，否则老库会整库
            # 显示"未跑完"，而且严格读缓存会一律 miss（每次全库重扫）。
            "status": "TEXT NOT NULL DEFAULT 'partial'",
            "sampled_frames": "INTEGER NOT NULL DEFAULT 0",
            "analyzed_frames": "INTEGER NOT NULL DEFAULT 0",
            "detector_runs": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE censor_probe ADD COLUMN {name} {definition}")
                if name == "status":
                    added_status = True
        if added_status:
            conn.execute(
                "UPDATE censor_probe SET status='completed' WHERE status='partial'"
            )
        had_history = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='censor_label_history'"
        ).fetchone()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS censor_label_history ("
            "path TEXT NOT NULL, file_identity TEXT NOT NULL, state TEXT NOT NULL,"
            "note TEXT NOT NULL, labeled_at TEXT NOT NULL,"
            "PRIMARY KEY(path, file_identity))"
        )
        if not had_history:
            conn.execute(
                "INSERT OR IGNORE INTO censor_label_history "
                "SELECT path, '', state, note, labeled_at FROM censor_label"
            )
        conn.execute("CREATE TABLE IF NOT EXISTS censor_label_revocations ("
                     "path TEXT NOT NULL,file_identity TEXT NOT NULL,revoked_at TEXT NOT NULL,"
                     "PRIMARY KEY(path,file_identity))")
        conn.execute("CREATE TABLE IF NOT EXISTS censor_label_events ("
                     "id INTEGER PRIMARY KEY,path TEXT NOT NULL,file_identity TEXT NOT NULL,"
                     "state TEXT,note TEXT NOT NULL,created_at TEXT NOT NULL)")

    # ── 读写 ──
    def get(
        self,
        path: str,
        size: int = -1,
        mtime: float = -1.0,
        *,
        analysis_signature: str | None = None,
        file_identity: str | None = None,
    ) -> Optional[VideoVerdict]:
        """读缓存。传了 size/mtime 时会校验是否过期，过期返回 None。"""
        key = str(path)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_RESULT_COLUMNS} FROM censor_probe WHERE path = ?",
                (key,),
            ).fetchone()
        return self._decode_row(key, row, size=size, mtime=mtime,
                                analysis_signature=analysis_signature, file_identity=file_identity)

    def _decode_row(self, key, row, *, size=-1, mtime=-1., analysis_signature=None, file_identity=None):
        # A damaged record must be a miss on every read path, including a
        # direct rescan. Keep the row for diagnosis; a completed scan repairs it.
        try:
            result = VerdictStore._decode_valid_row(key, row, size=size, mtime=mtime,
                analysis_signature=analysis_signature, file_identity=file_identity)
            if result is not None:
                from utils.profile_assets import resolve_asset
                for candidate in result.candidates:
                    candidate.thumb_path = resolve_asset(candidate.thumb_path, self.db_path.parent)
                    candidate.crop_path = resolve_asset(candidate.crop_path, self.db_path.parent)
            return result
        except (ValueError, TypeError, KeyError, OverflowError):
            return None

    @staticmethod
    def _decode_valid_row(key, row, *, size=-1, mtime=-1., analysis_signature=None, file_identity=None):
        if not row:
            return None
        (
            db_size,
            db_mtime,
            duration,
            number,
            provider,
            state,
            confidence,
            needs_review,
            reason,
            evidence,
            candidates,
            visual_score,
            elapsed,
            error,
            identity,
            signature,
            status,
            sampled,
            analyzed,
            runs,
        ) = row
        try:
            runs = json.loads(runs)
            if not isinstance(runs, dict):
                runs = {}
        except (TypeError, ValueError):
            runs = {}
        if analysis_signature is not None and (
            signature != analysis_signature or status != "completed" or error
        ):
            return None
        if file_identity is not None and identity != file_identity:
            return None
        if size >= 0 and int(db_size) != int(size):
            return None
        if mtime >= 0 and abs(float(db_mtime) - float(mtime)) > 1e-6:
            return None
        verdict = Verdict(
            state=CensorState(state),
            confidence=float(confidence),
            evidence=_load_evidence(evidence),
            needs_review=bool(needs_review),
            reason=reason or "",
        )
        return VideoVerdict(
            path=key,
            size=int(db_size),
            mtime=float(db_mtime),
            duration=float(duration),
            number=number or "",
            provider=provider or "",
            verdict=verdict,
            candidates=_load_candidates(candidates),
            visual_score=float(visual_score),
            elapsed=float(elapsed),
            error=error or "",
            file_identity=identity,
            analysis_signature=signature,
            status=ProcessingStatus(status),
            sampled_frames=int(sampled),
            analyzed_frames=int(analyzed),
            detector_runs=runs,
        )

    def put(self, result: VideoVerdict, *, policy=None, persist: bool = True,
            allow_manual: bool = True) -> None:
        """Submit an analysis against the current confirmation in one transaction.

        ``policy`` opts service submissions into authoritative label fusion. A
        writer lock must precede the label read: a review can finish while a
        detector is running, or immediately before this call acquires its lock.
        Incomplete analyses are reconciled for display without replacing a
        previously successful analysis. ``allow_manual=False`` rejects the old
        version's confirmation if the file changed or vanished during analysis.
        """
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if policy is not None:
                self._reconcile_result(conn, result, policy, allow_manual=allow_manual)
            if persist:
                self._write_result(conn, result)

    @staticmethod
    def _reconcile_result(conn, result, policy, *, allow_manual=True):
        from censor.fusion import fuse

        row = conn.execute(
            "SELECT state,note FROM censor_label_history h WHERE path=? AND file_identity=? "
            "AND NOT EXISTS (SELECT 1 FROM censor_label_revocations r "
            "WHERE r.path=h.path AND r.file_identity=h.file_identity)",
            (str(result.path), result.file_identity),
        ).fetchone()
        evidence = [ev for ev in result.verdict.evidence if ev.source is not EvidenceSource.MANUAL]
        if allow_manual and row and row[0] in (CensorState.CENSORED.value, CensorState.UNCENSORED.value):
            evidence.append(Evidence(EvidenceSource.MANUAL, CensorState(row[0]), 1., row[1] or '人工确认'))
        result.verdict = fuse(evidence, policy)
        if result.status is not ProcessingStatus.COMPLETED:
            if result.verdict.of(EvidenceSource.MANUAL) is None:
                result.verdict = fuse([], policy)
                result.verdict.evidence = evidence
                result.verdict.needs_review = True
                result.verdict.reason = result.error or result.status.label
            else:
                result.verdict.reason += '；最近分析未完成：' + (result.error or result.status.label)

    def _write_result(self, conn, result):
        """写入一条分析结论。

        一条铁律：**未完成的分析不得覆盖已完成的结论**。

        以前这里是无条件 UPSERT，而 `probe` 只在 `COMPLETED` 时才会 persist，
        两边凑在一起就是用户报的"列表 226 条、已保存 0 条"：
        只要有一个检测器抛异常，整批结果就变成 `PARTIAL` → 一行都不落库 →
        `stats()` 读到 0 → 重启后那 226 条整批消失。

        现在未完成也落库（界面上的行数与库里的行数才对得上），
        但绝不顶掉一条已经完整跑完的好结论。
        """
        if result.status is not ProcessingStatus.COMPLETED:
            existing = conn.execute(
                "SELECT status FROM censor_probe WHERE path=?", (str(result.path),)
            ).fetchone()
            if existing is not None and existing[0] == ProcessingStatus.COMPLETED.value:
                return
        conn.execute(
            "INSERT INTO censor_probe (path, size, mtime, duration, number, provider,"
            " state, confidence, needs_review, reason, evidence, candidates,"
            " visual_score, elapsed, error, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET"
            " size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,"
            " number=excluded.number, provider=excluded.provider, state=excluded.state,"
            " confidence=excluded.confidence, needs_review=excluded.needs_review,"
            " reason=excluded.reason, evidence=excluded.evidence,"
            " candidates=excluded.candidates, visual_score=excluded.visual_score,"
            " elapsed=excluded.elapsed, error=excluded.error, updated_at=excluded.updated_at",
            (
                str(result.path),
                int(result.size),
                float(result.mtime),
                float(result.duration),
                result.number,
                result.provider,
                result.verdict.state.value,
                float(result.verdict.confidence),
                1 if result.verdict.needs_review else 0,
                result.verdict.reason,
                _dump_evidence(result.verdict.evidence),
                _dump_candidates(result.candidates, self.db_path.parent),
                float(result.visual_score),
                float(result.elapsed),
                result.error or "",
                _now(),
            ),
        )
        conn.execute(
            "UPDATE censor_probe SET file_identity=?, analysis_signature=?, "
            "status=?, sampled_frames=?, analyzed_frames=?, detector_runs=? WHERE path=?",
            (
                result.file_identity,
                result.analysis_signature,
                result.status.value,
                result.sampled_frames,
                result.analyzed_frames,
                json.dumps(result.detector_runs, ensure_ascii=False),
                str(result.path),
            ),
        )

    def delete(self, path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_probe WHERE path = ?", (str(path),))

    def clear_probes(self) -> None:
        """清空自动检测结果，**保留人工结论**。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_probe")

    # ── 人工标注 ──
    def set_label(
        self, path: str, state: CensorState, note: str = "", *, file_identity: str = "",
        result: VideoVerdict | None = None, policy=None,
    ) -> None:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "INSERT INTO censor_label (path, state, note, labeled_at) VALUES (?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET state=excluded.state,"
                " note=excluded.note, labeled_at=excluded.labeled_at",
                (str(path), state.value, note, _now()),
            )
            conn.execute(
                "INSERT INTO censor_label_history VALUES (?,?,?,?,?) "
                "ON CONFLICT(path,file_identity) DO UPDATE SET state=excluded.state,"
                "note=excluded.note,labeled_at=excluded.labeled_at",
                (str(path), file_identity, state.value, note, _now()),
            )
            conn.execute("DELETE FROM censor_label_revocations WHERE path=? AND file_identity=?",
                         (str(path), file_identity))
            conn.execute("INSERT INTO censor_label_events(path,file_identity,state,note,created_at) "
                         "VALUES (?,?,?,?,?)", (str(path), file_identity, state.value, note, _now()))
            if result is not None:
                self._reconcile_result(conn, result, policy)
                self._write_result(conn, result)

    def get_label(
        self, path: str, *, file_identity: str | None = None
    ) -> Optional[tuple[CensorState, str]]:
        with self._connect() as conn:
            if file_identity is None:
                row = conn.execute(
                    "SELECT state, note FROM censor_label WHERE path=?", (str(path),)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT state,note FROM censor_label_history h WHERE path=? AND file_identity=? "
                    "AND NOT EXISTS (SELECT 1 FROM censor_label_revocations r "
                    "WHERE r.path=h.path AND r.file_identity=h.file_identity)",
                    (str(path), file_identity),
                ).fetchone()
        if not row:
            return None
        try:
            state = CensorState(row[0])
        except (TypeError, ValueError):
            return None
        return state, row[1] or ""

    def label_history(self, path: str) -> list[tuple]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT file_identity,state,note,labeled_at "
                "FROM censor_label_history WHERE path=? ORDER BY labeled_at",
                (str(path),),
            ).fetchall()

    def labels(self) -> dict[str, CensorState]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, state FROM censor_label").fetchall()
        return {path: CensorState(state) for path, state in rows}

    def clear_labels(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM censor_label")
            conn.execute("DELETE FROM censor_label_history")
            conn.execute("DELETE FROM censor_label_revocations")
            conn.execute("DELETE FROM censor_label_events")

    def remove_label(self, path: str, file_identity: str, *, policy=None) -> VideoVerdict | None:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("DELETE FROM censor_label WHERE path=?", (str(path),))
            conn.execute("INSERT OR REPLACE INTO censor_label_revocations VALUES (?,?,?)",
                         (str(path), file_identity, _now()))
            conn.execute("INSERT INTO censor_label_events(path,file_identity,state,note,created_at) "
                         "VALUES (?,?,NULL,?,?)", (str(path), file_identity, "撤回人工确认", _now()))
            if policy is not None:
                row = conn.execute(f'SELECT {_RESULT_COLUMNS} FROM censor_probe WHERE path=?',
                                   (str(path),)).fetchone()
                result = self._decode_row(str(path), row, file_identity=file_identity)
                if result is not None:
                    self._reconcile_result(conn, result, policy)
                    # A manual-only run may have skipped the visual detectors.
                    result.analysis_signature = ''
                    self._write_result(conn, result)
                return result
        return None

    def get_many(self, paths: Iterable[str]) -> dict[str, VideoVerdict]:
        keys = list(dict.fromkeys(str(p) for p in paths))
        out = {}
        with self._connect() as conn:
            for offset in range(0, len(keys), 800):
                batch = keys[offset:offset + 800]
                rows = conn.execute(f"SELECT path,{_RESULT_COLUMNS} FROM censor_probe WHERE path IN "
                                    f"({','.join('?' for _ in batch)})", batch).fetchall()
                for row in rows:
                    try:
                        result = self._decode_row(row[0], row[1:])
                        if result is not None:
                            out[row[0]] = result
                    except (ValueError, TypeError, KeyError):
                        continue
        return out

    # ── 统计 ──
    def stats(self) -> StoreStats:
        stats = StoreStats()
        with self._connect() as conn:
            for state, count in conn.execute(
                "SELECT state, COUNT(*) FROM censor_probe GROUP BY state"
            ):
                stats.total += int(count)
                if state == CensorState.CENSORED.value:
                    stats.censored = int(count)
                elif state == CensorState.UNCENSORED.value:
                    stats.uncensored = int(count)
                else:
                    stats.unknown = int(count)
            stats.review = int(
                conn.execute("SELECT COUNT(*) FROM censor_probe WHERE needs_review = 1").fetchone()[
                    0
                ]
            )
            stats.labeled = int(conn.execute("SELECT COUNT(*) FROM censor_label").fetchone()[0])
            stats.incomplete = int(
                conn.execute(
                    "SELECT COUNT(*) FROM censor_probe WHERE status != ?",
                    (ProcessingStatus.COMPLETED.value,),
                ).fetchone()[0]
            )
        return stats

    def review_queue(self, limit: int = 500) -> list[VideoVerdict]:
        """按"分数从高到低"给出待复核列表 —— 最可疑的排前面。"""
        if int(limit) <= 0:
            return []
        out = []
        from censor.review_route import needs_review
        # Stream rows from one snapshot instead of opening a connection per
        # video or materializing the entire history just to display one page.
        with self._connect() as conn:
            conn.execute('BEGIN')
            rows = conn.execute(
                f'SELECT {_HISTORY_COLUMNS} {_HISTORY_FROM} '
                'ORDER BY p.visual_score DESC,p.updated_at DESC,p.path')
            for row in rows:
                item = self._project_history_row(row)
                if item is not None and needs_review(item):
                    out.append(item)
                    if len(out) >= int(limit):
                        break
        return out

    def confirmed_censored(self, verify_identity=True) -> list[VideoVerdict]:
        """**已人工确认「有码」**的记录 —— 阶段二（找替换）的唯一数据来源。

        与历史页共用按身份关联的人工标记投影，不依赖 probe 内旧证据副本；
        换了文件版本（file_identity 不匹配）的老标记不会混进来。
        """
        with self._connect() as conn:
            rows = conn.execute(f"SELECT {_HISTORY_COLUMNS} {_HISTORY_FROM} "
                                "WHERE h.state='censored' ORDER BY p.updated_at DESC,p.path").fetchall()
        out: list[VideoVerdict] = []
        for row in rows:
            item = self._project_history_row(row)
            if item is None:
                continue
            if verify_identity:
                from censor.identity import identify_file
                try:
                    identity = identify_file(item.path).serialize()
                except OSError:
                    continue
                if item.file_identity != identity:
                    continue
            label = item.verdict.of(EvidenceSource.MANUAL)
            if label is None or label.state is not CensorState.CENSORED:
                continue
            out.append(item)
        return out

    def history(self, limit: int = 500, offset: int = 0) -> list[VideoVerdict]:
        """Compatibility list API; use history_page for a durable paging cursor."""
        return self.history_page(limit, offset)['records']

    def _project_history_row(self, row):
        """Repair only the displayed confirmation, leaving the saved row intact."""
        result = self._decode_row(row[0], row[1:-2])
        if result is None:
            return None
        current = row[-2] in (CensorState.CENSORED.value, CensorState.UNCENSORED.value)
        had_manual = result.verdict.of(EvidenceSource.MANUAL) is not None
        if not current and not had_manual:
            return result
        from censor.fusion import fuse
        evidence = [ev for ev in result.verdict.evidence if ev.source is not EvidenceSource.MANUAL]
        if current:
            evidence.append(Evidence(EvidenceSource.MANUAL, CensorState(row[-2]), 1., row[-1] or '人工确认'))
        result.verdict = fuse(evidence)
        if result.status is not ProcessingStatus.COMPLETED:
            if not current:
                result.verdict = fuse([])
                result.verdict.evidence = evidence
                result.verdict.needs_review = True
                result.verdict.reason = result.error or result.status.label
            else:
                result.verdict.reason += '；最近分析未完成：' + (result.error or result.status.label)
        return result

    def history_page(self, limit: int = 500, offset: int = 0) -> dict:
        limit, offset = max(0, int(limit)), max(0, int(offset))
        if not limit:
            return dict(records=[], next_offset=offset, has_more=False)
        with self._connect() as conn:
            rows = conn.execute(f'SELECT {_HISTORY_COLUMNS} {_HISTORY_FROM} '
                                'ORDER BY p.updated_at DESC,p.path LIMIT ? OFFSET ?',
                                (limit + 1, offset)).fetchall()
        records = []
        for row in rows[:limit]:
            result = self._project_history_row(row)
            if result is not None:
                records.append(result)
        return dict(records=records, next_offset=offset + min(len(rows), limit),
                    has_more=len(rows) > limit)

    def iter_history_pages(self, page_size: int = 500):
        """Stream one consistent history snapshot without loading every row at once."""
        page_size = max(1, int(page_size))
        with self._connect() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT count(*) FROM censor_probe').fetchone()[0]
            cursor = conn.execute(f'SELECT {_HISTORY_COLUMNS} {_HISTORY_FROM} '
                                  'ORDER BY p.updated_at DESC,p.path')
            offset = 0
            while True:
                rows = cursor.fetchmany(page_size)
                if not rows and offset:
                    break
                records = []
                for row in rows:
                    result = self._project_history_row(row)
                    if result is not None:
                        records.append(result)
                offset += len(rows)
                yield dict(records=records, next_offset=offset,
                           has_more=offset < total, total=total)
                if offset >= total:
                    break

    def referenced_thumbnails(self):
        with self._connect() as conn:
            rows = conn.execute('SELECT candidates FROM censor_probe').fetchall()
        paths = set()
        for (raw,) in rows:
            for candidate in _load_candidates(raw):
                for path in (candidate.thumb_path, candidate.crop_path):
                    if path:
                        from utils.profile_assets import resolve_asset
                        resolved = resolve_asset(path, self.db_path.parent)
                        if resolved:
                            paths.add(str(Path(resolved).resolve()))
        return paths
