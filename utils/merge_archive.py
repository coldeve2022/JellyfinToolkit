"""合并替换的安全归档与精确回滚。

与 Qt 解耦的纯函数层：
- 把原分集及其同名 sidecar（nfo/字幕/图片）备份到可配置的大容量盘；
- 合并产物改名为 `番号.<容器>`，并同步一份可用的 NFO；
- 记录 JSON 日志，支持按单个文件恢复。
"""

from __future__ import annotations

import json
import os
import re
import uuid
import threading
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.merge import unique_output_path
from utils.nfo_source import find_nfo


def journal_file() -> Path:
    """归档日志路径。

    不写成模块级常量：``from config import DATA_DIR`` 是**导入期绑定**，
    一旦运行时重定向数据目录（测试 / 便携模式切换），模块级常量会指向旧路径。
    """
    import config

    return Path(config.DATA_DIR) / "merge_archive_journal.json"


_JOURNAL_LOCK = threading.RLock()


def __getattr__(name):
    if name == "DATA_DIR":
        import config

        return config.DATA_DIR
    raise AttributeError(name)


@contextmanager
def _locked_journal():
    with _JOURNAL_LOCK:
        path = journal_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path.with_suffix(".lock.db")), timeout=30)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS lock_holder (id INTEGER)")
            conn.execute("BEGIN IMMEDIATE")
            yield
            conn.commit()
        finally:
            conn.close()


# 与视频同名、需要跟着视频一起备份的附加文件。
# 扩展名的**唯一来源**是 utils.media_types —— 这里只做导入，
# 不再自己写一份（以前这里另有一份，和 merge.py 的清单不一致）。
from utils.media_types import (  # noqa: E402
    DEFAULT_SIDECAR_EXTENSIONS as SIDECAR_EXTENSIONS,
    DEFAULT_SUBTITLE_EXTENSIONS as SUBTITLE_EXTENSIONS,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── 日志 ─────────────────────────────
def load_journal() -> list[dict]:
    path = journal_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"归档日志损坏，拒绝覆盖已有恢复记录：{exc}") from exc
    if not isinstance(data, list):
        raise ValueError("归档日志格式无效")
    return data


def save_journal(entries: list[dict]) -> None:
    path = journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(entries, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(str(tmp), str(path))
                break
            except PermissionError as exc:
                if (
                    sys.platform != "win32"
                    or getattr(exc, "winerror", None) not in (5, 32)
                    or time.monotonic() >= deadline
                ):
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def add_journal_entries(entries: list[dict]) -> None:
    if not entries:
        return
    with _locked_journal():
        all_entries = load_journal()
        by_id = {entry.get("id"): entry for entry in all_entries}
        for entry in entries:
            by_id[entry["id"]] = entry
        save_journal(list(by_id.values()))


def mark_entry_rolled_back(entry_id: str) -> None:
    with _locked_journal():
        all_entries = load_journal()
        for e in all_entries:
            if e.get("id") == entry_id:
                e["rolled_back"] = True
                break
        save_journal(all_entries)


def rollbackable_entries(entries: list[dict] | None = None) -> list[dict]:
    all_entries = entries if entries is not None else load_journal()
    return [
        e
        for e in all_entries
        if not e.get("rolled_back")
        and e.get("kind") in {"media", "sidecar", "nfo_backup"}
        and e.get("backup_path")
        and Path(e["backup_path"]).exists()
    ]


# ── 备份路径 ─────────────────────────
def fallback_backup_root() -> Path:
    """未配置备份盘时的落点：数据目录下的 backups/（一定可写，不依赖任何盘符）。"""
    import config

    return Path(config.DATA_DIR) / "backups"


def _root_usable(path: Path) -> bool:
    """目标根目录是否可用（父目录存在且可写）。"""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return probe.is_dir() and os.access(str(probe), os.W_OK)


def resolve_backup_root(cfg) -> Path:
    """解析「替换原分集」的备份根目录。

    历史实现的默认值是写死的固定盘符 —— 在没有该盘的机器上 mkdir 直接失败，
    整个「确认替换」功能报废。现在：留空/盘符不存在/不可写 → 一律回退到
    数据目录下的 backups/，保证任何机器上都能用。
    """
    raw = str(getattr(cfg, "merge_backup_root", "") or "").strip()
    if not raw:
        return fallback_backup_root()
    if re.fullmatch(r"[A-Za-z]:", raw):
        raw += "/"
    elif re.fullmatch(r"[A-Za-z]", raw):
        raw += ":/"
    p = Path(raw)
    if not p.is_absolute():
        # 相对路径按「数据目录下」解释，不再挂到某个不存在的盘符上
        p = fallback_backup_root() / p
    return p if _root_usable(p) else fallback_backup_root()


def group_backup_dir(cfg, number: str) -> Path:
    subfolder = Path(cfg.merge_original_subfolder)
    if (
        subfolder.is_absolute()
        or ".." in subfolder.parts
        or any(mark in number for mark in ("/", "\\"))
    ):
        raise ValueError("备份子目录与番号必须位于配置的备份根目录内")
    return resolve_backup_root(cfg) / subfolder / number


def move_file_to_backup(src: str, dst_dir: Path) -> tuple[bool, str, str]:
    """移动单个文件到备份目录，返回 (ok, error, backup_path)。"""
    sp = Path(src)
    if not sp.exists():
        return False, f"文件不存在: {src}", ""
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, str(e), ""
    dst = unique_output_path(dst_dir / sp.name)
    from utils.file_ops import safe_replace

    result = safe_replace(sp, dst, move_source=True, allow_overwrite=False, kind="archive_backup")
    return result["ok"], result["error"], str(dst) if result["ok"] else ""


def iter_sidecars(video_path: str):
    """找到与视频同目录的 sidecar 文件。

    包括：
    - 同主文件名（`video.nfo` / `video.srt` / `video.jpg`）；
    - 带语言/来源后缀的字幕（`video.zh.srt` / `video.chi.ass` / `video.default.srt`）。
    """
    sp = Path(video_path)
    stem = sp.stem
    try:
        for f in sp.parent.iterdir():
            if not f.is_file() or f == sp:
                continue
            suffix = f.suffix.lower()
            if suffix in SIDECAR_EXTENSIONS and f.stem == stem:
                yield str(f)
            elif suffix in SUBTITLE_EXTENSIONS and f.stem.startswith(stem + "."):
                yield str(f)
    except OSError:
        return


def choose_source_nfo(original_paths: list[str], number: str) -> Optional[str]:
    """选择合并后应保留的 NFO：优先 `番号.nfo`，其次第一个分集的 NFO。"""
    if not original_paths:
        return None
    first_dir = Path(original_paths[0]).parent
    preferred = first_dir / f"{number}.nfo"
    if preferred.exists():
        return str(preferred)
    for p in original_paths:
        nfo = find_nfo(p)
        if nfo and Path(nfo).name.lower() not in ("movie.nfo", "tvshow.nfo"):
            return nfo
    return find_nfo(original_paths[0])


def _make_entry(
    number: str, kind: str, original_path: str, backup_path: str, final_path: str = ""
) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "number": number,
        "kind": kind,
        "original_path": original_path,
        "backup_path": backup_path,
        "final_path": final_path,
        "moved_at": _now(),
        "rolled_back": False,
    }


# ── 主流程 ─────────────────────────
def archive_group_for_merge(
    cfg, number: str, original_paths: list[str], merged_output: str, should_stop=None
):
    """Persist a complete session intent before archiving any reviewed source."""
    from utils.file_ops import file_hash, safe_replace, OperationJournal, path_key

    output = Path(merged_output)
    entries = []
    session = uuid.uuid4().hex
    journal = OperationJournal()
    original_paths = list(dict.fromkeys(original_paths))
    moved = []
    errors = []
    finalized = []
    try:
        if should_stop and should_stop():
            return False, [], ["已停止"], None
        if not output.is_file() or not output.stat().st_size:
            return False, [], ["合并输出不存在或为空"], None
        if any(not Path(p).is_file() for p in original_paths):
            return False, [], ["原分集不存在，已拒绝归档"], None
        manifest_path = output.with_name(output.name + ".toolkit-merge.json")
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else None
        )
        if manifest:
            if manifest.get("output_hash") != file_hash(output):
                return False, [], ["合并输出已经改变，请重新校验"], None
            if any(
                not Path(p).is_file() or digest != file_hash(p)
                for p, digest in manifest.get("source_hashes", {}).items()
            ) or any(
                manifest.get("source_hashes", {}).get(p) != file_hash(p) for p in original_paths
            ):
                return False, [], ["原分集身份不符，请重新合并"], None
        # Legacy outputs require a real metadata validation before archival.
        else:
            from utils.tools import resolve_tool
            from utils.merge import PartFile, probe_part, validate_merged_output

            probe = resolve_tool("ffprobe")
            parts = [
                PartFile(
                    p, Path(p).name, Path(p).stem, number, i, "cd", probe=probe_part(probe or "", p)
                )
                for i, p in enumerate(original_paths)
            ]
            ok, why = validate_merged_output(probe or "", str(output), parts)
            if not ok:
                return False, [], [f"旧合并需重新校验：{why}"], None
        sidecars = list(dict.fromkeys(side for p in original_paths for side in iter_sidecars(p)))
        subtitle_extensions = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".lrc"}
        for p in original_paths:
            for side in iter_sidecars(p):
                if Path(side).suffix.lower() in subtitle_extensions:
                    key = Path(side).name[len(Path(p).stem) :]
                    merged_sub = output.with_name(output.stem + key)
                    if not merged_sub.is_file() or not merged_sub.stat().st_size:
                        return (
                            False,
                            [],
                            ["旧合并缺少拼接后的外挂字幕，请重新合并以保留时间轴"],
                            None,
                        )
        final = (
            output.parent / f"{number}{output.suffix}"
            if bool(getattr(cfg, "merge_rename_to_number", True))
            else output
        )
        if (
            final.exists()
            and path_key(final) != path_key(output)
            and str(final) not in original_paths
        ):
            return False, [], ["目标已存在，需要人工审阅，拒绝覆盖"], None
        group_dir = group_backup_dir(cfg, number) / session
        source_nfo = choose_source_nfo(original_paths, number)
        nfo_text = Path(source_nfo).read_bytes() if source_nfo else None
        final_nfo = final.with_suffix(".nfo")
        protected = {path_key(final_nfo)}
        for kind, paths in [
            ("media", original_paths),
            ("sidecar", [p for p in sidecars if path_key(p) not in protected]),
        ]:
            for src in paths:
                entry = _make_entry(number, kind, src, str(group_dir / Path(src).name))
                entry.update(session_id=session, state="prepared", expected_hash=file_hash(src))
                entries.append(entry)
        output_entry = _make_entry(number, "merged_output", str(output), "", str(final))
        output_entry.update(
            session_id=session,
            state="prepared",
            expected_hash=file_hash(output),
            session_complete=False,
        )
        entries.append(output_entry)
        # Durable full recovery locations precede every media move.
        add_journal_entries(entries)
        for entry in entries:
            if should_stop and should_stop():
                raise OSError("已停止")
            if entry["kind"] == "merged_output":
                continue
            result = safe_replace(
                entry["original_path"],
                entry["backup_path"],
                journal=journal,
                should_stop=should_stop,
                move_source=True,
                allow_overwrite=False,
                kind="merge_archive",
                session_id=session,
            )
            entry["operation_id"] = result["operation_id"]
            entry["state"] = result["state"]
            if not result["ok"]:
                raise OSError(result["error"])
            moved.append(entry)
            add_journal_entries([entry])
        if final != output:
            result = safe_replace(
                output,
                final,
                journal=journal,
                move_source=True,
                should_stop=should_stop,
                allow_overwrite=False,
                kind="merge_finalize",
                session_id=session,
            )
            output_entry.update(operation_id=result["operation_id"], state=result["state"])
            if not result["ok"]:
                raise OSError(result["error"])
            finalized.append(result)
        else:
            output_entry["state"] = "succeeded"
        add_journal_entries([output_entry])
        # Rename merged sidecars with the media; originals already have durable backups.
        generated = []
        for side in iter_sidecars(str(output)):
            sp = Path(side)
            suffix = sp.name[len(output.stem) :]
            target = final.with_name(final.stem + suffix)
            if sp == target:
                continue
            result = safe_replace(
                sp,
                target,
                journal=journal,
                move_source=True,
                should_stop=should_stop,
                allow_overwrite=False,
                kind="merge_final_sidecar",
                session_id=session,
            )
            if not result["ok"]:
                raise OSError(result["error"])
            generated.append(result)
            finalized.append(result)
        if nfo_text and not final_nfo.exists():
            from utils.file_ops import safe_write_text
            from utils.nfo import read_text_any_encoding

            # The source NFO may now be in backup; preserve its bytes/encoding through a stage.
            source_entry = next((e for e in entries if e["original_path"] == source_nfo), None)
            source_path = source_entry["backup_path"] if source_entry else source_nfo
            text = read_text_any_encoding(source_path)
            if text is None:
                raise OSError("NFO解码失败，原文件已保留在备份")
            result = safe_write_text(
                final_nfo, text, journal=journal, should_stop=should_stop, session_id=session
            )
            if not result["ok"]:
                raise OSError(result["error"])
            finalized.append(result)
            nfo_entry = _make_entry(number, "nfo_final", source_nfo, "", str(final_nfo))
            nfo_entry.update(
                session_id=session, state="succeeded", operation_id=result["operation_id"]
            )
            entries.append(nfo_entry)
            add_journal_entries([nfo_entry])
        output_entry["session_complete"] = True
        add_journal_entries([output_entry])
        return True, entries, errors, str(final)
    except Exception as exc:
        errors.append(str(exc))
        for result in reversed(finalized):
            restored = journal.restore(result["operation_id"])
            if not restored["ok"]:
                errors.append("产物恢复失败：" + restored["error"])
        # Failed persistence leaves the already-saved intent and SQLite transaction records.
        for entry in reversed(moved):
            result = journal.restore(entry["operation_id"])
            entry["state"] = "rolled_back" if result["ok"] else "partial"
            entry["rolled_back"] = result["ok"]
            if not result["ok"]:
                errors.append("恢复失败，记录保留：" + result["error"])
        try:
            add_journal_entries(entries)
        except Exception as log_error:
            errors.append(f"恢复记录更新失败：{log_error}")
        return False, entries, errors, None


def restore_entry(entry: dict) -> tuple[bool, str]:
    """把单个备份文件恢复到原始路径。返回 (ok, detail)。"""
    backup = Path(entry.get("backup_path") or "")
    original = Path(entry.get("original_path") or "")
    if not backup.exists():
        return False, "备份文件不存在"
    try:
        original.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, str(e)
    from utils.file_ops import safe_replace, file_hash

    if original.exists():
        if original.is_file() and file_hash(original) == file_hash(backup):
            return True, str(original)
        return False, "原路径已被其它文件占用，请先恢复该会话的合并输出"
    result = safe_replace(
        backup, original, move_source=True, allow_overwrite=False, kind="archive_restore"
    )
    return result["ok"], str(original) if result["ok"] else result["error"]


def restore_session(session_id, should_stop=None):
    """Undo a merge session in reverse operation order, before restoring base names."""
    from utils.file_ops import OperationJournal

    journal = OperationJournal()
    stats = dict(ok=0, fail=0, fails=[], cancelled=False)
    operations = [entry for entry in journal.all() if entry.get("session_id") == session_id]
    if not operations:
        return dict(
            ok=0, fail=1, fails=[("session", "未找到该会话，旧日志请逐项恢复")], cancelled=False
        )
    for operation in operations:
        if should_stop and should_stop():
            stats["cancelled"] = True
            break
        result = journal.restore(operation["id"], should_stop)
        if result["ok"]:
            stats["ok"] += 1
        else:
            stats["fail"] += 1
            stats["fails"].append((operation["id"], result["error"]))
            break  # Dependency recovery must not remove later backups on a conflict.
    if not stats["fail"] and not stats["cancelled"]:
        with _locked_journal():
            entries = load_journal()
            for entry in entries:
                if entry.get("session_id") == session_id:
                    entry.update(rolled_back=True, state="rolled_back")
            save_journal(entries)
    return stats
