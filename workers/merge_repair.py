"""已损坏旧合并的批量扫描与修复后台线程。"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from utils.merge_archive import (
    add_journal_entries,
    archive_group_for_merge,
    iter_sidecars,
    mark_entry_rolled_back,
    resolve_backup_root,
    restore_entry,
)
from utils.merge import display_number_for_group
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
                    self._ffmpeg, self._ffprobe, path,
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
        number = item.get("number", "")
        final_path = item.get("final_path") or ""
        media_entries = item.get("media_entries") or []
        if len(media_entries) < 2:
            return False, "原分集记录不足 2 个"

        # 1) 恢复原始分集。原文件已存在则直接使用，不覆盖。
        original_paths = []
        for e in media_entries:
            original = e.get("original_path") or ""
            backup = e.get("backup_path") or ""
            if not original:
                continue
            if Path(original).exists():
                original_paths.append(original)
                continue
            if backup and Path(backup).exists():
                ok, detail = restore_entry(e)
                if not ok:
                    return False, f"恢复原分集失败 {Path(original).name}: {detail}"
                original_paths.append(original)
                mark_entry_rolled_back(e.get("id", ""))
            else:
                return False, f"缺少原分集和备份: {Path(original).name}"

        if len(original_paths) < 2:
            return False, "恢复后的原分集不足 2 个"

        # 1.5) 恢复同名 NFO/图片/字幕 sidecar，让重新替换时能再次生成 NFO。
        for e in item.get("sidecar_entries", []):
            original = e.get("original_path") or ""
            backup = e.get("backup_path") or ""
            if not original or Path(original).exists():
                continue
            if backup and Path(backup).exists():
                ok, detail = restore_entry(e)
                if not ok:
                    return False, f"恢复 sidecar 失败 {Path(original).name}: {detail}"
                mark_entry_rolled_back(e.get("id", ""))

        # 2) 按原始顺序构建分集，重新走新的两阶段合并链路。
        try:
            parts = build_parts_from_paths(original_paths, number, self._ffprobe)
            group = make_group_from_parts(number, parts)
        except Exception as e:  # noqa: BLE001
            return False, f"构建分集失败: {e}"
        label = display_number_for_group(group) or number

        merge_worker = MergeWorker([group], self._ffmpeg, self._options, self._ffprobe)
        merge_worker.log.connect(lambda msg: self.log.emit(msg))
        self._merge_worker = merge_worker
        try:
            ok, merged_output = merge_worker._merge_group(group)
        finally:
            self._merge_worker = None
        if not ok:
            return False, f"重新合并失败: {merged_output}"

        # 3) 删除坏文件及它的同名 sidecar（优先回收站，失败则移入隔离目录）。
        cleanup_errors = []
        if final_path and Path(final_path).exists() and Path(final_path) != Path(merged_output):
            ok, err = self._remove_damaged(final_path)
            if not ok:
                cleanup_errors.append(f"坏文件 {Path(final_path).name}: {err}")
        for side in iter_sidecars(final_path):
            if Path(side).exists():
                ok, err = self._remove_damaged(side)
                if not ok:
                    cleanup_errors.append(f"{Path(side).name}: {err}")
        if cleanup_errors:
            self.log.emit("⚠️ 部分旧文件清理失败：" + "；".join(cleanup_errors))
        if final_path and Path(final_path).exists() and Path(final_path) != Path(merged_output):
            return False, f"旧合并文件未成功移除，已保留新合并文件，请手动处理：{final_path}"

        # 4) 用新合并结果替换已恢复的原分集（会再次备份到备份根目录并生成/更新 NFO）。
        ok, entries, errors, final_output = archive_group_for_merge(
            self._cfg, label, original_paths, merged_output
        )
        if not ok:
            return False, "；".join(errors) or "替换原文件失败"
        add_journal_entries(entries)
        for err in errors:
            self.log.emit(f"⚠️ {number} 非致命问题: {err}")
        return True, final_output or merged_output

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
