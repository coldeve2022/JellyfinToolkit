"""One reviewable restored-video pairing/execution service for GUI and CLI."""

from __future__ import annotations

import os
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


def preview_release_replacements(candidate_paths, originals, should_stop=None):
    """Pair downloaded versions by complete work ID; never choose among copies."""
    from censor.numbering import parse_from_path
    from censor.releases.models import normalize_number
    index = {}
    for value in _videos(originals, should_stop):
        key = normalize_number(parse_from_path(value).number)
        if key:
            index.setdefault(key, []).append(str(value))
    plan = []
    for value in _videos(candidate_paths, should_stop):
        if should_stop and should_stop():
            break
        key = normalize_number(parse_from_path(value).number)
        matches = list(dict.fromkeys(index.get(key, []))) if key else []
        status = "ready" if len(matches) == 1 else "ambiguous" if matches else "not_found"
        target = matches[0] if status == "ready" else ""
        if target and path_key(value) == path_key(target):
            status, target = "conflict", ""
        if target and Path(value).suffix.lower() != Path(target).suffix.lower():
            # 封装不同 ≠ 不能替换：换的只是"盒子"，内容通常还是同一条片子。
            # 做法是替换前用 ffmpeg -c copy 无损转成原文件的封装，路径不变，
            # 这样 Jellyfin 的条目/播放进度/收藏都还在。
            status = "container_convert"
        plan.append(dict(source=str(value), target=target, candidates=matches, status=status,
                         error="" if status == "ready" else "需要选择要用的新版本",
                         convert_container=status == "container_convert",
                         source_hash=file_hash(value), target_hash=file_hash(target) if target else None))
    grouped = {}
    for row in plan:
        if row["target"] and row["status"] == "ready":
            grouped.setdefault(path_key(row["target"]), []).append(row)
    for rows in grouped.values():
        if len(rows) > 1:
            for row in rows:
                row.update(status="conflict", error="多个候选指向同一原文件，请只保留一个版本")
    return plan


def transcode_container(source, target_suffix, *, log=print):
    """把 ``source`` 无损转封装成 ``target_suffix``（如 .mp4），返回临时文件路径。

    ``-c copy`` 只重排容器、不重新编码：几秒钟、画质零损失。之所以要它，
    是因为 mkv 内容直接改名成 .mp4 会让扩展名与容器不符 —— Jellyfin 用
    ffprobe 探测还能播，但客户端直连/硬件解码常按扩展名选解复用器而失败。
    """
    import subprocess
    import tempfile

    from utils.tools import resolve_tool

    ffmpeg = resolve_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("缺少 ffmpeg，无法转换封装；请在设置里配置 ffmpeg 路径")
    suffix = str(target_suffix or "").lower()
    handle, out = tempfile.mkstemp(prefix="container-", suffix=suffix)
    os.close(handle)
    command = [ffmpeg, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", str(source), "-c", "copy", "-map", "0"]
    if suffix == ".mp4":
        # faststart 让 moov 前置，Jellyfin 直连与拖动更稳
        command += ["-movflags", "+faststart"]
    command.append(out)
    log(f"转换封装：{Path(source).suffix or '原格式'} → {suffix}")
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        Path(out).unlink(missing_ok=True)
        raise ValueError(f"转换封装失败：{exc}") from exc
    if done.returncode != 0 or not Path(out).exists() or Path(out).stat().st_size == 0:
        Path(out).unlink(missing_ok=True)
        detail = (done.stderr or "").strip().splitlines()
        raise ValueError("转换封装失败（可能是字幕/音频编码装不进目标容器）："
                         + (detail[-1][:160] if detail else f"ffmpeg 退出码 {done.returncode}"))
    return out


def execute_replacements(plan, *, journal=None, should_stop=None, log=print, validator=None):
    journal = journal or OperationJournal()
    stats = dict(ok=0, fail=0, cancelled=False, fails=[], operations=[])
    for row in plan:
        if should_stop and should_stop():
            stats["cancelled"] = True
            break
        converted = ""
        try:
            # container_convert 的 target 已经唯一确定，只是封装不同 ——
            # 它和 ready 一样可以直接执行（执行时会先转封装）
            if row.get("status") not in ("ready", "container_convert") or not row.get("target"):
                raise ValueError(row.get("error") or "配对未获确认")
            if (
                file_hash(row["source"]) != row["source_hash"]
                or file_hash(row["target"]) != row["target_hash"]
            ):
                raise ValueError("计划创建后文件发生变化，请重新审阅")
            source = row["source"]
            if row.get("convert_container"):
                suffix = Path(row["target"]).suffix
                converted = transcode_container(source, suffix, log=log)
                source = converted
            result = safe_replace(
                source,
                row["target"],
                journal=journal,
                should_stop=should_stop,
                move_source=True,
                validator=validator
                or (lambda output: validate_restored_output(row["target"], output)),
                kind="restored_replace",
                # 转封装后源已经是程序生成的临时文件，原 source_hash 不再适用
                # （原始文件在进入这一步之前已经校验过）
                expected_source_hash=None if converted else row["source_hash"],
                expected_target_hash=row["target_hash"],
            )
            if converted:
                Path(converted).unlink(missing_ok=True)
            stats["operations"].append(result)
            if not result["ok"]:
                if result["state"] == "cancelled":
                    stats["cancelled"] = True
                    break
                raise ValueError(result["error"])
            stats["ok"] += 1
            log(f"替换成功（可恢复）：{row['source']} → {row['target']}")
        except (OSError, ValueError, KeyError) as exc:
            if converted:
                Path(converted).unlink(missing_ok=True)
            stats["fail"] += 1
            stats["fails"].append((row.get("source", ""), str(exc)))
            log(f"替换失败：{exc}")
    return stats
