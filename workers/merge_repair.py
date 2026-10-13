"""已损坏旧合并的批量扫描与修复后台线程。"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils.merge_archive import (
    iter_sidecars,
    resolve_backup_root,
)
from utils.merge_repair import (
    archived_merge_items,
    build_parts_from_paths,
    make_group_from_parts,
    validate_merged_video_quick,
)
from utils.trash import trash_file
from workers.merge import MergeWorker


class RepairScanWorker(QThread):
    """扫描所有已完成合并，校验最终文件是否可正常解码。"""

    log = Signal(str)
    progress = Signal(int)
    item_done = Signal(str, str, bool, str)  # number, final_path, ok, detail
    finished = Signal(dict)

    def __init__(self, ffmpeg="", entries=None, ffprobe="", parent=None):
        super().__init__(parent)
        self._ffmpeg = ffmpeg or ""
        self._ffprobe = ffprobe or ""
        self._entries = entries
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        items = archived_merge_items(self._entries)
        total = len(items)
        if total == 0:
            self.log.emit("未找到已完成的合并记录。")
        failed = 0
        for i, item in enumerate(items):
            if self._stopped:
                break
            path = item.get("final_path") or ""
            self.log.emit(f"校验 {item.get('number')}: {path}")
            try:
                ok, detail = validate_merged_video_quick(
                    self._ffmpeg,
                    self._ffprobe,
                    path,
                    item.get("media_entries") or [],
                )
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)
            if not ok:
                failed += 1
            self.item_done.emit(item.get("number"), path, ok, detail)
            self.progress.emit(int((i + 1) / total * 100) if total else 0)
        self.log.emit(f"扫描完成：{total} 个合并，其中疑似损坏 {failed} 个。")
        self.finished.emit({"total": total, "damaged": failed})


class RepairMergeWorker(QThread):
    """对选中的旧合并执行：恢复原分集 → 删除坏文件 → 用新链路重新合并 → 重新替换。"""

    log = Signal(str)
    progress = Signal(int)
    item_done = Signal(str, bool, str)  # number, ok, detail
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, items, cfg, ffmpeg="", ffprobe="", options=None, parent=None):
        super().__init__(parent)
        self._items = list(items)
        self._cfg = cfg
        self._ffmpeg = ffmpeg or ""
        self._ffprobe = ffprobe or ""
        self._options = dict(options or {})
        self._stopped = False
        self._merge_worker = None

    def stop(self) -> None:
        self._stopped = True
        if self._merge_worker is not None:
            self._merge_worker.stop()

    def run(self) -> None:
        total = len(self._items)
        ok = fail = 0
        fails: list = []
        for i, item in enumerate(self._items):
            if self._stopped:
                break
            self.progress.emit(int(i / total * 100) if total else 0)
            number = item.get("number", "")
            self.log.emit(f"开始修复 {number}")
            try:
                ok_flag, detail = self._repair_one(item)
            except Exception as e:  # noqa: BLE001
                ok_flag, detail = False, str(e)
            self.item_done.emit(number, ok_flag, detail)
            if ok_flag:
                ok += 1
                self.log.emit(f"✅ 修复完成 {number}: {detail}")
            else:
                fail += 1
                fails.append((number, detail))
                self.log.emit(f"❌ 修复失败 {number}: {detail}")
            self.progress.emit(int((i + 1) / total * 100) if total else 0)
        self.log.emit(f"批量修复结束：成功 {ok} / 失败 {fail}")
        self.finished.emit({"ok": ok, "fail": fail, "fails": fails})

    def _repair_one(self, item: dict) -> tuple:
        from utils.file_ops import OperationJournal, safe_replace, file_hash, path_key

        number = item.get("number", "")
        final_path = item.get("final_path") or ""
        entries = item.get("media_entries") or []
        if len(entries) < 2:
            return False, "原分集不足两个"
        if self._stopped:
            return False, "已停止"
        if (
            item.get("output_hash")
            and Path(final_path).exists()
            and file_hash(final_path) != item["output_hash"]
        ):
            return False, "最终文件已改变，请重新审阅"
        sources = []
        for entry in entries:
            backup = entry.get("backup_path") or ""
            original = entry.get("original_path") or ""
            if backup and Path(backup).is_file():
                candidate = backup
            elif (
                original and path_key(original) != path_key(final_path) and Path(original).is_file()
            ):
                candidate = original
            else:
                return False, "缺少有效原分集备份（最终合并不能作为输入）"
            if entry.get("expected_hash") and file_hash(candidate) != entry["expected_hash"]:
                return False, "原分集备份身份不符，请人工审阅"
            sources.append(candidate)
        parts = build_parts_from_paths(sources, number, self._ffprobe)
        group = make_group_from_parts(number, parts)
        worker = MergeWorker([group], self._ffmpeg, self._options, self._ffprobe)
        worker.log.connect(self.log.emit)
        self._merge_worker = worker
        try:
            ok, output = worker._merge_group(group)
        finally:
            self._merge_worker = None
        if not ok:
            return False, "重新合并失败：" + output
        if self._stopped:
            return False, "已停止，新产物已保留可审阅"
        journal = OperationJournal()
        operations = []
        try:
            result = safe_replace(
                output,
                final_path,
                journal=journal,
                move_source=True,
                should_stop=lambda: self._stopped,
                kind="merge_repair",
                expected_target_hash=item.get("output_hash"),
            )
            if not result["ok"]:
                raise RuntimeError(result["error"])
            operations.append(result["operation_id"])
            # Preserve old final sidecars and roll back the entire reviewed bundle on failure.
            for side in iter_sidecars(output):
                sp = Path(side)
                target = Path(final_path).with_name(
                    Path(final_path).stem + sp.name[len(Path(output).stem) :]
                )
                installed = safe_replace(
                    sp,
                    target,
                    journal=journal,
                    move_source=True,
                    should_stop=lambda: self._stopped,
                    kind="merge_repair_sidecar",
                )
                if not installed["ok"]:
                    raise RuntimeError(installed["error"])
                operations.append(installed["operation_id"])
            if self._stopped:
                raise RuntimeError("已停止")
        except Exception as exc:
            errors = []
            for operation in reversed(operations):
                restored = journal.restore(operation)
                if not restored["ok"]:
                    errors.append(restored["error"])
            return False, str(exc) + ("；回滚需人工处理：" + "；".join(errors) if errors else "")
        return True, final_path

    def _remove_damaged(self, path: str) -> tuple:
        p = Path(path)
        if not p.exists():
            return True, ""
        ok, err = trash_file(path)
        if ok:
            return True, ""
        # 回收站不可用时移入备份根目录下的隔离目录，避免直接物理删除。
        try:
            root = resolve_backup_root(self._cfg)
            quarantine = root / "_quarantine_damaged" / self._cfg.merge_original_subfolder
            quarantine.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = quarantine / f"{p.stem}_{stamp}{p.suffix}"
            if target.exists():
                i = 2
                while (quarantine / f"{p.stem}_{stamp}_{i}{p.suffix}").exists():
                    i += 1
                target = quarantine / f"{p.stem}_{stamp}_{i}{p.suffix}"
            shutil.move(str(p), str(target))
            return True, f"已移入隔离目录 {target}"
        except OSError as e:
            return False, f"{err}; 隔离失败: {e}"
