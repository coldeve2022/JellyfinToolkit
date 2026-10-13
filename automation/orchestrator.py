"""Plan and execute scoped operations against one reviewed library snapshot."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import os
from pathlib import Path

from config import ToolkitConfig
from utils import jellyfin_api, library_source
from utils.file_scan import iter_media_files
from utils.library import summarize
from utils.scoring import DEC_DELETE, dedup_groups
from utils.trash import DeleteJournal, trash_file
from utils.merge import (
    build_merge_plan,
    display_number_for_group,
    unique_output_path,
    EpisodeGroup,
    PartFile,
    PartProbe,
)
from automation.pipeline import _merge_options, check_old_merges, merge_group_ffmpeg, resolve_tools
from automation.plans import save_plan, load_plan, file_identity, write_json_atomic, claim_plan


def _data_dir():
    import config

    return config.DATA_DIR


def _key(path):
    return os.path.normcase(os.path.realpath(path))


def _has_protected_activity(item):
    return (
        not getattr(item, "behavior_known", True)
        or getattr(item, "is_favorite", False)
        or getattr(item, "is_played", False)
        or getattr(item, "has_progress", False)
        or getattr(item, "play_count", 0) > 0
        or getattr(item, "rating", None) is not None
    )


def collect_snapshot(cfg, log=print, should_stop=None):
    items = library_source.collect(
        cfg,
        log=log,
        should_stop=should_stop,
        exclude_keywords=getattr(cfg, "exclude_path_keywords", None),
    )
    return {
        "items": items,
        "paths": [it.path for it in items if it.path and Path(it.path).is_file()],
        "summary": summarize(items),
    }


def score_items(items, log=print, cfg=None):
    from utils.scoring_service import score_snapshot

    result = score_snapshot(items, cfg or ToolkitConfig.load())
    log(f"评分完成：{result['counts']}")
    return result


def find_missing_subtitles(paths, subtitle_extensions, output_dir=""):
    from utils.subtitle_clean import find_subtitles

    formats = [str(e).lstrip(".") for e in (subtitle_extensions or [".srt"])]
    return [p for p in paths if not find_subtitles(p, formats, output_dir or None)]


def fix_nfo_directory(directory, log=print, should_stop=None):
    from utils.nfo import fix_nfo_directory as execute

    return execute(directory, log, should_stop)


def replace_restored(restored_paths, library_paths, marker, log=print, should_stop=None):
    from utils.replacement import preview_replacements, execute_replacements

    result = execute_replacements(
        preview_replacements(restored_paths, library_paths, marker, should_stop),
        log=log,
        should_stop=should_stop,
    )
    return result["ok"], result["fail"]


def clean_jellyfin(server, api_key, refresh, log=print, should_stop=None, dirty=None):
    stopped = should_stop or (lambda: False)
    if not server or not api_key:
        raise ValueError("未配置媒体服务器 API")
    rows = (
        jellyfin_api.scan_dirty(server, api_key, log=log, should_stop=stopped)
        if dirty is None
        else dirty
    )
    backup = _data_dir() / f"jellyfin_clean_backup_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    return jellyfin_api.execute_clean_plan(
        server, api_key, rows, str(backup), refresh=refresh, should_stop=stopped, log=log
    )


def repair_report(paths, log=print, ffmpeg="", ffprobe="", should_stop=None):
    from utils.video_health import analyze_video

    if not ffprobe:
        raise ValueError("视频体检需要有效的 ffprobe")
    result = {"corrupted": [], "incompatible": []}
    for path in paths:
        if should_stop and should_stop():
            break
        info = analyze_video(path, ffprobe=ffprobe)
        if info.get("is_corrupted"):
            result["corrupted"].append(path)
        elif not info.get("is_compatible"):
            result["incompatible"].append(path)
    log(f"视频体检完成：{len(result['corrupted'])} 个损坏，{len(result['incompatible'])} 个不兼容")
    return result


def _group_from_dict(raw):
    row = dict(raw)
    row["parts"] = [
        PartFile(**{**part, "probe": PartProbe(**part["probe"])}) for part in row["parts"]
    ]
    return EpisodeGroup(**row)


def _prepare_actions(cfg, opts, paths, items, scoring, merge_plan, stopped):
    from utils.nfo import preview_nfo_fixes
    from utils.replacement import preview_replacements
    from utils.merge_executor import choose_merge_container, preview_merge_inputs
    from utils.labels import legacy_protection_labels

    kinds = ("merge", "dedup", "delete", "nfo_fix", "replace", "jellyfin_clean")
    selected = opts.get("selected_actions")
    if selected is None:
        selected = [kind for kind in kinds if opts.get("apply_" + kind)]
    if set(selected) - set(kinds):
        raise ValueError("操作类型无效")
    labels = {
        **legacy_protection_labels(_data_dir() / "smart.db", items, scoring["labels"]),
        **scoring["labels"],
    }
    duplicates = dedup_groups(items, labels)
    protected = {_key(it.path) for it in items if it.path and it.id in labels}
    for group in duplicates:
        protected.update(
            _key(it.path) for it in group["items"] if it.path and it.id not in group["losers"]
        )
    actions, queued = [], set()

    def queue_delete(item, kind):
        if not item.path or item.id in labels or _has_protected_activity(item):
            return
        key = _key(item.path)
        if key in protected or key in queued or not Path(item.path).is_file():
            return
        queued.add(key)
        actions.append({"kind": kind, "path": item.path, "item_id": item.id, "size": item.size})

    if "dedup" in selected:
        for group in duplicates:
            if not group["all_cd"]:
                for item in group["items"]:
                    if item.id in group["losers"]:
                        queue_delete(item, "dedup")
    if "delete" in selected:
        for result in scoring["results"]:
            if result.decision == DEC_DELETE and not result.is_seen and not result.is_junk:
                queue_delete(result.item, "delete")
    if "merge" in selected:
        for group in merge_plan.groups:
            if group.status != "ready" or len(group.selected_paths) < 2:
                continue
            parts = [p for p in group.parts if p.path in group.selected_paths]
            ext = choose_merge_container(parts, cfg.merge_output_container)
            target = unique_output_path(
                Path(parts[0].path).parent
                / f"{display_number_for_group(group) or group.number} 合并.{ext}"
            )
            actions.append(
                {
                    "kind": "merge",
                    "group": asdict(group),
                    "inputs": preview_merge_inputs(parts),
                    "target": str(target),
                }
            )
    if "replace" in selected:
        for row in preview_replacements(
            opts.get("restored_paths") or [],
            opts.get("library_paths") or [],
            cfg.restored_suffix_marker,
            stopped,
        ):
            if row["status"] != "ready":
                raise ValueError(f"替换配对需要审核：{row['source']}：{row['status']}")
            actions.append(
                {
                    "kind": "replace",
                    "entry": row,
                    "path": row["source"],
                    "inputs": [row["source"], row["target"]],
                    "target": row["target"],
                }
            )
    if "nfo_fix" in selected:
        for row in preview_nfo_fixes(opts.get("nfo_dirs") or [], stopped):
            if row["status"] != "ready":
                raise ValueError(f"NFO目标冲突：{row['target']}")
            actions.append(
                {"kind": "nfo_fix", "entry": row, "path": row["source"], "target": row["target"]}
            )
    if "jellyfin_clean" in selected:
        allowed = {str(it.id).replace("-", "").lower() for it in items}
        for row in jellyfin_api.scan_dirty(
            cfg.jellyfin_server, cfg.jellyfin_api_key, log=lambda _: None, should_stop=stopped
        ):
            if str(row.get("item_id", row.get("Id", ""))).replace("-", "").lower() in allowed:
                actions.append({"kind": "jellyfin_clean", "entry": row})
    touched = set(queued)
    for action in actions:
        if action["kind"] in ("merge", "replace", "nfo_fix"):
            keys = {_key(p) for p in action.get("inputs", [action.get("path")]) if p}
            if touched & keys:
                raise ValueError("同一文件同时被选入多个写操作，请拆分审核计划")
            touched.update(keys)
    return actions, selected, duplicates


def _execute_plan(cfg, opts, plan, report, ffmpeg, ffprobe, stopped, log, progress):
    from utils.nfo import execute_nfo_fixes
    from utils.replacement import execute_replacements
    from utils.labels import LabelStore
    from utils.labels import legacy_protection_labels
    from utils.state_context import state_context

    selected = {kind for kind in plan["selected_actions"] if opts.get("apply_" + kind)}
    requested = {k[6:] for k, v in opts.items() if k.startswith("apply_") and v}
    if requested - set(plan["selected_actions"]):
        raise ValueError("执行操作超出已审核计划，请重新生成计划")
    if not selected:
        report["status"] = "planned"
        return report
    current_by_path = {}
    context = plan.get("report", {}).get("stages", {}).get("score", {}).get(
        "source_context"
    ) or state_context(cfg)
    if selected & {"delete", "dedup"}:
        current = collect_snapshot(cfg, log, stopped)
        if state_context(cfg, current["items"]) != context:
            raise ValueError("实际媒体库或观看用户发生变化，请重新生成审核计划")
        for item in current["items"]:
            if item.path:
                current_by_path.setdefault(_key(item.path), []).append(item)
        if stopped():
            report["status"] = "cancelled"
            return report
    claim_plan(plan)
    rows = []
    report["operations"] = rows
    report["status"] = "succeeded"
    for action in plan["actions"]:
        if stopped():
            report["status"] = "cancelled"
            break
        kind = action["kind"]
        if kind not in selected:
            continue
        for path, expected in action["file_identities"].items():
            if expected is None or file_identity(path, stopped) != expected:
                raise ValueError(f"计划文件发生变化，请重新审核：{path}")
        if action.get("target") and file_identity(action["target"], stopped) != action.get(
            "target_identity"
        ):
            raise ValueError(f"计划目标发生变化，请重新审核：{action['target']}")
        detail = ""
        if kind in ("delete", "dedup"):
            labels = LabelStore(_data_dir() / "smart.db", context=context).all()
            labels = {
                **legacy_protection_labels(_data_dir() / "smart.db", current["items"], labels),
                **labels,
            }
            entities = current_by_path.get(_key(action["path"]), [])
            if not entities:
                raise ValueError("当前媒体库缺少计划条目，请重新采集并审核")
            if any(it.id in labels or _has_protected_activity(it) for it in entities):
                rows.append(
                    {
                        "id": action["id"],
                        "kind": kind,
                        "state": "protected",
                        "path": action["path"],
                        "detail": "最新人工标签或观看状态保护了同一物理文件",
                    }
                )
                write_json_atomic(_data_dir() / "operation_results" / f"{plan['id']}.json", rows)
                continue
            ok, detail = trash_file(action["path"], should_stop=stopped)
            if ok:
                DeleteJournal(_data_dir() / "automation_delete_journal.db").record(
                    action["path"], action["size"], "auto_" + kind
                )
        elif kind == "merge":
            options = {
                **_merge_options(cfg),
                "planned_output": action["target"],
                "source_hashes": {
                    p: identity[-1] for p, identity in action["file_identities"].items()
                },
            }
            ok, detail = merge_group_ffmpeg(
                _group_from_dict(action["group"]), ffmpeg, ffprobe, options, log, stopped
            )
        elif kind == "replace":
            detail = execute_replacements([action["entry"]], should_stop=stopped, log=log)
            ok = detail["fail"] == 0 and not detail["cancelled"]
        elif kind == "nfo_fix":
            detail = execute_nfo_fixes([action["entry"]], should_stop=stopped, log=log)
            ok = detail["fail"] == 0 and not detail["cancelled"]
        else:
            detail = clean_jellyfin(
                cfg.jellyfin_server, cfg.jellyfin_api_key, False, log, stopped, [action["entry"]]
            )
            ok = not detail["error"] and not detail["cancelled"]
        rows.append(
            {
                "id": action["id"],
                "kind": kind,
                "path": action.get("path", ""),
                "state": "succeeded" if ok else "cancelled" if stopped() else "failed",
                "detail": detail,
            }
        )
        write_json_atomic(_data_dir() / "operation_results" / f"{plan['id']}.json", rows)
        if progress:
            progress(int(len(rows) / max(1, len(plan["actions"])) * 100))
        if not ok:
            report.update(status="cancelled" if stopped() else "failed", error=str(detail))
            break
    if stopped():
        report["status"] = "cancelled"
    report["operations"] = rows
    for kind in selected:
        matching = [r for r in rows if r["kind"] == kind]
        report["stages"][kind + "_apply"] = {
            "ok": sum(r["state"] == "succeeded" for r in matching),
            "fail": sum(r["state"] == "failed" for r in matching),
            "results": matching,
        }
    if (
        any(r["kind"] == "jellyfin_clean" and r["state"] == "succeeded" for r in rows)
        and report["status"] == "succeeded"
        and opts.get("refresh_library", True)
    ):
        ok, detail = jellyfin_api.refresh_library(cfg.jellyfin_server, cfg.jellyfin_api_key)
        if not ok:
            report.update(status="failed", error=detail)
    return report


def run_orchestrator(cfg, options=None, log=print, progress=None, should_stop=None):
    opts, stopped = options or {}, should_stop or (lambda: False)
    report = {"status": "planned", "error": "", "stages": {}, "summary": {}}
    try:
        if stopped():
            report["status"] = "cancelled"
            return report
        ffmpeg, ffprobe = resolve_tools(cfg, opts.get("ffmpeg", ""), opts.get("ffprobe", ""))
        if opts.get("approved_plan_id"):
            plan = load_plan(opts["approved_plan_id"], cfg, stopped)
            report.update(plan.get("report") or {})
            report["plan"] = plan
            frozen_tools = report.get("tools") or {}
            ffmpeg = frozen_tools.get("ffmpeg", ffmpeg)
            ffprobe = frozen_tools.get("ffprobe", ffprobe)
            return _execute_plan(cfg, opts, plan, report, ffmpeg, ffprobe, stopped, log, progress)
        try:
            snapshot = collect_snapshot(cfg, log, stopped)
        except Exception as exc:
            if opts.get("roots") is None or opts.get("apply_delete") or opts.get("apply_dedup"):
                raise
            report["warnings"] = [f"媒体库采集不可用，仅处理手动目录；未生成评分清理建议：{exc}"]
            snapshot = {"items": [], "paths": [], "summary": {}}
        raw = opts.get("roots") if opts.get("roots") is not None else snapshot["paths"]
        paths = list(
            iter_media_files(raw, cfg.video_extensions, cfg.exclude_path_keywords, stopped, log)
        )
        scoped = {_key(path) for path in paths}
        items = (
            [it for it in snapshot["items"] if it.path and _key(it.path) in scoped]
            if opts.get("roots") is not None
            else snapshot["items"]
        )
        local = sum(bool(it.path and Path(it.path).is_file()) for it in items)
        report["stages"]["collect"] = {
            "paths": len(paths),
            "items": len(items),
            "from_db": opts.get("roots") is None,
            "local_items": local,
            "remote_items": len(items) - local,
            "can_modify_local": bool(local),
        }
        if len(items) > local:
            report.setdefault("warnings", []).append(
                f"{len(items) - local} 项仅可读取元数据，本地文件操作不可用；请检查路径映射。"
            )
        if stopped():
            report["status"] = "cancelled"
            return report
        scoring = score_items(items, log, cfg)
        from utils.state_context import bind_resolved_context

        bind_resolved_context(cfg, scoring)
        report["stages"]["score"] = {
            "labels": len(scoring["labels"]),
            "counts": scoring["counts"],
            "source_context": scoring.get("source_context", ""),
            "resolved_user_id": scoring.get("resolved_user_id", ""),
            "resolved_source_identity": scoring.get("resolved_source_identity", ""),
        }
        missing = find_missing_subtitles(paths, cfg.subtitle_extensions, cfg.whisper_output_dir)
        report["stages"]["subtitles"] = {"missing": len(missing), "items": missing}
        report["stages"]["repair_report"] = (
            repair_report(paths, log, ffmpeg, ffprobe, stopped)
            if opts.get("scan_video")
            else {"skipped": True}
        )
        merge_plan = build_merge_plan(
            paths, ffmpeg, ffprobe, _merge_options(cfg), progress=progress, should_stop=stopped
        )
        report["stages"]["merge_plan"] = {
            "total_groups": len(merge_plan.groups),
            **{
                status: sum(g.status == status for g in merge_plan.groups)
                for status in (
                    "ready",
                    "needs_review",
                    "skip_variant",
                    "skip_duplicate",
                    "conflict",
                )
            },
        }
        if opts.get("check_old") and not stopped():
            report["stages"]["old_merge_health"] = check_old_merges(ffmpeg, ffprobe, log, stopped)
        if stopped():
            report["status"] = "cancelled"
            return report
        actions, selected, duplicates = _prepare_actions(
            cfg, opts, paths, items, scoring, merge_plan, stopped
        )
        report["stages"]["dedup_report"] = {
            "groups": len(duplicates),
            "candidates": sum(len(g["losers"]) for g in duplicates if not g["all_cd"]),
        }
        report["summary"] = {
            "library_files": len(paths),
            "library_items": len(items),
            "score_counts": scoring["counts"],
            "missing_subtitles": len(missing),
            "dedup_groups": len(duplicates),
            "merge_ready": report["stages"]["merge_plan"]["ready"],
            "applied": [k for k, v in opts.items() if k.startswith("apply_") and v],
        }
        report["tools"] = {"ffmpeg": ffmpeg, "ffprobe": ffprobe}
        log(f"正在冻结 {len(actions)} 项操作的文件身份；文件较大时校验需要时间，可随时停止。")
        plan = save_plan(cfg, actions, selected, report=report, should_stop=stopped)
        report["plan"] = plan
        if any(opts.get("apply_" + kind) for kind in selected):
            return _execute_plan(cfg, opts, plan, report, ffmpeg, ffprobe, stopped, log, progress)
    except Exception as exc:
        report.update(status="cancelled" if stopped() else "failed", error=str(exc))
        log(f"自动化失败：{exc}")
    return report
