"""One reviewable restored-video pairing/execution service for GUI and CLI."""

from __future__ import annotations

from pathlib import Path

from utils.file_ops import OperationJournal, file_hash, path_key, safe_replace
from utils.naming import clean_restored_name


def _videos(paths, should_stop=None):
    from utils.file_scan import iter_media_files

    return iter_media_files(paths, should_stop=should_stop)


def preview_replacements(restored_paths, library_paths, marker="restored", should_stop=None):
    index = {}
    for value in _videos(library_paths, should_stop):
        path = Path(value)
        index.setdefault(path.name.casefold(), []).append(str(path))
    plan = []
    for value in _videos(restored_paths, should_stop):
        path = Path(value)
        if path.suffix.lower() != ".mp4" or marker.casefold() not in path.name.casefold():
            continue
        clean = clean_restored_name(path.name, marker)
        candidates = list(dict.fromkeys(index.get((clean or "").casefold(), [])))
        status = "ready" if len(candidates) == 1 else "ambiguous" if candidates else "not_found"
        target = candidates[0] if status == "ready" else ""
        if target and path_key(path) == path_key(target):
            status, target = "conflict", ""
        plan.append(
            dict(
                source=str(path),
                target=target,
                candidates=candidates,
                status=status,
                error="" if status == "ready" else "需要审阅配对",
                source_hash=file_hash(path),
                target_hash=file_hash(target) if target else None,
            )
        )
    targets = {}
    for row in plan:
        if row["target"]:
            targets.setdefault(path_key(row["target"]), []).append(row)
    for rows in targets.values():
        if len(rows) > 1:
            for row in rows:
                row.update(status="conflict", error="多个恢复文件配对同一目标")
    return plan


def validate_restored_output(original, output):
    from utils.tools import resolve_tool
    from utils.merge import probe_part

    tool = resolve_tool("ffprobe")
    if not tool:
        return False, "缺少 ffprobe，不能可靠校验替换产物"
    old, new = probe_part(tool, str(original)), probe_part(tool, str(output))
    if not new.ok or not new.video_codec or new.duration <= 0:
        return False, "恢复视频没有有效视频流/时长，已保留原件"
    if not old.ok or old.duration <= 0:
        return False, "原版无法读出时长，需人工审阅替换，已保留原件"
    if abs(new.duration - old.duration) > max(0.5, old.duration * 0.05):
        return False, "恢复视频时长与原版不符，已保留原件"
    for kind in ("audio", "subtitle"):
        if sum(s.get("codec_type") == kind for s in old.streams) != sum(
            s.get("codec_type") == kind for s in new.streams
        ):
            return False, "恢复视频的音轨/字幕轨与原版不符，已保留原件"
    return True, ""


def execute_replacements(plan, *, journal=None, should_stop=None, log=print, validator=None):
    journal = journal or OperationJournal()
    stats = dict(ok=0, fail=0, cancelled=False, fails=[], operations=[])
    for row in plan:
        if should_stop and should_stop():
            stats["cancelled"] = True
            break
        try:
            if row.get("status") != "ready" or not row.get("target"):
                raise ValueError(row.get("error") or "配对未获确认")
            if (
                file_hash(row["source"]) != row["source_hash"]
                or file_hash(row["target"]) != row["target_hash"]
            ):
                raise ValueError("计划创建后文件发生变化，请重新审阅")
            result = safe_replace(
                row["source"],
                row["target"],
                journal=journal,
                should_stop=should_stop,
                move_source=True,
                validator=validator
                or (lambda output: validate_restored_output(row["target"], output)),
                kind="restored_replace",
                expected_source_hash=row["source_hash"],
                expected_target_hash=row["target_hash"],
            )
            stats["operations"].append(result)
            if not result["ok"]:
                if result["state"] == "cancelled":
                    stats["cancelled"] = True
                    break
                raise ValueError(result["error"])
            stats["ok"] += 1
            log(f"替换成功（可恢复）：{row['source']} → {row['target']}")
        except (OSError, ValueError, KeyError) as exc:
            stats["fail"] += 1
            stats["fails"].append((row.get("source", ""), str(exc)))
            log(f"替换失败：{exc}")
    return stats
