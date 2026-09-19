"""破解视频自动替换页面 — 匹配 restored 视频并覆盖媒体库原文件。"""

import os
import shutil
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QSplitter,
)

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, LogPanel, InfoCard
from utils.censor_tags import update_jellyfin_db_tags
from utils.naming import clean_restored_name
from utils.nfo import update_nfo_censor_tags
from utils.nfo_source import find_nfo


class ReplaceWorker(QThread):
    """后台替换线程 — 索引原库后匹配 restored 文件。

    安全策略：先把原文件备份为 .bak，新文件落位成功后再删除备份；
    新文件落位失败时自动回滚备份。
    """

    log = Signal(str)
    finished = Signal(int, int)  # 成功数, 未匹配数

    def __init__(self, restored_paths: list[str], library_paths: list[str],
                 marker: str = "restored", cfg: ToolkitConfig | None = None,
                 parent=None):
        super().__init__(parent)
        self._restored = restored_paths
        self._library = library_paths
        self._marker = marker
        self._cfg = cfg
        self._db_backed_up = False
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        self.log.emit("🔍 第一步：扫描原始媒体库，建立索引...")

        video_exts = {".mp4", ".mkv", ".avi", ".ts", ".mov", ".wmv"}
        original_index: dict[str, str] = {}
        for lib_path in self._library:
            if self._stopped:
                return
            p = Path(lib_path)
            if p.is_file() and p.suffix.lower() in video_exts:
                original_index[p.name] = str(p)
            elif p.is_dir():
                try:
                    for f in p.rglob("*"):
                        if self._stopped:
                            return
                        if f.is_file() and f.suffix.lower() in video_exts:
                            original_index[f.name] = str(f)
                except OSError:
                    continue

        self.log.emit(f"✅ 索引完成：共记录 {len(original_index)} 个原版视频")

        # 收集待替换文件（支持文件与文件夹混合）
        tasks: list[str] = []
        for rest in self._restored:
            if self._stopped:
                return
            p = Path(rest)
            if p.is_file():
                tasks.append(str(p))
            elif p.is_dir():
                try:
                    for f in p.rglob("*"):
                        if self._stopped:
                            return
                        if f.is_file():
                            tasks.append(str(f))
                except OSError:
                    continue

        self.log.emit("🔍 第二步：匹配并替换...")
        replaced = 0
        not_found = 0

        for src in tasks:
            if self._stopped:
                return

            filename = os.path.basename(src)
            # 只处理带破解标记的 mp4
            if self._marker.lower() not in filename.lower():
                continue
            if not filename.lower().endswith(".mp4"):
                continue

            clean_name = clean_restored_name(filename, self._marker)
            if not clean_name:
                self.log.emit(f"  ⚠️ 无法识别破解标记: {filename}")
                not_found += 1
                continue

            self.log.emit(f"▶ {filename} → 寻找原文件: {clean_name}")

            if clean_name in original_index:
                target = original_index[clean_name]
                bak = target + ".bak"
                try:
                    # 1) 备份原文件
                    if os.path.exists(bak):
                        os.remove(bak)
                    if os.path.exists(target):
                        shutil.move(target, bak)
                    # 2) 新文件落位
                    shutil.move(src, target)
                    # 3) 落位成功，清理备份
                    if os.path.exists(bak):
                        os.remove(bak)
                    self._sync_censor_metadata(target)
                    self.log.emit(f"  ✅ 替换成功 → {target}")
                    replaced += 1
                    del original_index[clean_name]
                except Exception as e:
                    # 4) 失败回滚备份
                    if os.path.exists(bak) and not os.path.exists(target):
                        try:
                            shutil.move(bak, target)
                            self.log.emit("  ↩️ 已回滚原文件")
                        except Exception:
                            self.log.emit(f"  ⚠️ 回滚失败，备份保留在 {bak}")
                    self.log.emit(f"  ❌ 替换失败: {e}")
                    not_found += 1
            else:
                self.log.emit(f"  ⚠️ 未找到原文件 [{clean_name}]，已跳过")
                not_found += 1

        self.finished.emit(replaced, not_found)

    def _sync_censor_metadata(self, target: str) -> None:
        """替换成功后同步 NFO 与 Jellyfin 数据库的“有码 → 无码破解”。"""
        nfo = find_nfo(target)
        if nfo:
            ok, detail = update_nfo_censor_tags(nfo)
            self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"NFO 标签：{detail}")
        else:
            self.log.emit("  ⚠️ 未找到同名 NFO，跳过 NFO 标签同步")

        if not self._cfg:
            self.log.emit("  ⚠️ 未提供配置，跳过 Jellyfin 数据库标签同步")
            return
        db_path = getattr(self._cfg, "jellyfin_db_path", "")
        if not db_path or not Path(db_path).exists():
            self.log.emit(
                "  ⚠️ 未找到本地库文件，跳过数据库标签同步"
                f"（配置值：{db_path or '空'}）。\n"
                "     换名后的条目会在服务器下次扫描时自动更新；"
                "若使用 Emby 或「服务器 API」模式，请在服务器端刷新媒体库。")
            return
        backup = not self._db_backed_up
        self._db_backed_up = True
        ok, detail = update_jellyfin_db_tags(
            db_path, target, backup=backup, log=self.log.emit)
        self.log.emit(("  ✅ " if ok else "  ⚠️ ") + f"Jellyfin 数据库：{detail}")


class ReplacePage(QWidget):
    """破解视频自动替换页面。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: ReplaceWorker | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("破解视频自动替换")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "工作原理：自动识别文件名中含 'restored' 的新视频，去除标记后匹配媒体库原文件并替换。\n"
            "左侧拖入 Lada 等工具破解完成的新视频 | 右侧拖入原始媒体库文件/文件夹\n"
            "替换前会自动备份原文件，失败时自动回滚，安全可靠。"
        )
        layout.addWidget(hint)

        splitter = QSplitter()
        left_w = QWidget()
        left_l = QVBoxLayout(left_w)
        left_l.setContentsMargins(0, 0, 6, 0)
        left_l.addWidget(QLabel("📦 破解完成的视频/文件夹:"))
        self.list_restored = DragDropListWidget()
        self.list_restored.files_dropped.connect(self._add_to_restored)
        left_l.addWidget(self.list_restored)
        splitter.addWidget(left_w)

        right_w = QWidget()
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(6, 0, 0, 0)
        right_l.addWidget(QLabel("📚 原始媒体库 视频/文件夹:"))
        self.list_library = DragDropListWidget()
        self.list_library.files_dropped.connect(self._add_to_library)
        right_l.addWidget(self.list_library)
        splitter.addWidget(right_w)
        splitter.setSizes([400, 400])
        layout.addWidget(splitter)

        layout.addWidget(QLabel("处理日志:"))
        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(200)
        layout.addWidget(self.log_panel)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("🚀 开始自动替换")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        btn_row.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_stop)

        btn_row.addStretch()
        clear_btn = QPushButton("清空队列")
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self.stats = QLabel("")
        self.stats.setProperty("cssClass", "muted-label")
        layout.addWidget(self.stats)

    # ── 拖拽接收 ──
    def _add_to_restored(self, paths: list[str]) -> None:
        for p in paths:
            self.list_restored.add_item(os.path.basename(p) or p, p)

    def _add_to_library(self, paths: list[str]) -> None:
        for p in paths:
            self.list_library.add_item(os.path.basename(p) or p, p)

    def _start(self) -> None:
        if self.list_restored.count() == 0 or self.list_library.count() == 0:
            QMessageBox.warning(self, "提示", "请确保两侧都拖入了文件或文件夹！")
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.clear()

        restored = self.list_restored.get_all_paths()
        library = self.list_library.get_all_paths()

        self._worker = ReplaceWorker(
            restored, library, self.cfg.restored_suffix_marker, self.cfg)
        self._worker.log.connect(self.log_panel.log)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.log_panel.log_warning("任务已停止")
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def _on_finished(self, replaced: int, not_found: int) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log_panel.log("─" * 50)
        self.log_panel.log_success(f"✅ 完成！替换: {replaced} | 未匹配/失败: {not_found}")
        self.stats.setText(f"本次: 成功 {replaced} | 未匹配 {not_found}")

    def _clear(self) -> None:
        self.list_restored.clear()
        self.list_library.clear()
        self.log_panel.clear()
        self.stats.setText("")

    def shutdown(self) -> None:
        """应用退出时调用：停止后台替换线程。"""
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
