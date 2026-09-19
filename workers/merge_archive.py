"""合并替换回滚后台线程。"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from utils.merge_archive import mark_entry_rolled_back, restore_entry
from utils.merge import finalize_legacy_merged_output


class RollbackWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # {"ok": int, "fail": int, "fails": [(id, err)]}

    def __init__(self, entries: list, parent=None):
        super().__init__(parent)
        self._entries = list(entries)
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        ok = fail = 0
        fails: list = []
        total = len(self._entries)
        try:
            for i, entry in enumerate(self._entries):
                if self._stopped:
                    break
                ok_flag, detail = restore_entry(entry)
                name = entry.get("original_path") or ""
                if ok_flag:
                    ok += 1
                    mark_entry_rolled_back(entry.get("id", ""))
                    self.log.emit(f"↩ 已恢复: {name}")
                else:
                    fail += 1
                    fails.append((entry.get("id", ""), detail))
                    self.log.emit(f"❌ 恢复失败: {name} — {detail}")
                if total:
                    self.progress.emit(int((i + 1) / total * 100))
        finally:
            self.log.emit(f"回滚完成：成功 {ok} / 失败 {fail}")
            self.finished.emit({"ok": ok, "fail": fail, "fails": fails})


class LegacyMergeRenameWorker(QThread):
    """批量把旧的「番号 合并.容器」改名为「番号.容器」，并同步 NFO / Jellyfin。"""

    log = Signal(str)
    progress = Signal(int)
    item_done = Signal(str, bool, str)  # path, ok, final_path/detail
    finished = Signal(dict)

    def __init__(self, items: list, server: str = "", api_key: str = "",
                 parent=None):
        super().__init__(parent)
        self._items = list(items)
        self._server = server or ""
        self._api_key = api_key or ""
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        from utils import jellyfin_api
        ok = fail = 0
        fails = []
        total = len(self._items)
        try:
            for i, item in enumerate(self._items):
                if self._stopped:
                    break
                path = item.get("path") or ""
                success, detail, final_path = finalize_legacy_merged_output(path)
                self.item_done.emit(path, success, final_path if success else detail)
                if success:
                    ok += 1
                    self.log.emit(f"✅ {item.get('display_number') or path} → {final_path}")
                else:
                    fail += 1
                    fails.append((path, detail))
                    self.log.emit(f"❌ {item.get('display_number') or path}: {detail}")
                if total:
                    self.progress.emit(int((i + 1) / total * 100))

            if self._server and self._api_key:
                self.log.emit("⏳ 请求 Jellyfin 全库元数据刷新…")
                try:
                    jf_ok, jf_msg = jellyfin_api.refresh_library(
                        self._server, self._api_key)
                    self.log.emit(("✅ " if jf_ok else "⚠️ ") + jf_msg)
                except Exception as e:  # noqa: BLE001
                    self.log.emit(f"⚠️ Jellyfin 刷新失败: {e}")
            elif self._server and not self._api_key:
                self.log.emit("⚠️ 未配置 Jellyfin API Key，跳过自动同步，请在软件设置中填写后手动刷新。")
        finally:
            self.log.emit(f"旧合并整理完成：成功 {ok} / 失败 {fail}")
            self.finished.emit({"ok": ok, "fail": fail, "fails": fails})


class JellyfinRefreshWorker(QThread):
    """替换或整理完成后，请求 Jellyfin 全库元数据刷新。"""

    log = Signal(str)
    finished = Signal(dict)

    def __init__(self, server: str, api_key: str, parent=None):
        super().__init__(parent)
        self._server = server or ""
        self._api_key = api_key or ""

    def stop(self) -> None:
        pass

    def run(self) -> None:
        from utils import jellyfin_api
        if not self._server or not self._api_key:
            self.finished.emit({"ok": False, "message": "未配置 Jellyfin API Key"})
            return
        try:
            ok, message = jellyfin_api.refresh_library(self._server, self._api_key)
            self.log.emit(("✅ " if ok else "⚠️ ") + message)
            self.finished.emit({"ok": ok, "message": message})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"⚠️ Jellyfin 刷新失败: {e}")
            self.finished.emit({"ok": False, "message": str(e)})
