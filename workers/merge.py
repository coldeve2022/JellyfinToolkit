"""分集合并 Worker 层 — 后台扫描与后台合并，遵循现有 ffmpeg 安全模式。

安全原则：
- 合并：先写临时文件 → ffmpeg 成功 → os.replace 原子落位；
- 失败/停止：删除临时文件，原文件不动；
- 原文件默认保留，由页面“确认替换”按钮决定何时移入备份。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils.merge import (
    MergePlan,
    build_concat_paths,
    build_merge_plan,
    check_merge_compat,
    choose_output_container,
    display_number_for_group,
    scan_files,
    unique_output_path,
    validate_merge_selection,
    validate_merged_output,
)


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _video_map_spec(part) -> str:
    idx = getattr(part.probe, "video_stream_index", -1)
    return f"0:{idx}" if idx >= 0 else "0:v:0"


def _audio_map_spec(part) -> str:
    idx = getattr(part.probe, "audio_stream_index", -1)
    return f"0:{idx}?" if idx >= 0 else "0:a:0?"


def _needs_normalize(part, ext: str) -> bool:
    """非 MP4 容器、含 attached_pic、真实视频流不是首路，或 time_base 不统一时，都需要 remux。"""
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


class ScanMergeWorker(QThread):
    """扫描目录 → 探测 → 分组 → 出 MergePlan。"""

    log = Signal(str)
    progress = Signal(int)
    plan_ready = Signal(object)   # MergePlan
    error = Signal(str)
    finished = Signal()

    def __init__(self, root_paths, ffmpeg="", ffprobe="", options=None, parent=None):
        super().__init__(parent)
        self._roots = list(root_paths)
        self._ffmpeg = ffmpeg or ""
        self._ffprobe = ffprobe or ""
        self._options = dict(options or {})
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        try:
            self.log.emit("正在扫描目录…")
            files = scan_files(
                self._roots,
                exclude_keywords=self._options.get("exclude_path_keywords"),
            )
            if self._stopped:
                return
            self.log.emit(f"发现 {len(files)} 个视频文件，正在探测参数…")
            plan: MergePlan = build_merge_plan(
                files, self._ffmpeg, self._ffprobe, self._options,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
            )
            if self._stopped:
                return
            self.plan_ready.emit(plan)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"扫描失败: {e}")
        finally:
            self.finished.emit()


class MergeWorker(QThread):
    """按计划逐组合并无损拼接，产出去合并文件，不触碰原文件。"""

    log = Signal(str)
    progress = Signal(int)
    group_done = Signal(str, bool, str)   # 番号, 是否成功, 输出路径/错误
    finished = Signal()

    def __init__(self, tasks, ffmpeg="", options=None, ffprobe="", parent=None):
        super().__init__(parent)
        # tasks: list of EpisodeGroup（使用其 selected_paths）
        self._tasks = list(tasks)
        self._ffmpeg = ffmpeg or ""
        self._ffprobe = ffprobe or ""
        self._options = dict(options or {})
        self._timeout = max(30, int(options.get("merge_timeout_per_file", 3600)))
        self._stopped = False
        self._current_process = None

    def stop(self) -> None:
        self._stopped = True
        if self._current_process:
            try:
                self._current_process.kill()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:
        if not self._ffmpeg:
            self.log.emit("❌ 未找到 ffmpeg，无法合并")
            self.finished.emit()
            return
        try:
            total = len(self._tasks)
            done = 0
            for group in self._tasks:
                if self._stopped:
                    return
                self.progress.emit(int(done / total * 100) if total else 0)
                ok, msg = self._merge_group(group)
                self.group_done.emit(group.number, ok, msg)
                if ok:
                    self.log.emit(f"✅ 合并完成 {group.number} → {msg}")
                else:
                    self.log.emit(f"❌ 合并失败 {group.number}: {msg}")
                done += 1
            self.progress.emit(100 if total else 0)
        finally:
            self.finished.emit()

    def _run_ffmpeg(self, cmd: list, timeout: int) -> tuple:
        """执行 ffmpeg 并返回 (是否成功, stderr 摘要)。"""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
        )
        self._current_process = proc
        timed_out = False
        try:
            _out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            _out, err = proc.communicate()
        self._current_process = None
        if self._stopped:
            return False, "已停止"
        if timed_out:
            return False, "合并超时"
        if proc.returncode == 0:
            return True, (err or "").strip()
        detail = (err or _out or "").strip()
        if detail:
            detail = detail[-800:]
        return False, f"ffmpeg 合并失败{('：' + detail) if detail else ''}"

    def _reencode_part_to_target(self, part, out_path: Path, ref, ext: str) -> tuple:
        """把参数不一致的分集转码到参照分集的帧率/分辨率/音频参数。"""
        if not ref.probe.video_codec:
            return False, "参照分集缺少视频流，无法自动转码"

        ref_p = ref.probe
        fps = ref_p.fps or 30.0
        sample_rate = ref_p.sample_rate or 48000
        channels = ref_p.channels or 2

        need_scale = (
            ref_p.width > 0 and part.probe.width > 0
            and (ref_p.width != part.probe.width or ref_p.height != part.probe.height)
        )
        video_filter_parts = []
        if need_scale:
            video_filter_parts.append(
                f"scale={ref_p.width}:{ref_p.height}:force_original_aspect_ratio=decrease")
            video_filter_parts.append(
                f"pad={ref_p.width}:{ref_p.height}:(ow-iw)/2:(oh-ih)/2")
        video_filter_parts.append(f"fps={fps}")
        video_filter_parts.append("format=yuv420p")
        video_filter = ",".join(video_filter_parts)

        cmd = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", part.path,
            "-map", _video_map_spec(part), "-map", _audio_map_spec(part),
            "-vf", video_filter,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", str(sample_rate), "-ac", str(channels),
        ]
        if ext in ("mp4", "m4v", "mov"):
            cmd += ["-video_track_timescale", "90000"]
        cmd.append(str(out_path))
        self.log.emit(f"⏳ 转码归一化分集 {part.name}")
        return self._run_ffmpeg(cmd, self._timeout)

    def _merge_group(self, group) -> tuple:
        partial = intermediate = list_file = None
        norm_files: list[Path] = []
        try:
            selected = set(group.selected_paths)
            parts = [p for p in group.parts if p.path in selected]
            parts.sort(key=lambda p: p.part_index)
            if len(parts) < 2:
                return False, "分集不足"
            allow_reencode = bool(
                self._options.get("merge_allow_reencode_fallback"))
            valid, reason = validate_merge_selection(parts, allow_reencode)
            if not valid:
                return False, reason
            out_dir = Path(parts[0].path).parent
            ext = choose_output_container(parts, self._options.get("merge_output_container", "mp4"))
            label = display_number_for_group(group) or group.number
            final = unique_output_path(out_dir / f"{label} 合并.{ext}")
            partial = out_dir / f"{label} 合并.partial.{ext}"
            list_file = out_dir / f"{label} 合并.concat.txt"
            compatible, compat_issues = check_merge_compat(parts)
            use_reencode = False
            if not compatible:
                if not allow_reencode:
                    return False, "；".join(compat_issues[:5])
                if any(bool(p.probe.audio_codec) for p in parts) and not all(
                    bool(p.probe.audio_codec) for p in parts
                ):
                    return False, "分集音轨存在性不一致，自动转码暂不支持，请手动处理"
                use_reencode = True
                self.log.emit(
                    f"⚠️ {label} 分集参数不一致，将先转码归一化（消耗 CPU，耗时较长）")
            # 先把每个分集统一为干净的容器与真实视频/音频流，避免 attached_pic、
            # 多路流顺序或 time_base 不一致导致 concat 后损坏/黑屏。
            concat_inputs = []
            for i, p in enumerate(parts, 1):
                if use_reencode:
                    norm = out_dir / f"{label} 合并.norm{i}.{ext}"
                    ok, err = self._reencode_part_to_target(
                        p, norm, parts[0], ext)
                    if not ok:
                        return False, err or "转码归一化失败"
                    if not norm.exists() or norm.stat().st_size <= 0:
                        return False, "转码归一化输出为空"
                    norm_files.append(norm)
                    concat_inputs.append(str(norm))
                    continue
                if not _needs_normalize(p, ext):
                    concat_inputs.append(p.path)
                    continue
                norm = out_dir / f"{label} 合并.norm{i}.{ext}"
                norm_cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", p.path,
                    "-map", _video_map_spec(p), "-map", _audio_map_spec(p),
                    "-c", "copy",
                ]
                if ext in ("mp4", "m4v", "mov"):
                    norm_cmd += ["-video_track_timescale", "90000"]
                norm_cmd.append(str(norm))
                self.log.emit(f"⏳ 规范化分集 {label} {i}/{len(parts)}")
                ok, err = self._run_ffmpeg(norm_cmd, self._timeout)
                if not ok:
                    return False, err or "规范化分集失败"
                if not norm.exists() or norm.stat().st_size <= 0:
                    return False, "规范化分集输出为空"
                norm_files.append(norm)
                concat_inputs.append(str(norm))

            build_concat_paths(concat_inputs, str(list_file))

            if ext in ("mp4", "m4v", "mov"):
                concat_cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(partial),
                ]
                self.log.emit(f"⏳ 合并 {label}（{len(parts)} 个分集 → {ext}）")
                ok, err = self._run_ffmpeg(concat_cmd, self._timeout)
            else:
                intermediate = out_dir / f"{label} 合并.inter.mkv"
                concat_cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy",
                    str(intermediate),
                ]
                self.log.emit(f"⏳ 合并 {label}（{len(parts)} 个分集 → 中间容器）")
                ok, err = self._run_ffmpeg(concat_cmd, self._timeout)
                if not ok:
                    return False, err or "生成中间容器失败"
                if not intermediate.exists() or intermediate.stat().st_size <= 0:
                    return False, "中间容器为空"

                remux_cmd = [
                    self._ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(intermediate),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy",
                    str(partial),
                ]
                self.log.emit(f"⏳ 生成最终文件 {label}.{ext}")
                ok, err = self._run_ffmpeg(remux_cmd, self._timeout)

            if not ok:
                return False, err or "封装为最终容器失败"
            if not partial.exists() or partial.stat().st_size <= 0:
                return False, "最终文件为空"

            if self._ffprobe:
                self.log.emit(f"⏳ 轻量校验 {label} 元数据（ffprobe，不占 CPU）")
                ok, err = validate_merged_output(
                    self._ffprobe, str(partial), parts, timeout=self._timeout
                )
                if not ok:
                    return False, err or "输出元数据校验失败"
            else:
                self.log.emit(f"⏳ 快速校验 {label} 容器结构（仅 demux，不解码）")
                check_cmd = [
                    self._ffmpeg, "-v", "error",
                    "-i", str(partial),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", "-f", "null", "-",
                ]
                ok, err = self._run_ffmpeg(check_cmd, self._timeout)
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
