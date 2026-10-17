"""Shared UI events and owned task lifecycle. No worker may outlive its owner unnoticed."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QSpinBox,
    QDoubleSpinBox,
    QMessageBox,
    QPushButton,
)


class Events(QObject):
    labels_changed = Signal()
    config_changed = Signal()
    subtitle_ready = Signal(list)
    restored_ready = Signal(list, list)
    media_changed = Signal(list)


events = Events()
_library_snapshots = {}


def _snapshot_key(cfg):
    import hashlib

    fields = {
        name: getattr(cfg, name, None)
        for name in (
            "library_source",
            "server_type",
            "jellyfin_server",
            "jellyfin_user_id",
            "jellyfin_db_path",
            "library_path_mappings",
            "library_scope_ids",
            "library_scope_paths",
            "exclude_path_keywords",
            "jellyfin_api_key",
            "ai_base_url",
            "ai_model",
            "engine",
            "enhance_match",
            "score_th_keep",
            "score_th_delete",
        )
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def publish_snapshot(cfg, data):
    if data.get("error") or "items" not in data:
        return
    _library_snapshots[_snapshot_key(cfg)] = copy.deepcopy(data["items"])


def accept_snapshot(owner, data):
    if data.get("error") or data.get("cancelled"):
        return False
    if getattr(owner, "_snapshot_request_key", _snapshot_key(owner.cfg)) != _snapshot_key(
        owner.cfg
    ):
        data["error"] = "任务期间媒体库、分析范围或观看用户发生变化，旧结果已丢弃，请重新扫描。"
        return False
    from utils.state_context import bind_resolved_context
    from utils.labels import LabelStore
    from config import DATA_DIR

    before_context = state_context(owner.cfg)
    before = owner.cfg.jellyfin_user_id
    context = bind_resolved_context(owner.cfg, data)
    owner._state_context = context
    if hasattr(owner, "store"):
        owner.store = LabelStore(DATA_DIR / "smart.db", context=context)
        owner.labels = owner.store.all()
    if hasattr(owner, "deck_state"):
        from utils.deck_state import DeckState

        owner.deck_state = DeckState(DATA_DIR / "smart.db", context=context)
        owner._skipped = owner.deck_state.skipped()
    if before != owner.cfg.jellyfin_user_id:
        owner.cfg.save()
    if before_context != context:
        events.config_changed.emit()
        events.labels_changed.emit()
    if (
        isinstance(data.get("labels"), dict)
        and data["labels"] != LabelStore(DATA_DIR / "smart.db", context=context).all()
    ):
        data["error"] = "分析期间人工反馈发生变化，旧结果已丢弃，请重新分析。"
        return False
    publish_snapshot(owner.cfg, data)
    return True


def invalidate_snapshots(*args):
    _library_snapshots.clear()


events.config_changed.connect(invalidate_snapshots)
events.media_changed.connect(invalidate_snapshots)


class SnapshotAnalyzeWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)

    def __init__(self, items, source, labels, th_keep, th_delete):
        super().__init__()
        self.items, self.source, self.labels = (
            copy.deepcopy(items),
            copy.deepcopy(source),
            dict(labels),
        )
        self.th_keep, self.th_delete = th_keep, th_delete
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        from utils.scoring_service import score_snapshot

        try:
            self.log.emit("使用当前共享库快照分析；在库概览重新扫描可刷新文件事实。")
            result = score_snapshot(
                self.items, self.source, self.labels, th_keep=self.th_keep, th_delete=self.th_delete
            )
            if self._stopped:
                self.finished.emit({"cancelled": True, "error": "分析已取消"})
            else:
                self.progress.emit(100)
                self.finished.emit(result)
        except Exception as exc:
            self.finished.emit({"error": str(exc)})


def analysis_worker(cfg, labels, exclude_keywords=None, th_keep=None, th_delete=None):
    items = _library_snapshots.get(_snapshot_key(cfg))
    if items is not None:
        return SnapshotAnalyzeWorker(items, cfg, labels, th_keep, th_delete)
    from workers.scorer import AnalyzeWorker

    return AnalyzeWorker(
        cfg, labels, exclude_keywords=exclude_keywords, th_keep=th_keep, th_delete=th_delete
    )


def page_workers(page):
    for value in vars(page).values():
        if isinstance(value, QThread) or callable(getattr(value, "isRunning", None)):
            yield value


def stop_page(page):
    manager = TaskManager.instance()
    for task_id, entry in list(manager._active.items()):
        if entry["owner"] is page:
            manager.stop(task_id)
    for worker in page_workers(page):
        if worker.isRunning():
            stop = getattr(worker, "stop", None)
            if callable(stop):
                stop()
            elif isinstance(worker, QThread):
                worker.requestInterruption()


class CallWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self, function):
        super().__init__()
        self.function = function
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()

    def run(self):
        try:
            if not self._stopped:
                result = self.function(lambda: self._stopped)
                if not self._stopped:
                    self.result.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


class TaskManager(QObject):
    changed = Signal()
    _instance = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        from config import DATA_DIR

        try:
            history = json.loads((DATA_DIR / "task_history.json").read_text(encoding="utf-8"))
            if isinstance(history, list):
                self.records = [
                    row
                    for row in history
                    if isinstance(row, dict)
                    and all(key in row for key in ("id", "title", "status", "started", "progress"))
                ]
                for row in self.records:
                    if row["status"] in ("running", "stopping"):
                        row["status"] = "interrupted"
        except (OSError, ValueError):
            pass
        self._active = {}
        self._timer = QTimer(self)
        self._timer.setInterval(35)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def running(self, owner=None):
        self._poll()
        return any(owner is None or entry["owner"] is owner for entry in self._active.values())

    def submit(self, worker, owner, title, mutating=False, resources=()):
        self._poll()
        if self.running(owner) or any(
            mutating and entry["record"]["mutating"] for entry in self._active.values()
        ):
            return False
        resource_set = set(resources)
        if any(resource_set.intersection(entry["resources"]) for entry in self._active.values()):
            return False
        if type(worker).__name__ in (
            "AnalyzeWorker",
            "SnapshotAnalyzeWorker",
            "IngestWorker",
            "RerankWorker",
            "AutomationWorker",
        ):
            owner._snapshot_request_key = _snapshot_key(owner.cfg)
        task_id = uuid.uuid4().hex
        record = {
            "id": task_id,
            "title": title,
            "status": "running",
            "mutating": bool(mutating),
            "started": datetime.now().isoformat(timespec="seconds"),
            "progress": 0,
            "detail": "",
            "logs": [],
        }
        controls = {}
        buttons = {}
        candidates = owner.findChildren(QObject)
        footer = getattr(owner, "_action_footer", None)
        if footer is not None:
            candidates += footer.findChildren(QObject)
        # Save each widget's LOCAL state before disabling any ancestor.  Using
        # isEnabled() after disabling a table captures False for its headers,
        # then permanently disables them when the task restores the controls.
        # WA_ForceDisabled also distinguishes an explicitly disabled control
        # from one merely inheriting a disabled page/container.
        for control in candidates:
            if isinstance(control, QPushButton) and not any(
                token in control.text() for token in ("停止", "暂停", "继续")
            ):
                buttons[control] = not control.testAttribute(Qt.WA_ForceDisabled)
            if isinstance(
                control,
                (QAbstractItemView, QCheckBox, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox),
            ):
                controls[control] = not control.testAttribute(Qt.WA_ForceDisabled)
        for control in [*buttons, *controls]:
            control.setEnabled(False)
        # Snapshot mutable configuration and request payloads before running.
        for attr in ("cfg", "_cfg", "source", "labels", "items", "_options"):
            value = getattr(worker, attr, None)
            if value is not None and not isinstance(value, (str, bytes)):
                try:
                    setattr(worker, attr, copy.deepcopy(value))
                except (TypeError, ValueError):
                    pass
        self.records.append(record)
        self._active[task_id] = {
            "worker": worker,
            "owner": owner,
            "record": record,
            "controls": controls,
            "buttons": buttons,
            "resources": resource_set,
            "stopping": False,
        }
        worker.finished.connect(lambda *args, tid=task_id: self._result(tid, args))
        if hasattr(worker, "result"):
            worker.result.connect(lambda *args, tid=task_id: self._result(tid, args))
        if hasattr(worker, "error"):
            worker.error.connect(lambda value, tid=task_id: self._error(tid, value))
        if hasattr(worker, "progress"):
            worker.progress.connect(lambda *args, tid=task_id: self._progress(tid, args))
        if hasattr(worker, "log"):
            worker.log.connect(lambda value, tid=task_id: self._log(tid, value))
        worker.start()
        self.changed.emit()
        return True

    def _result(self, task_id, args):
        entry = self._active.get(task_id)
        if not entry:
            return
        entry["reported"] = True
        for button in entry["buttons"]:
            if button.isEnabled():
                entry["buttons"][button] = True
            button.setEnabled(False)
        for value in args:
            if isinstance(value, list):
                for result in value:
                    if (
                        isinstance(result, dict)
                        and result.get("ok") is False
                        and result.get("error")
                    ):
                        entry["record"]["error"] = str(result["error"])
            if isinstance(value, dict):
                if value.get("error") or value.get("fail") or value.get("fails"):
                    entry["record"]["error"] = str(
                        value.get("error") or value.get("fails") or "部分项失败"
                    )
                if value.get("cancelled"):
                    entry["stopping"] = True

    def _error(self, task_id, value):
        if task_id in self._active:
            self._active[task_id]["record"]["error"] = str(value)

    def _progress(self, task_id, args):
        if task_id in self._active and args and isinstance(args[-1], (int, float)):
            self._active[task_id]["record"]["progress"] = max(0, min(100, int(args[-1])))
            self.changed.emit()

    def _log(self, task_id, value):
        if task_id in self._active:
            record = self._active[task_id]["record"]
            record["detail"] = str(value)
            record["logs"] = (record["logs"] + [str(value)])[-200:]
            self.changed.emit()

    def stop(self, task_id):
        entry = self._active.get(task_id)
        if entry:
            entry["stopping"] = True
            entry["record"]["status"] = "stopping"
            stop = getattr(entry["worker"], "stop", None)
            if callable(stop):
                stop()
            else:
                entry["worker"].requestInterruption()
            self.changed.emit()

    def stop_all(self):
        for task_id in list(self._active):
            self.stop(task_id)

    def _poll(self):
        for task_id, entry in list(self._active.items()):
            if entry["worker"].isRunning():
                continue
            if not entry.get("delivery_barrier_passed"):
                if not entry.get("delivery_barrier_scheduled"):
                    entry["delivery_barrier_scheduled"] = True
                    QTimer.singleShot(0, self, lambda tid=task_id: self._delivered(tid))
                continue
            stats = getattr(entry["worker"], "stats", None)
            if isinstance(stats, dict):
                self._result(task_id, (stats,))
            record = entry["record"]
            cancelled = entry["stopping"] or getattr(entry["worker"], "_stopped", False)
            record["status"] = (
                "cancelled" if cancelled else ("failed" if record.get("error") else "succeeded")
            )
            record["ended"] = datetime.now().isoformat(timespec="seconds")
            if record["status"] == "succeeded":
                record["progress"] = 100
            for control, enabled in entry["controls"].items():
                try:
                    control.setEnabled(enabled)
                except RuntimeError:
                    pass
            for button, enabled in entry["buttons"].items():
                try:
                    button.setEnabled(enabled)
                except RuntimeError:
                    pass
            del self._active[task_id]
            self._save_history()
            self.changed.emit()

    def _delivered(self, task_id):
        entry = self._active.get(task_id)
        if entry is not None:
            entry["delivery_barrier_passed"] = True
            self._poll()

    def _save_history(self):
        from config import DATA_DIR

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = DATA_DIR / "task_history.json"
            # Logs remain in memory; persisted summaries must not carry third-party secrets.
            rows = [
                {k: v for k, v in row.items() if k not in ("logs", "detail", "error")}
                for row in self.records[-200:]
            ]
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


_WRITE_WORKERS = {
    "TrashWorker",
    "ReplaceWorker",
    "NFOWorker",
    "CrossDBTransferWorker",
    "FFmpegRepairWorker",
    "MergeWorker",
    "RepairMergeWorker",
    "RollbackWorker",
    "LegacyMergeRenameWorker",
    "JellyfinCleanWorker",
    "JellyfinRestoreWorker",
    "WhisperGenWorker",
    "LadaRestoreWorker",
    "JellyfinRefreshWorker",
}
_TASK_LABELS = {
    "AnalyzeWorker": "读取媒体库并评分",
    "SnapshotAnalyzeWorker": "从库快照分析",
    "RerankWorker": "根据最新反馈重排",
    "IngestWorker": "扫描库概览",
    "TrashWorker": "可恢复隔离文件",
    "ReplaceWorker": "执行恢复视频替换计划",
    "NFOWorker": "执行NFO修复计划",
    "CrossDBTransferWorker": "迁移播放记录",
    "FFmpegRepairWorker": "修复或转码视频",
    "FFprobeAnalyzer": "检测视频健康",
    "MergeWorker": "合并已勾选分集",
    "ScanMergeWorker": "扫描分集与多版本",
    "RepairMergeWorker": "修复旧合并产物",
    "RepairScanWorker": "检测旧合并",
    "RollbackWorker": "恢复原分集备份",
    "JellyfinCleanWorker": "执行标签清洗计划",
    "JellyfinRestoreWorker": "恢复服务器标签",
    "WhisperGenWorker": "批量生成字幕",
    "LadaRestoreWorker": "处理马赛克并校验产物",
    "AIRecheckWorker": "AI内容复核",
    "AIScoreWorker": "AI内容语义打分",
    "AIDedupWorker": "AI重复版本建议",
    "AITasteWorker": "AI标签画像",
    "AIReportWorker": "生成AI画像报告",
    "_CensoredScan": "从库挑选待处理视频",
    "_MissingSubtitleScan": "从库检查字幕",
    "FileScannerWorker": "扫描字幕缺失",
    "CensorshipScanWorker": "按文件名分类视频",
}


def start_task(owner, worker, title=None, mutating=None):
    if getattr(owner.window(), "_closing", False):
        stop = getattr(worker, "stop", None)
        if callable(stop):
            stop()
        return False
    manager = TaskManager.instance()
    name = type(worker).__name__
    title = title or _TASK_LABELS.get(name, "后台任务")
    mutating = name in _WRITE_WORKERS if mutating is None else mutating
    pending = [entry for entry in manager._active.values() if entry["owner"] is owner]
    if pending:
        # Custom result signals can precede actual QThread termination; preserve
        # the next worker until the prior run has really returned.
        QTimer.singleShot(40, owner, lambda: start_task(owner, worker, title, mutating))
        return True
    if not manager.submit(worker, owner, title, mutating):
        QMessageBox.information(
            owner,
            "等待当前任务结束",
            "当前页面或媒体写操作仍在进行。请在任务中心等待结束或请求停止。",
        )
        return False
    return True


def busy(owner, mutating=False):
    manager = TaskManager.instance()
    return (
        manager.running(owner)
        or any(w.isRunning() for w in page_workers(owner))
        or (mutating and any(entry["record"]["mutating"] for entry in manager._active.values()))
    )


def after_stopped(owner, callback):
    if busy(owner):
        QTimer.singleShot(40, owner, lambda: after_stopped(owner, callback))
    else:
        callback()


def import_media(owner, paths, callback):
    """Directory enumeration is asynchronous; direct-file additions remain cheap."""
    from pathlib import Path

    if not any(Path(path).is_dir() for path in paths):
        return False
    from utils.file_scan import iter_media_files

    roots = list(paths)
    extensions = list(owner.cfg.video_extensions)
    excludes = list(owner.cfg.exclude_path_keywords)
    worker = CallWorker(lambda cancel: list(iter_media_files(roots, extensions, excludes, cancel)))
    owner._import_worker = worker
    worker.result.connect(lambda files: after_stopped(owner, lambda: callback(files)))
    if hasattr(owner, "log_panel"):
        worker.error.connect(owner.log_panel.log_error)
    start_task(owner, worker, "收集目录中的正片视频")
    return True


def state_context(cfg):
    from utils.state_context import state_context as context

    return context(cfg)


def refresh_configuration(page):
    """Apply saved defaults to idle controls; invalidate plans on context change."""
    if busy(page):
        after_stopped(page, lambda: refresh_configuration(page))
        return
    cfg = page.cfg
    context = state_context(cfg)
    previous = getattr(page, "_state_context", context)
    page._state_context = context
    if hasattr(page, "store"):
        from config import DATA_DIR
        from utils.labels import LabelStore

        page.store = LabelStore(DATA_DIR / "smart.db", context=context)
        page.labels = page.store.all()
    if hasattr(page, "deck_state"):
        from config import DATA_DIR
        from utils.deck_state import DeckState

        page.deck_state = DeckState(DATA_DIR / "smart.db", context=context)
        page._skipped = page.deck_state.skipped()
    if hasattr(page, "db_label"):
        page.db_label.setText(page._source_text())
    if previous != context:
        for attr in ("items", "results", "deck_ids", "groups", "_delete_rows"):
            if hasattr(page, attr):
                setattr(page, attr, [])
        if hasattr(page, "on_context_changed"):
            page.on_context_changed()
        if hasattr(page, "_clear_card"):
            page._clear_card("媒体库、分析范围或观看用户已变更，请重新分析。")
        if hasattr(page, "_render_table"):
            page._render_table()
        if hasattr(page, "_render_tree"):
            page._render_tree()
        if hasattr(page, "_fill_tables"):
            page._fill_tables()
    if hasattr(page, "_scan_results"):
        page._scan_results.clear()
    if hasattr(page, "combo_codec"):
        page.combo_codec.setCurrentIndex(max(0, page.combo_codec.findData(cfg.ffmpeg_gpu_codec)))
        page.combo_hw.setCurrentIndex(max(0, page.combo_hw.findData(cfg.ffmpeg_hardware_accel)))
        page.spin_crf.setValue(cfg.transcode_crf)
    module = type(page).__module__
    if module.endswith("subtitle_gen"):
        page.input_tool.setText(cfg.whisper_tool_path)
        page.input_out.setText(cfg.whisper_output_dir)
        page._select(page.combo_model, cfg.whisper_model)
        page._select(page.combo_formats, cfg.whisper_sub_formats)
        page._refresh_tool_status()
    elif module.endswith("lada"):
        page.input_tool.setText(cfg.lada_cli_path)
        page.input_out.setText(cfg.lada_output_dir)
        from utils.tools import resolve_tool

        page.set_ffprobe(resolve_tool("ffprobe") or "ffprobe")
        page._refresh_tool_status()
