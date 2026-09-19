"""旧合并结果检测与修复的纯函数层。

用于处理已经生成但可能损坏的“番号 合并.mp4 / 番号.mp4”：
- 从合并归档日志中找出已完成的合并；
- 用 ffmpeg 完整解码校验最终文件；
- 生成可供后台修复线程使用的原始分集清单。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from utils.merge import (
    EpisodeGroup,
    PartFile,
    extract_number_ext,
    parse_part_index,
    probe_part,
    strip_variant_tokens,
)
from utils.merge_archive import load_journal


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def validate_video_file(ffmpeg: str, path: str, timeout: int = 3600) -> tuple:
    """完整解码主视频/首音轨，返回 (是否可用, 摘要)。"""
    if not ffmpeg or not path or not os.path.isfile(path):
        return False, "文件不存在或未找到 ffmpeg"
    cmd = [
        ffmpeg, "-v", "error",
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "校验超时"
    except OSError as e:
        return False, str(e)
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return False, err[-500:] or "ffmpeg 解码返回非零"
    low = err.lower()
    error_markers = (
        "error while decoding", "invalid data found", "invalid nal",
        "corrupt", "failed to open", "co located poc", "missing picture",
        "packet size mismatch", "non-existing pps", "decode_slice_header error",
    )
    if any(marker in low for marker in error_markers):
        return False, err[-500:] or "检测到解码错误"
    return True, "解码正常"


_CORRUPTION_MARKERS = (
    "non-monoton", "non monoton", "invalid dts", "invalid pts",
    "timestamp discontinuity", "application provided invalid",
    "corrupt", "invalid data found", "invalid nal", "error while",
    "failed to open", "truncated", "broken header", "missing moov",
    "moov atom not found", "packet corrupt",
)


def validate_video_stream(ffmpeg: str, path: str, timeout: int = 3600) -> tuple:
    """轻量校验：只 demux/remux 主视频/首音轨，不解码，CPU 占用低。"""
    if not ffmpeg or not path or not os.path.isfile(path):
        return False, "文件不存在或未找到 ffmpeg"
    cmd = [
        ffmpeg, "-hide_banner", "-v", "warning", "-xerror",
        "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c", "copy", "-f", "null", "-",
    ]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "快速校验超时"
    except OSError as e:
        return False, str(e)
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return False, err[-500:] or "容器结构校验返回非零"
    low = err.lower()
    for marker in _CORRUPTION_MARKERS:
        if marker in low:
            return False, err[-500:] or f"检测到损坏标记: {marker}"
    return True, "容器结构正常"


def validate_final_metadata(ffprobe: str, final_path: str,
                            media_entries: list, timeout: int = 120) -> tuple:
    """轻量校验旧合并结果：只读 ffprobe 元数据，对比原分集时长/帧率/音轨。

    原分集优先读原路径，不存在则读备份路径；全程不解码，CPU 占用接近 0。
    """
    if not ffprobe or not final_path or not Path(final_path).exists():
        return False, "文件不存在或未找到 ffprobe"
    final_probe = probe_part(ffprobe, final_path, timeout=timeout)
    if not final_probe.ok:
        return False, final_probe.error or "最终文件探测失败"
    if not final_probe.video_codec or final_probe.duration <= 0:
        return False, "最终文件缺少可读视频流或时长为 0"

    source_durations: list[float] = []
    first_fps = 0.0
    first_audio = ""
    for e in media_entries:
        src = e.get("original_path") or ""
        if not src or not Path(src).exists():
            backup = e.get("backup_path") or ""
            if backup and Path(backup).exists():
                src = backup
            else:
                continue
        sp = probe_part(ffprobe, src, timeout=timeout)
        if not sp.ok:
            continue
        if sp.duration > 0:
            source_durations.append(sp.duration)
        if not first_fps and sp.fps > 0:
            first_fps = sp.fps
        if not first_audio:
            first_audio = sp.audio_codec

    if source_durations and len(source_durations) == len(media_entries):
        expected = sum(source_durations)
        tolerance = max(2.0, expected * 0.02)
        if abs(final_probe.duration - expected) > tolerance:
            return False, (
                f"时长异常：{final_probe.duration:.2f}s，预期约 {expected:.2f}s"
            )

    if first_fps > 0 and final_probe.fps > 0:
        diff = abs(final_probe.fps - first_fps)
        if diff > max(0.05, first_fps * 0.10):
            return False, (
                f"帧率异常：{final_probe.fps:.3f}fps，预期约 {first_fps:.3f}fps"
            )

    if first_audio and not final_probe.audio_codec:
        return False, "最终文件丢失音轨"

    return True, "元数据校验正常"


def validate_merged_video_quick(ffmpeg: str, ffprobe: str, final_path: str,
                                media_entries: list, timeout: int = 3600) -> tuple:
    """不解码的合并完整性检测：先元数据，再做容器/流复制校验。"""
    if ffprobe:
        ok, detail = validate_final_metadata(
            ffprobe, final_path, media_entries, timeout=120
        )
        if not ok:
            return ok, detail
    if ffmpeg:
        return validate_video_stream(ffmpeg, final_path, timeout)
    if not ffprobe:
        return False, "缺少 ffmpeg/ffprobe"
    return True, "元数据校验通过"


def archived_merge_items(entries=None) -> list:
    """按番号汇总所有已完成替换的合并，返回可修复项。

    每项包含：
    - number / final_path / media_entries
    - backup_count / original_count（当前可用的来源数量）
    """
    all_entries = entries if entries is not None else load_journal()
    by_number: dict[str, list] = {}
    for e in all_entries:
        number = e.get("number")
        if number:
            by_number.setdefault(number, []).append(e)

    items = []
    for number, group_entries in by_number.items():
        media_by_original: dict[str, dict] = {}
        for e in group_entries:
            if e.get("kind") != "media":
                continue
            op = e.get("original_path") or ""
            if not op:
                continue
            if op not in media_by_original:
                media_by_original[op] = e
                continue
            cur = media_by_original[op]
            cur_has = bool(cur.get("backup_path") and Path(cur["backup_path"]).exists())
            new_has = bool(e.get("backup_path") and Path(e["backup_path"]).exists())
            if new_has and not cur_has:
                media_by_original[op] = e
            elif new_has == cur_has:
                media_by_original[op] = e
        media = [
            e for e in media_by_original.values()
            if Path(e.get("original_path", "")).exists()
            or (e.get("backup_path") and Path(e["backup_path"]).exists())
        ]
        sidecar_by_original: dict[str, dict] = {}
        for e in group_entries:
            if e.get("kind") != "sidecar":
                continue
            op = e.get("original_path") or ""
            if not op:
                continue
            if not (Path(op).exists() or (e.get("backup_path") and Path(e["backup_path"]).exists())):
                continue
            if op not in sidecar_by_original:
                sidecar_by_original[op] = e
                continue
            cur = sidecar_by_original[op]
            cur_has = bool(cur.get("backup_path") and Path(cur["backup_path"]).exists())
            new_has = bool(e.get("backup_path") and Path(e["backup_path"]).exists())
            if new_has and not cur_has:
                sidecar_by_original[op] = e
            elif new_has == cur_has:
                sidecar_by_original[op] = e
        sidecar_entries = list(sidecar_by_original.values())
        outputs = [
            e for e in group_entries
            if e.get("kind") == "merged_output" and e.get("final_path")
        ]
        if not media or not outputs:
            continue
        final_path = outputs[-1].get("final_path") or ""
        backup_count = sum(
            1 for e in media
            if e.get("backup_path") and Path(e["backup_path"]).exists()
        )
        original_count = sum(
            1 for e in media
            if e.get("original_path") and Path(e["original_path"]).exists()
        )
        items.append({
            "number": number,
            "final_path": final_path,
            "media_entries": media,
            "backup_count": backup_count,
            "original_count": original_count,
            "sidecar_entries": sidecar_entries,
        })
    return items


def build_parts_from_paths(paths: list, number: str, ffprobe: str) -> list:
    """从已恢复的原分集路径构建 PartFile 列表，保持原合并顺序。"""
    parts = []
    for path in paths:
        probe = probe_part(ffprobe, path)
        if not probe.ok:
            raise ValueError(f"无法探测分集参数：{path} — {probe.error}")
        name = os.path.basename(path)
        stem, _ext = os.path.splitext(name)
        display_number = extract_number_ext(stem) or number
        part_index, part_kind = parse_part_index(stem, display_number)
        variants = strip_variant_tokens(stem)[1]
        parts.append(PartFile(
            path=path,
            name=name,
            stem=stem,
            number=display_number,
            part_index=part_index,
            part_kind=part_kind,
            variants=variants,
            probe=probe,
        ))
    parts.sort(key=lambda p: p.part_index)
    return parts


def make_group_from_parts(number: str, parts: list) -> EpisodeGroup:
    return EpisodeGroup(
        number=number,
        parts=parts,
        status="ready",
        selected_paths=[p.path for p in parts],
    )
