"""Headless merge workflow using the same discovery and executor as the GUI."""

from __future__ import annotations

from pathlib import Path
import sys

from config import ToolkitConfig
from utils import library_source, tools
from utils.file_scan import iter_media_files
from utils.merge import MergePlan, build_merge_plan
from utils.merge_archive import load_journal
from utils.merge_repair import archived_merge_items, validate_merged_video_quick


def resolve_tools(cfg: ToolkitConfig, ffmpeg_arg="", ffprobe_arg=""):
    configured = ffmpeg_arg or getattr(cfg, "merge_ffmpeg_path", "")
    tools.configure_tools(configured, force=True)
    ffmpeg = configured or tools.find_binary("ffmpeg") or ""
    ffprobe = ffprobe_arg
    if not ffprobe and ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sibling.is_file():
            ffprobe = str(sibling)
    return ffmpeg, ffprobe or tools.find_binary("ffprobe") or ""


def merge_group_ffmpeg(group, ffmpeg, ffprobe, options=None, log=print, should_stop=None):
    from utils.merge_executor import merge_group_ffmpeg as execute

    return execute(group, ffmpeg, ffprobe, options, log, should_stop)


def collect_paths_from_db(cfg, log=print, should_stop=None):
    items = library_source.collect(
        cfg,
        log=log,
        should_stop=should_stop,
        exclude_keywords=getattr(cfg, "exclude_path_keywords", None),
    )
    return [it.path for it in items if it.path and Path(it.path).is_file()]


def _merge_options(cfg):
    names = (
        "merge_output_container",
        "merge_ffmpeg_path",
        "merge_rename_to_number",
        "merge_dup_frame_count",
        "merge_dup_hash_distance",
        "merge_dup_sim_threshold",
        "merge_max_parts",
        "merge_min_part_seconds",
        "merge_timeout_per_file",
        "exclude_path_keywords",
        "video_extensions",
    )
    return {name: getattr(cfg, name) for name in names}


def plan_merges(paths, ffmpeg, ffprobe, options, log=print, progress=None, should_stop=None):
    files = list(
        iter_media_files(
            paths,
            options.get("video_extensions"),
            options.get("exclude_path_keywords"),
            should_stop,
            log,
        )
    )
    if not files:
        return MergePlan(groups=[], unrecognized=[], stats={})
    return build_merge_plan(
        files, ffmpeg, ffprobe, options, progress=progress, should_stop=should_stop
    )


def check_old_merges(ffmpeg, ffprobe, log=print, should_stop=None):
    if not ffmpeg or not ffprobe:
        raise ValueError("旧合并体检需要有效的 ffmpeg 和 ffprobe")
    damaged = []
    for item in archived_merge_items(load_journal()):
        if should_stop and should_stop():
            break
        path = item.get("final_path") or ""
        ok, detail = validate_merged_video_quick(
            ffmpeg, ffprobe, path, item.get("media_entries") or []
        )
        log(f"旧合并校验 {item.get('number', '')}: {detail}")
        if not ok:
            damaged.append({"number": item.get("number", ""), "final_path": path, "detail": detail})
    return damaged


def run_pipeline(
    cfg,
    roots=None,
    ffmpeg="",
    ffprobe="",
    apply_merge=False,
    check_old=False,
    log=print,
    progress=None,
    should_stop=None,
):
    stopped = should_stop or (lambda: False)
    report = {
        "status": "planned",
        "error": "",
        "paths": 0,
        "apply_merge": apply_merge,
        "merge": {"total_groups": 0, "executed": 0, "failed": 0, "results": []},
        "old_merges": {"damaged": 0, "items": []},
    }
    try:
        if stopped():
            report["status"] = "cancelled"
            return report
        ffmpeg, ffprobe = resolve_tools(cfg, ffmpeg, ffprobe)
        options = _merge_options(cfg)
        paths = roots if roots is not None else collect_paths_from_db(cfg, log, stopped)
        files = list(
            iter_media_files(paths, cfg.video_extensions, cfg.exclude_path_keywords, stopped, log)
        )
        report["paths"] = len(files)
        plan = build_merge_plan(
            files, ffmpeg, ffprobe, options, progress=progress, should_stop=stopped
        )
        merge = report["merge"]
        merge["total_groups"] = len(plan.groups)
        for status in (
            "ready",
            "needs_review",
            "skip_variant",
            "skip_duplicate",
            "conflict",
            "skip",
        ):
            merge[status] = sum(g.status == status for g in plan.groups)
        for group in plan.groups:
            if stopped():
                break
            if group.status != "ready":
                continue
            if not apply_merge:
                merge["results"].append(
                    {
                        "number": group.number,
                        "action": "planned",
                        "paths": list(group.selected_paths),
                    }
                )
                continue
            ok, output = merge_group_ffmpeg(group, ffmpeg, ffprobe, options, log, stopped)
            merge["results"].append(
                {
                    "number": group.number,
                    "action": "merged" if ok else "failed",
                    "output" if ok else "error": output,
                }
            )
            merge["executed" if ok else "failed"] += 1
            if not ok:
                report["error"] = output
                break
        if check_old and not stopped() and not merge["failed"]:
            damaged = check_old_merges(ffmpeg, ffprobe, log, stopped)
            report["old_merges"] = {"damaged": len(damaged), "items": damaged}
        report["status"] = (
            "cancelled"
            if stopped()
            else "failed"
            if merge["failed"]
            else "succeeded"
            if apply_merge
            else "planned"
        )
    except Exception as exc:
        report.update(status="cancelled" if stopped() else "failed", error=str(exc))
        log(f"流水线失败：{exc}")
    return report
