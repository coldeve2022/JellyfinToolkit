"""分集合并 Worker 层 — 后台扫描与后台合并，遵循现有 ffmpeg 安全模式。

安全原则：
- 合并：先写临时文件 → ffmpeg 成功 → os.replace 原子落位；
- 失败/停止：删除临时文件，原文件不动；
- 原文件默认保留，由页面“确认替换”按钮决定何时移入备份。
"""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QThread, Signal

from utils.merge import MergePlan, build_merge_plan, scan_files


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class ScanMergeWorker(QThread):
    """扫描目录 → 探测 → 分组 → 出 MergePlan。"""

    log = Signal(str)
    progress = Signal(int)
    plan_ready = Signal(object)  # MergePlan
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
                files,
                self._ffmpeg,
                self._ffprobe,
                self._options,
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
    group_done = Signal(str, bool, str)  # 番号, 是否成功, 输出路径/错误
    finished = Signal()

    def __init__(self, tasks, ffmpeg="", options=None, ffprobe="", parent=None):
        super().__init__(parent)
        # tasks: list of EpisodeGroup（使用其 selected_paths）
        self._tasks = list(tasks)
        self._ffmpeg = ffmpeg or ""
        self._ffprobe = ffprobe or ""
        self._options = dict(options or {})
        self._timeout = max(30, int(self._options.get("merge_timeout_per_file", 3600)))
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

    def _merge_group(self, group) -> tuple:
        from utils.merge_executor import merge_group_ffmpeg

        return merge_group_ffmpeg(
            group,
            self._ffmpeg,
            self._ffprobe,
            self._options,
            self.log.emit,
            should_stop=lambda: self._stopped,
            runner=self._run_ffmpeg,
        )
