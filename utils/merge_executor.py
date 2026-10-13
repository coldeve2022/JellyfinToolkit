"""Shared cancellable merge core. Originals stay intact; outputs keep declared tracks."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from utils.file_ops import OperationJournal, file_hash, safe_replace
from utils.merge import (
    build_concat_paths,
    choose_output_container,
    check_merge_compat,
    display_number_for_group,
    probe_part,
    unique_output_path,
    validate_merge_selection,
    validate_merged_output,
)


def _runner(cmd, timeout, log, should_stop):
    from utils.proc import run_streaming

    result = run_streaming(cmd, timeout=timeout, should_stop=should_stop, on_line=log)
    return (
        result["code"] == 0 and not result["error"],
        result["error"] or "\n".join(result["lines"][-12:]),
    )


def _external_subtitles(parts):
    from utils.merge_archive import iter_sidecars
    from utils.subtitle_clean import parse_srt, parse_vtt, render_srt, render_vtt

    merged = {}
    offset = 0.0
    for part in parts:
        for filename in iter_sidecars(part.path):
            path = Path(filename)
            if path.suffix.lower() not in {".srt", ".vtt", ".ass", ".ssa", ".sub", ".lrc"}:
                continue
            if path.suffix.lower() not in {".srt", ".vtt"}:
                raise ValueError(f"外置字幕格式需先转换为 SRT/VTT，不能安全自动拼接：{path.name}")
            from utils.nfo import read_text_any_encoding

            text = read_text_any_encoding(str(path))
            if text is None:
                raise ValueError(f"字幕无法完整解码：{path.name}")
            header = "WEBVTT"
            if path.suffix.lower() == ".srt":
                cues = parse_srt(text)
            else:
                cues, header = parse_vtt(text)
            if not cues or len(cues) != text.count("-->") or part.probe.duration <= 0:
                raise ValueError(f"字幕/分集时长不能安全解析：{path.name}")
            key = path.name[len(Path(part.path).stem) :]
            entry = merged.setdefault(key, dict(cues=[], header=header))
            if header != entry["header"]:
                raise ValueError("分集 WebVTT 样式不同，需要人工审阅")
            for cue in cues:
                entry["cues"].append(
                    dict(cue, start=cue["start"] + offset, end=cue["end"] + offset)
                )
        offset += part.probe.duration
    return {
        key: (
            render_srt(value["cues"])
            if key.lower().endswith(".srt")
            else render_vtt(value["cues"], value["header"])
        )
        for key, value in merged.items()
    }


def _track_signature(probe):
    return [
        (s.get("codec_type"), s.get("codec_name"), (s.get("tags") or {}).get("language", ""))
        for s in probe.streams
        if s.get("codec_type") in {"audio", "subtitle"}
    ]


def preview_merge_inputs(parts):
    """Freeze every original sidecar/source NFO that the merge/archive workflow reads."""
    from utils.merge_archive import iter_sidecars, choose_source_nfo

    paths = [part.path for part in parts]
    inputs = list(paths)
    for path in paths:
        inputs.extend(iter_sidecars(path))
    source_nfo = choose_source_nfo(paths, parts[0].number if parts else "")
    if source_nfo:
        inputs.append(source_nfo)
    return list(dict.fromkeys(inputs))


def choose_merge_container(parts, requested="mp4"):
    """The plan and executor share a codec-preserving container decision."""
    container = choose_output_container(parts, requested)
    subtitles = {
        s.get("codec_name")
        for p in parts
        for s in p.probe.streams
        if s.get("codec_type") == "subtitle"
    }
    if not subtitles:
        return container
    if subtitles == {"mov_text"}:
        if container in {"mp4", "mov"}:
            return container
        raise ValueError("mov_text 字幕需保留 MP4/MOV 容器，不能静默转成其它字幕格式")
    if "mov_text" in subtitles:
        raise ValueError("混合字幕编码不能安全无损拼接，请人工审阅")
    return "mkv"


def merge_group_ffmpeg(
    group, ffmpeg, ffprobe, options=None, log=print, should_stop=None, runner=None
):
    options = options or {}
    stopped = should_stop or (lambda: False)
    timeout = max(1, float(options.get("merge_timeout_per_file", 3600)))
    run = runner or (lambda cmd, limit: _runner(cmd, limit, log, stopped))
    operations = []
    journal = None
    try:
        if stopped():
            return False, "已停止"
        selected = set(group.selected_paths)
        parts = sorted((p for p in group.parts if p.path in selected), key=lambda p: p.part_index)
        reencode = bool(options.get("merge_allow_reencode_fallback"))
        valid, why = validate_merge_selection(parts, reencode)
        if not valid:
            return False, why
        if not ffmpeg or not ffprobe:
            return False, "缺少 ffmpeg/ffprobe，不能可靠校验合并产物"
        # Source bytes are the execution identity; reviewed snapshots reject changed input.
        planned_hashes = options.get("source_hashes") or {}
        source_hashes = {path: file_hash(path) for path in preview_merge_inputs(parts)}
        if planned_hashes and set(planned_hashes) != set(source_hashes):
            return False, "原分集关联文件集合已改变，请重新审阅计划"
        if any(
            planned_hashes.get(path, digest) != digest for path, digest in source_hashes.items()
        ):
            return False, "原分集已改变，请重新创建合并计划"
        signatures = [_track_signature(p.probe) for p in parts]
        if signatures and any(value != signatures[0] for value in signatures[1:]):
            return False, "分集音轨/字幕轨数量、编码或语言不一致，需人工审阅以防丢轨"
        if any(sum(s.get("codec_type") == "video" for s in p.probe.streams) > 1 for p in parts):
            return False, "多视频轨暂不能安全拼接，请人工审阅"
        compatible, issues = check_merge_compat(parts)
        use_reencode = not compatible
        if use_reencode and not reencode:
            return False, "；".join(issues)
        if use_reencode and any(
            p.probe.color_transfer in {"smpte2084", "arib-std-b67"} for p in parts
        ):
            return False, "HDR 分集转码需要明确色彩转换，已拒绝自动降为 SDR"
        external = _external_subtitles(parts)
        ext = choose_merge_container(parts, options.get("merge_output_container", "mp4"))
        label = display_number_for_group(group) or group.number
        out_dir = Path(parts[0].path).parent
        supplied = options.get("planned_output")
        final = Path(supplied) if supplied else unique_output_path(out_dir / f"{label} 合并.{ext}")
        if supplied and final.suffix.lower() != f".{ext}":
            return False, "计划输出容器与保留轨道所需容器不同，请重新审阅"
        if final.exists() or any(final.with_name(final.stem + key).exists() for key in external):
            return False, "计划输出或字幕路径已存在，请重新审阅"
        final.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".toolkit-merge-", dir=final.parent) as temporary:
            temp = Path(temporary)
            inputs = []
            expected_parts = parts
            for i, part in enumerate(parts):
                if stopped():
                    return False, "已停止"
                normal = temp / f"part{i}.{ext}"
                video_map = (
                    f"0:{part.probe.video_stream_index}"
                    if part.probe.video_stream_index >= 0
                    else "0:v:0"
                )
                cmd = [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    part.path,
                    "-map",
                    video_map,
                    "-map",
                    "0:a?",
                    "-map",
                    "0:s?",
                    "-map_metadata",
                    "0",
                ]
                if use_reencode:
                    ref = parts[0].probe
                    filters = [
                        f"scale={ref.width}:{ref.height}:force_original_aspect_ratio=decrease",
                        f"pad={ref.width}:{ref.height}:(ow-iw)/2:(oh-ih)/2",
                        f"fps={ref.fps or 30}",
                        "format=yuv420p",
                    ]
                    cmd += [
                        "-vf",
                        ",".join(filters),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "18",
                        "-c:a",
                        "aac",
                        "-c:s",
                        "copy",
                    ]
                else:
                    cmd += ["-c", "copy"]
                if ext in {"mp4", "mov"}:
                    cmd += ["-video_track_timescale", "90000"]
                ok, why = run(cmd + [str(normal)], timeout)
                if not ok:
                    return False, why or "规范化分集失败"
                if not normal.is_file() or not normal.stat().st_size:
                    return False, "规范化分集输出为空"
                inputs.append(str(normal))
            if use_reencode:
                expected_parts = [
                    replace(
                        p,
                        probe=replace(
                            p.probe,
                            video_codec="h264",
                            audio_codec="aac" if p.probe.audio_codec else "",
                            pixel_format="yuv420p",
                        ),
                    )
                    for p in parts
                ]
            list_file = temp / "inputs.txt"
            build_concat_paths(inputs, str(list_file))
            partial = temp / f"output.{ext}"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-map",
                "0:s?",
                "-c",
                "copy",
            ]
            if ext in {"mp4", "mov"}:
                cmd += ["-movflags", "+faststart"]
            ok, why = run(cmd + [str(partial)], timeout)
            if not ok:
                return False, why or "合并失败"
            if stopped():
                return False, "已停止"
            ok, why = validate_merged_output(
                ffprobe, str(partial), expected_parts, timeout=int(timeout)
            )
            if not ok:
                return False, why
            output_probe = probe_part(ffprobe, str(partial))
            expected_counts = {
                kind: sum(s.get("codec_type") == kind for s in parts[0].probe.streams)
                for kind in ("audio", "subtitle")
            }
            actual_counts = {
                kind: sum(s.get("codec_type") == kind for s in output_probe.streams)
                for kind in ("audio", "subtitle")
            }
            if expected_counts != actual_counts:
                return False, "合并输出音轨/字幕轨数量不符，已保留原分集"
            if any(file_hash(path) != digest for path, digest in source_hashes.items()):
                return False, "执行中原分集发生变化"
            journal = OperationJournal()
            result = safe_replace(
                partial,
                final,
                journal=journal,
                should_stop=stopped,
                move_source=True,
                allow_overwrite=False,
                kind="merge_output",
            )
            operations.append(result)
            if not result["ok"]:
                return False, result["error"]
            for key, text in external.items():
                if stopped():
                    raise RuntimeError("已停止")
                staging = temp / ("subtitle" + key)
                staging.write_text(text, encoding="utf-8")
                result = safe_replace(
                    staging,
                    final.with_name(final.stem + key),
                    journal=journal,
                    should_stop=stopped,
                    move_source=True,
                    allow_overwrite=False,
                    kind="merge_subtitle",
                )
                operations.append(result)
                if not result["ok"]:
                    raise RuntimeError(result["error"])
            # Durable identity lets archival refuse substituted/empty outputs.
            manifest = dict(
                source_hashes=source_hashes,
                output_hash=file_hash(final),
                output=str(final),
                sidecars=list(external),
                operations=[r["operation_id"] for r in operations],
            )
            manifest_path = final.with_name(final.name + ".toolkit-merge.json")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True, str(final)
    except Exception as exc:
        for operation in reversed(operations):
            if operation.get("ok") and journal:
                journal.restore(operation["operation_id"])
        return False, str(exc)
