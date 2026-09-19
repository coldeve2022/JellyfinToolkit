"""整体自动化编排器 - 新模块，不改动原有页面/worker。

把全软件功能串成一条共享快照的流水线，默认只做只读审计，写操作全部显式开关：
  1. 采集 Jellyfin 库
  2. 评分分层
  3. 字幕缺失报告
  4. 去重报告
  5. 修复健康报告（可选，逐文件 ffprobe/ffmpeg）
  6. 分集合并计划
  7. 旧合并体检（可选）
  8. 分集合并执行（无损，仅生成新文件）
  9. NFO 关联修复执行
 10. 破解视频替换执行
 11. 去重执行（移入回收站）
 12. 建议删除执行（移入回收站）
 13. Jellyfin 脏标签清洗执行（先备份再清洗）
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from config import DATA_DIR, ToolkitConfig
from utils import jellyfin_api, library_source
from utils.labels import LabelStore
from utils.library import summarize
from utils.naming import clean_restored_name
from utils.nfo import fix_nfo_content, nfo_new_filename, read_text_any_encoding, safe_rename
from utils.scoring import DEC_DELETE, dedup_groups
from utils.scoring_v2 import decision_counts, rank_items
from utils.trash import DeleteJournal, trash_file

from automation.pipeline import (
    _merge_options,
    build_merge_plan,
    check_old_merges,
    merge_group_ffmpeg,
    resolve_tools,
)


def _progress(progress: Optional[Callable[[int], None]], value: int) -> None:
    if progress:
        try:
            progress(max(0, min(100, int(value))))
        except Exception:  # noqa: BLE001
            pass


def collect_snapshot(cfg: ToolkitConfig, log: Callable[[str], None]) -> dict:
    """采集并汇总全库，作为后续所有阶段的共享快照。

    数据来源由 ``utils.library_source`` 按配置决定（本地库文件 / 服务器 API），
    因此自动化流程对 Jellyfin 与 Emby 一视同仁。
    """
    try:
        items = library_source.collect(
            cfg, log=log,
            exclude_keywords=getattr(cfg, "exclude_path_keywords", None),
        )
    except Exception as e:  # noqa: BLE001  采集不了不该中断整个自动化流程
        log(f"库数据采集失败，跳过全库采集：{e}")
        return {"items": [], "paths": [], "summary": {}}
    paths = [it.path for it in items if it.path and Path(it.path).exists()]
    return {"items": items, "paths": paths, "summary": summarize(items)}


def score_items(items: list, log: Callable[[str], None]) -> dict:
    """评分分层：返回 results / counts / labels。"""
    labels = LabelStore(DATA_DIR / "smart.db").all()
    if not items:
        return {"results": [], "counts": {}, "labels": labels}
    results = rank_items(items, labels)
    counts = decision_counts(results)
    log(f"评分完成：{counts}")
    return {"results": results, "counts": counts, "labels": labels}


def find_missing_subtitles(paths: list[str], subtitle_extensions) -> list[str]:
    """找出没有同名字幕的视频路径。"""
    missing: list[str] = []
    for path in paths:
        base = os.path.splitext(path)[0]
        if not any(os.path.exists(base + ext) for ext in subtitle_extensions):
            missing.append(path)
    return missing


def fix_nfo_directory(directory: str, log: Callable[[str], None] = print,
                      should_stop: Optional[Callable[[], bool]] = None) -> tuple[int, int]:
    """复制 NFOWorker 的核心逻辑：修正 NFO 内容并重命名关联文件。"""
    fixed = renamed = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if should_stop and should_stop():
                return fixed, renamed
            old_path = os.path.join(root, name)
            new_name = nfo_new_filename(name)
            if new_name and new_name != name:
                new_path = os.path.join(root, new_name)
                try:
                    safe_rename(old_path, new_path)
                    log(f"[重命名] {name} -> {new_name}")
                    renamed += 1
                    old_path = new_path
                except OSError as e:
                    log(f"[重命名失败] {name}: {e}")
            if old_path.lower().endswith(".nfo"):
                content = read_text_any_encoding(old_path)
                if content is None:
                    log(f"[跳过] 无法解码 {name}")
                    continue
                new_content = fix_nfo_content(content)
                if new_content is not None:
                    try:
                        with open(old_path, "w", encoding="utf-8") as fh:
                            fh.write(new_content)
                        log(f"[修正] {name}")
                        fixed += 1
                    except OSError as e:
                        log(f"[写入失败] {name}: {e}")
    return fixed, renamed


def replace_restored(restored_paths: list[str], library_paths: list[str],
                     marker: str, log: Callable[[str], None] = print,
                     should_stop: Optional[Callable[[], bool]] = None) -> tuple[int, int]:
    """复制 ReplaceWorker 的核心逻辑：备份 -> 落位 -> 成功删备份，失败回滚。"""
    video_exts = {".mp4", ".mkv", ".avi", ".ts", ".mov", ".wmv"}
    original_index: dict[str, str] = {}

    def _walk(paths):
        for p in paths:
            if should_stop and should_stop():
                return
            path = Path(p)
            if path.is_file() and path.suffix.lower() in video_exts:
                original_index[path.name] = str(path)
            elif path.is_dir():
                for f in path.rglob("*"):
                    if should_stop and should_stop():
                        return
                    if f.is_file() and f.suffix.lower() in video_exts:
                        original_index[f.name] = str(f)

    _walk(library_paths)

    tasks: list[str] = []
    for p in restored_paths:
        path = Path(p)
        if path.is_file():
            tasks.append(str(path))
        elif path.is_dir():
            for f in path.rglob("*"):
                if should_stop and should_stop():
                    break
                if f.is_file():
                    tasks.append(str(f))

    replaced = not_found = 0
    for src in tasks:
        if should_stop and should_stop():
            return replaced, not_found
        filename = os.path.basename(src)
        if marker.lower() not in filename.lower():
            continue
        if not filename.lower().endswith(".mp4"):
            continue
        clean_name = clean_restored_name(filename, marker)
        if not clean_name:
            not_found += 1
            continue
        target = original_index.get(clean_name)
        if not target:
            not_found += 1
            continue
        bak = target + ".bak"
        try:
            if os.path.exists(bak):
                os.remove(bak)
            if os.path.exists(target):
                shutil.move(target, bak)
            shutil.move(src, target)
            if os.path.exists(bak):
                os.remove(bak)
            log(f"替换成功：{filename} -> {target}")
            replaced += 1
            original_index.pop(clean_name, None)
        except Exception as e:  # noqa: BLE001
            if os.path.exists(bak) and not os.path.exists(target):
                try:
                    shutil.move(bak, target)
                except Exception:  # noqa: BLE001
                    pass
            log(f"替换失败(已保留原文件)：{filename} - {e}")
            not_found += 1
    return replaced, not_found


def clean_jellyfin(server: str, api_key: str, refresh: bool,
                   log: Callable[[str], None] = print) -> dict:
    """复制 JellyfinCleanWorker：扫描 -> 备份 -> 清洗 -> 可选刷新。"""
    if not server or not api_key:
        return {"scanned": 0, "cleaned": 0, "backup": "", "error": "未配置 Jellyfin API"}
    dirty = jellyfin_api.scan_dirty(server, api_key, log=log)
    if not dirty:
        return {"scanned": 0, "cleaned": 0, "backup": "", "error": ""}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = str(DATA_DIR / f"jellyfin_clean_backup_{ts}.json")
    jellyfin_api.save_dirty_backup(dirty, backup)
    cleaned = jellyfin_api.apply_clean(server, api_key, dirty, log=log)
    refresh_ok = False
    if refresh:
        refresh_ok, _msg = jellyfin_api.refresh_library(server, api_key)
    return {"scanned": len(dirty), "cleaned": cleaned, "backup": backup,
            "refresh_ok": refresh_ok, "error": ""}


def repair_report(paths: list[str], log: Callable[[str], None] = print) -> dict:
    """逐文件分析损坏/兼容性（CPU 较重的可选阶段）。"""
    from workers.ffmpeg import analyze_video

    corrupted: list[str] = []
    incompatible: list[str] = []
    for path in paths:
        info = analyze_video(path)
        if info.get("is_corrupted"):
            corrupted.append(path)
        elif not info.get("is_compatible"):
            incompatible.append(path)
    log(f"视频健康检查完成：损坏 {len(corrupted)}，不兼容 {len(incompatible)}")
    return {"corrupted": corrupted, "incompatible": incompatible}


def run_orchestrator(cfg: ToolkitConfig, options: Optional[dict] = None,
                     log: Callable[[str], None] = print,
                     progress: Optional[Callable[[int], None]] = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> dict:
    opts = options or {}
    ffmpeg, ffprobe = resolve_tools(
        cfg, opts.get("ffmpeg", ""), opts.get("ffprobe", ""))
    merge_options = _merge_options(cfg)

    report: dict = {"stages": {}, "summary": {}}

    snapshot = collect_snapshot(cfg, log)
    items = snapshot["items"]
    paths = list(opts.get("roots") or snapshot["paths"])
    from_db = not bool(opts.get("roots"))
    report["stages"]["collect"] = {"paths": len(paths), "from_db": from_db}

    scoring = score_items(items, log)
    results = scoring["results"]
    labels = scoring["labels"]
    report["stages"]["score"] = {"labels": len(labels), "counts": scoring["counts"]}

    missing_subtitles = find_missing_subtitles(paths, getattr(cfg, "subtitle_extensions", []))
    report["stages"]["subtitles"] = {"missing": len(missing_subtitles),
                                     "items": missing_subtitles[:200]}

    dup_groups = dedup_groups(items)
    dedup_candidates = sum(
        len(g["items"]) - 1 for g in dup_groups if not g["all_cd"])
    report["stages"]["dedup_report"] = {"groups": len(dup_groups),
                                        "candidates": dedup_candidates}

    if opts.get("scan_video"):
        report["stages"]["repair_report"] = repair_report(paths, log)
    else:
        report["stages"]["repair_report"] = {"skipped": True}

    merge_plan = build_merge_plan(
        paths, ffmpeg, ffprobe, merge_options, log=log,
        progress=progress, should_stop=should_stop,
    )
    report["stages"]["merge_plan"] = {
        "total_groups": len(merge_plan.groups),
        "ready": sum(1 for g in merge_plan.groups if g.status == "ready"),
        "needs_review": sum(1 for g in merge_plan.groups if g.status == "needs_review"),
        "skip_variant": sum(1 for g in merge_plan.groups if g.status == "skip_variant"),
        "skip_duplicate": sum(1 for g in merge_plan.groups if g.status == "skip_duplicate"),
        "conflict": sum(1 for g in merge_plan.groups if g.status == "conflict"),
    }

    if opts.get("check_old"):
        report["stages"]["old_merge_health"] = check_old_merges(ffmpeg, ffprobe, log)

    if opts.get("apply_merge"):
        merged = failed = 0
        merge_results: list[dict] = []
        ready_groups = [g for g in merge_plan.groups if g.status == "ready"]
        total_merge = len(ready_groups)
        for i, group in enumerate(ready_groups, 1):
            ok, output = merge_group_ffmpeg(group, ffmpeg, ffprobe, merge_options, log)
            if ok:
                merged += 1
                merge_results.append({"number": group.number, "action": "merged", "output": output})
            else:
                failed += 1
                merge_results.append({"number": group.number, "action": "failed", "error": output})
            _progress(progress, int(i / total_merge * 100) if total_merge else 100)
        report["stages"]["merge_apply"] = {
            "executed": merged, "failed": failed, "results": merge_results,
        }

    if opts.get("apply_nfo_fix"):
        nfo_fixed = nfo_renamed = 0
        for directory in opts.get("nfo_dirs") or []:
            f, r = fix_nfo_directory(directory, log, should_stop)
            nfo_fixed += f
            nfo_renamed += r
        report["stages"]["nfo_fix_apply"] = {"fixed": nfo_fixed, "renamed": nfo_renamed}

    if opts.get("apply_replace"):
        replaced, not_found = replace_restored(
            opts.get("restored_paths") or [],
            opts.get("library_paths") or [],
            getattr(cfg, "restored_suffix_marker", "restored"),
            log, should_stop,
        )
        report["stages"]["replace_apply"] = {"replaced": replaced, "not_found": not_found}

    if opts.get("apply_dedup"):
        journal = DeleteJournal(DATA_DIR / "automation_delete_journal.db")
        ok = fail = 0
        fails: list = []
        for group in dup_groups:
            if group["all_cd"]:
                continue
            for it in group["items"]:
                if it.id in labels or it.id not in group["losers"] or not it.path:
                    continue
                succ, err = trash_file(it.path)
                if succ:
                    ok += 1
                    journal.record(it.path, it.size, "auto_dedup")
                else:
                    fail += 1
                    fails.append((it.path, err))
        report["stages"]["dedup_apply"] = {"ok": ok, "fail": fail, "fails": fails}

    if opts.get("apply_delete"):
        journal = DeleteJournal(DATA_DIR / "automation_delete_journal.db")
        ok = fail = 0
        fails: list = []
        for r in results:
            if r.decision != DEC_DELETE:
                continue
            if r.is_seen or r.is_junk or r.item.id in labels or not r.item.path:
                continue
            succ, err = trash_file(r.item.path)
            if succ:
                ok += 1
                journal.record(r.item.path, r.item.size, "auto_delete")
            else:
                fail += 1
                fails.append((r.item.path, err))
        report["stages"]["delete_apply"] = {"ok": ok, "fail": fail, "fails": fails}

    if opts.get("apply_jellyfin_clean"):
        report["stages"]["jellyfin_clean_apply"] = clean_jellyfin(
            getattr(cfg, "jellyfin_server", ""),
            getattr(cfg, "jellyfin_api_key", ""),
            bool(opts.get("refresh_library", True)),
            log,
        )

    counts = scoring["counts"]
    report["summary"] = {
        "library_files": len(paths),
        "score_counts": counts,
        "missing_subtitles": len(missing_subtitles),
        "dedup_groups": len(dup_groups),
        "merge_ready": report["stages"]["merge_plan"]["ready"],
        "applied": [k for k, v in opts.items() if k.startswith("apply_") and v],
    }
    return report
