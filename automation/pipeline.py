"""无界面自动化联动流水线 - 新模块，不修改原有页面/worker。

设计目标：把现有工具串成一条可无人值守的执行链：
  1. 从 Jellyfin 数据库采集视频清单
  2. 按番号构建分集合并计划
  3. 仅对 status == ready 的组执行无损合并（默认 dry-run，不触碰原文件）
  4. 校验已经完成替换的旧合并是否损坏
  5. 汇总为机器可读报告

安全红线：
  - 合并只产出“番号 合并.<容器>”，默认绝不移动/覆盖原分集；
  - 只有显式传 apply_merge=True 才执行合并，且复用原有 temp -> os.replace 原子落位；
  - 旧合并检测只做不解码的 demux/remux，不占用 CPU 解码。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from config import ToolkitConfig
from utils import library_source
from utils.merge import (
    EpisodeGroup,
    MergePlan,
    build_concat_paths,
    build_merge_plan,
    choose_output_container,
    display_number_for_group,
    scan_files,
    unique_output_path,
    validate_merged_output,
    validate_merge_selection,
)
from utils.merge_archive import load_journal
from utils.merge_repair import archived_merge_items, validate_merged_video_quick


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def resolve_tools(cfg: ToolkitConfig, ffmpeg_arg: str = "",
                  ffprobe_arg: str = "") -> tuple[str, str]:
    """解析 ffmpeg/ffprobe：显式参数优先，其次配置，最后 PATH。"""
    from workers.ffmpeg import find_binary

    ffmpeg = ffmpeg_arg or getattr(cfg, "merge_ffmpeg_path", "") or find_binary("ffmpeg") or ""
    ffprobe = ffprobe_arg or ""
    if not ffprobe:
        if ffmpeg:
            candidate = Path(ffmpeg).with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
            if candidate.exists():
                ffprobe = str(candidate)
        ffprobe = ffprobe or find_binary("ffprobe") or ""
    return ffmpeg, ffprobe


def _video_map_spec(part) -> str:
    idx = getattr(part.probe, "video_stream_index", -1)
    return f"0:{idx}" if idx >= 0 else "0:v:0"


def _audio_map_spec(part) -> str:
    idx = getattr(part.probe, "audio_stream_index", -1)
    return f"0:{idx}?" if idx >= 0 else "0:a:0?"


def _needs_normalize(part, ext: str) -> bool:
    """非 MP4 容器、attached_pic、真实视频流非首路或 time_base 非标准时需 remux。"""
    if ext not in ("mp4", "m4v", "mov"):
        return True
    probe = getattr(part, "probe", None)
    if probe is None:
        return True
    if getattr(probe, "container", "") not in ("mp4", "m4v", "mov"):
        return True
    if getattr(probe, "has_attached_pic", False):
        return True
    if getattr(probe, "video_stream_index", -1) not in (-1, 0):
        return True
    if getattr(probe, "video_time_base", "") != "1/90000":
        return True
    return False


def _run_cmd(cmd: list, timeout: int) -> tuple[bool, str]:
    """执行 ffmpeg 子进程，返回 (是否成功, stderr 摘要)。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creationflags(),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        out, err = proc.communicate()
        return False, "合并超时"
    if proc.returncode == 0:
        return True, (err or "").strip()
    detail = (err or out or "").strip()
    return False, f"ffmpeg 合并失败：{detail[-800:]}"


def merge_group_ffmpeg(group: EpisodeGroup, ffmpeg: str, ffprobe: str,
                       options: Optional[dict] = None,
                       log: Callable[[str], None] = print) -> tuple[bool, str]:
    """对单个 ready 组执行无损合并。返回 (ok, 输出路径或错误)。

    完整复制 workers.merge.MergeWorker._merge_group 的实现，仅去掉 Qt 信号，
    便于无界面执行。
    """
    options = options or {}
    timeout = max(30, int(options.get("merge_timeout_per_file", 3600)))
    partial = intermediate = list_file = None
    norm_files: list[Path] = []
    try:
        selected = set(group.selected_paths)
        parts = [p for p in group.parts if p.path in selected]
        parts.sort(key=lambda p: p.part_index)
        if len(parts) < 2:
            return False, "分集不足"
        valid, reason = validate_merge_selection(parts)
        if not valid:
            return False, reason
        out_dir = Path(parts[0].path).parent
        ext = choose_output_container(parts, options.get("merge_output_container", "mp4"))
        label = display_number_for_group(group) or group.number
        final = unique_output_path(out_dir / f"{label} 合并.{ext}")
        partial = out_dir / f"{label} 合并.partial.{ext}"
        list_file = out_dir / f"{label} 合并.concat.txt"

        concat_inputs: list[str] = []
        for i, p in enumerate(parts, 1):
            if not _needs_normalize(p, ext):
                concat_inputs.append(p.path)
                continue
            norm = out_dir / f"{label} 合并.norm{i}.{ext}"
            norm_cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", p.path,
                "-map", _video_map_spec(p), "-map", _audio_map_spec(p),
                "-c", "copy",
            ]
            if ext in ("mp4", "m4v", "mov"):
                norm_cmd += ["-video_track_timescale", "90000"]
            norm_cmd.append(str(norm))
            log(f"规范化分集 {label} {i}/{len(parts)}")
            ok, err = _run_cmd(norm_cmd, timeout)
            if not ok:
                return False, err or "规范化分集失败"
            if not norm.exists() or norm.stat().st_size <= 0:
                return False, "规范化分集输出为空"
            norm_files.append(norm)
            concat_inputs.append(str(norm))

        build_concat_paths(concat_inputs, str(list_file))

        if ext in ("mp4", "m4v", "mov"):
            concat_cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", "-movflags", "+faststart",
                str(partial),
            ]
            log(f"合并 {label}（{len(parts)} 个分集 → {ext}）")
            ok, err = _run_cmd(concat_cmd, timeout)
        else:
            intermediate = out_dir / f"{label} 合并.inter.mkv"
            concat_cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", str(intermediate),
            ]
            log(f"合并 {label}（{len(parts)} 个分集 → 中间容器）")
            ok, err = _run_cmd(concat_cmd, timeout)
            if not ok:
                return False, err or "生成中间容器失败"
            if not intermediate.exists() or intermediate.stat().st_size <= 0:
                return False, "中间容器为空"
            remux_cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(intermediate),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", str(partial),
            ]
            log(f"生成最终文件 {label}.{ext}")
            ok, err = _run_cmd(remux_cmd, timeout)

        if not ok:
            return False, err or "封装为最终容器失败"
        if not partial.exists() or partial.stat().st_size <= 0:
            return False, "最终文件为空"

        if ffprobe:
            ok, err = validate_merged_output(ffprobe, str(partial), parts, timeout=timeout)
            if not ok:
                return False, err or "输出元数据校验失败"
        else:
            check_cmd = [
                ffmpeg, "-v", "error", "-i", str(partial),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c", "copy", "-f", "null", "-",
            ]
            ok, err = _run_cmd(check_cmd, timeout)
            if not ok:
                return False, f"容器结构校验失败：{err or '未知错误'}"

        os.replace(str(partial), str(final))
        return True, str(final)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        for p in norm_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        for p in (partial, intermediate, list_file):
            if p is None:
                continue
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def collect_paths_from_db(cfg: ToolkitConfig, log: Callable[[str], None] = print) -> list[str]:
    """从媒体库读取全库视频路径，避免手动指定目录。

    来源由 ``utils.library_source`` 决定（本地库文件 / 服务器 API），Jellyfin 与 Emby 通用。
    """
    try:
        items = library_source.collect(
            cfg,
            log=log,
            exclude_keywords=getattr(cfg, "exclude_path_keywords", None),
        )
    except Exception as e:  # noqa: BLE001
        log(f"全库采集失败：{e}")
        return []
    return [it.path for it in items if it.path and Path(it.path).exists()]


def _merge_options(cfg: ToolkitConfig) -> dict:
    return {
        "merge_output_container": getattr(cfg, "merge_output_container", "mp4"),
        "merge_ffmpeg_path": getattr(cfg, "merge_ffmpeg_path", ""),
        "merge_rename_to_number": getattr(cfg, "merge_rename_to_number", True),
        "merge_dup_frame_count": getattr(cfg, "merge_dup_frame_count", 9),
        "merge_dup_hash_distance": getattr(cfg, "merge_dup_hash_distance", 10),
        "merge_dup_sim_threshold": getattr(cfg, "merge_dup_sim_threshold", 0.8),
        "merge_max_parts": getattr(cfg, "merge_max_parts", 20),
        "merge_min_part_seconds": getattr(cfg, "merge_min_part_seconds", 3.0),
        "merge_timeout_per_file": getattr(cfg, "merge_timeout_per_file", 3600),
        "exclude_path_keywords": getattr(cfg, "exclude_path_keywords", None),
    }


def plan_merges(paths: list[str], ffmpeg: str, ffprobe: str,
                options: dict, log: Callable[[str], None] = print,
                progress: Optional[Callable[[int], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None) -> MergePlan:
    """扫描路径并生成分集合并计划。"""
    if not paths:
        return MergePlan(groups=[], unrecognized=[], stats={})
    files = scan_files(paths, exclude_keywords=options.get("exclude_path_keywords"))
    return build_merge_plan(
        files, ffmpeg, ffprobe, options,
        progress=progress, should_stop=should_stop,
    )


def check_old_merges(ffmpeg: str, ffprobe: str,
                     log: Callable[[str], None] = print) -> list[dict]:
    """不解码地校验历史合并结果，返回损坏项列表。"""
    damaged: list[dict] = []
    try:
        items = archived_merge_items(load_journal())
    except Exception as e:  # noqa: BLE001
        log(f"读取合并归档失败：{e}")
        return damaged
    for item in items:
        final_path = item.get("final_path") or ""
        number = item.get("number") or ""
        ok, detail = validate_merged_video_quick(
            ffmpeg, ffprobe, final_path, item.get("media_entries") or [],
        )
        log(f"旧合并校验 {number}: {'正常' if ok else '损坏'} - {detail}")
        if not ok:
            damaged.append({"number": number, "final_path": final_path, "detail": detail})
    return damaged


def run_pipeline(cfg: ToolkitConfig, roots: Optional[list[str]] = None,
                 ffmpeg: str = "", ffprobe: str = "", apply_merge: bool = False,
                 check_old: bool = False,
                 log: Callable[[str], None] = print,
                 progress: Optional[Callable[[int], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None) -> dict:
    """执行自动化联动流水线，返回报告 dict。"""
    ffmpeg, ffprobe = resolve_tools(cfg, ffmpeg, ffprobe)
    options = _merge_options(cfg)
    paths = list(roots or []) or collect_paths_from_db(cfg, log)
    files = scan_files(paths, exclude_keywords=options.get("exclude_path_keywords"))
    plan = build_merge_plan(
        files, ffmpeg, ffprobe, options,
        progress=progress, should_stop=should_stop,
    )

    merge_results: list[dict] = []
    executed = failed = 0
    for group in plan.groups:
        if group.status != "ready":
            continue
        if not apply_merge:
            merge_results.append({
                "number": group.number,
                "action": "planned",
                "paths": list(group.selected_paths),
            })
            continue
        ok, output = merge_group_ffmpeg(group, ffmpeg, ffprobe, options, log)
        if ok:
            executed += 1
            merge_results.append({"number": group.number, "action": "merged", "output": output})
        else:
            failed += 1
            merge_results.append({"number": group.number, "action": "failed", "error": output})

    old_damaged = check_old_merges(ffmpeg, ffprobe, log) if check_old else []

    report = {
        "paths": len(files),
        "merge": {
            "total_groups": len(plan.groups),
            "ready": sum(1 for g in plan.groups if g.status == "ready"),
            "needs_review": sum(1 for g in plan.groups if g.status == "needs_review"),
            "skip_variant": sum(1 for g in plan.groups if g.status == "skip_variant"),
            "skip_duplicate": sum(1 for g in plan.groups if g.status == "skip_duplicate"),
            "conflict": sum(1 for g in plan.groups if g.status == "conflict"),
            "skip": sum(1 for g in plan.groups if g.status == "skip"),
            "executed": executed,
            "failed": failed,
            "results": merge_results,
        },
        "old_merges": {
            "damaged": len(old_damaged),
            "items": old_damaged,
        },
        "apply_merge": apply_merge,
    }
    return report
