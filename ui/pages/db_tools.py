"""数据库工具页面 — 跨库播放记录转移 + 数据库结构检查。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QLineEdit, QTextEdit, QMessageBox, QGroupBox,
    QFileDialog,
)
from PySide6.QtCore import QThread, Signal

from config import ToolkitConfig
from ui.widgets import LogPanel
from workers.database import CrossDBTransferWorker


class DBInspectWorker(QThread):
    """数据库结构检查线程。"""

    result = Signal(str)

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._stopped = False

    def stop(self) -> None:
        """请求中止：真实库表多、行数大，逐个 COUNT(*) 可能很慢，
        没有中止手段的话，关窗时线程停不下来会触发
        "QThread: Destroyed while thread is still running" 直接崩掉进程。"""
        self._stopped = True

    def run(self) -> None:
        if not os.path.exists(self._db_path):
            self.result.emit("❌ 数据库文件不存在！")
            return

        conn = None
        try:
            conn = sqlite3.connect(self._db_path)
            c = conn.cursor()

            lines = []
            c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [t[0] for t in c.fetchall() if not t[0].startswith("sqlite_")]
            lines.append(f"📊 共 {len(tables)} 个表: {', '.join(tables)}")
            lines.append("")

            for table in tables:
                if self._stopped:
                    lines.append("\n（已中止）")
                    return
                c.execute(f"SELECT COUNT(*) FROM [{table}]")
                count = c.fetchone()[0]
                c.execute(f"PRAGMA table_info([{table}])")
                cols = [(col[1], col[2]) for col in c.fetchall()]
                col_str = ", ".join(f"{n}({t})" for n, t in cols[:10])
                if len(cols) > 10:
                    col_str += f" ... (+{len(cols)-10})"
                lines.append(f"  {table}: {count} 行 | {col_str}")

            # 检查 .ts 残留
            try:
                c.execute("SELECT COUNT(*) FROM BaseItems WHERE Path LIKE '%.ts'")
                ts = c.fetchone()[0]
                lines.append(f"\n⚠️  仍有 {ts} 条 .ts 记录残留在 BaseItems 表中")
            except Exception:
                pass

            # 抽查 UserData 真实数据（收藏/播放进度/评分等）
            try:
                lines.append("\n📋 UserData 抽查（有收藏或进度的记录，最多 5 条）:")
                c.execute("""
                    SELECT ItemId, CustomDataKey, IsFavorite, PlaybackPositionTicks,
                           PlayCount, Rating, LastPlayedDate
                    FROM UserData
                    WHERE IsFavorite = 1 OR PlaybackPositionTicks > 0
                    LIMIT 5
                """)
                rows = c.fetchall()
                if not rows:
                    c.execute(
                        "SELECT ItemId, CustomDataKey, IsFavorite, "
                        "PlaybackPositionTicks, PlayCount, Rating, LastPlayedDate "
                        "FROM UserData LIMIT 5"
                    )
                    rows = c.fetchall()
                for row in rows:
                    item_id, key, fav, pos, count, rating, played = row
                    lines.append(
                        f"  ItemId={item_id} | Key={key} | 收藏={fav} "
                        f"| 进度={pos} | 次数={count} | 评分={rating} | 看过={played}"
                    )
                if not rows:
                    lines.append("  该库暂无任何 UserData 记录")
            except Exception:
                pass

            if not self._stopped:
                self.result.emit("\n".join(lines))

        except Exception as e:
            if not self._stopped:
                self.result.emit(f"❌ 检查异常: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


class DBToolsPage(QWidget):
    """数据库工具页面。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._transfer_worker: CrossDBTransferWorker | None = None
        self._inspector: DBInspectWorker | None = None
        self._setup_ui()

    def _initial_db_path(self) -> str:
        """优先用配置里的路径；没配就用自动解析（含自动检测）出来的库文件路径。"""
        if getattr(self.cfg, "jellyfin_db_path", ""):
            return self.cfg.jellyfin_db_path
        from utils import library_source
        try:
            kind, opts = library_source.resolve(self.cfg)
        except library_source.SourceUnavailable:
            return ""
        return str(opts.get("path", "")) if kind == library_source.SOURCE_DB else ""

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        title = QLabel("数据库工具")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        # ── 数据库路径 ──
        # 本页要**写**库文件（跨库转移播放记录），所以必须拿到真实的本地库文件；
        # 「服务器 API」模式下没有库文件，这里就留空让用户自己「浏览」。
        path_group = QGroupBox("媒体库数据库路径（Jellyfin / Emby）")
        path_lay = QHBoxLayout(path_group)
        self.db_path_input = QLineEdit(self._initial_db_path())
        self.db_path_input.setPlaceholderText(
            "跨库转移需要本地库文件；Emby 或服务器在别的机器时此功能不可用")
        path_lay.addWidget(self.db_path_input)
        browse_btn = QPushButton("浏览")
        browse_btn.clicked.connect(self._browse_db)
        path_lay.addWidget(browse_btn)
        layout.addWidget(path_group)

        # ── 跨库转移 ──
        trans_group = QGroupBox("🔀 跨库播放记录转移 (ts → mp4)")
        trans_lay = QVBoxLayout(trans_group)

        trans_hint = QLabel(
            "当 .ts 视频转码为 .mp4 后，旧库中的播放记录、收藏、进度会丢失。\n"
            "本功能自动从旧库备份中提取播放数据，注入到新库对应 .mp4 记录中。"
        )
        trans_hint.setProperty("cssClass", "subtitle")
        trans_lay.addWidget(trans_hint)

        self.trans_log = LogPanel()
        self.trans_log.setMaximumHeight(180)
        trans_lay.addWidget(self.trans_log)

        self.trans_progress = QProgressBar()
        self.trans_progress.setValue(0)
        trans_lay.addWidget(self.trans_progress)

        btn_row = QHBoxLayout()
        self.btn_transfer = QPushButton("开始跨库转移")
        self.btn_transfer.setProperty("cssClass", "accent")
        self.btn_transfer.clicked.connect(self._start_transfer)
        btn_row.addWidget(self.btn_transfer)

        self.btn_trans_stop = QPushButton("停止")
        self.btn_trans_stop.setEnabled(False)
        self.btn_trans_stop.clicked.connect(self._stop_transfer)
        btn_row.addWidget(self.btn_trans_stop)
        btn_row.addStretch()
        trans_lay.addLayout(btn_row)

        layout.addWidget(trans_group)

        # ── 数据库检查 ──
        inspect_group = QGroupBox("🔍 数据库结构检查")
        inspect_lay = QVBoxLayout(inspect_group)

        self.inspect_output = QTextEdit()
        self.inspect_output.setReadOnly(True)
        self.inspect_output.setMaximumHeight(220)
        inspect_lay.addWidget(self.inspect_output)

        inspect_btn = QPushButton("检查数据库结构")
        inspect_btn.clicked.connect(self._inspect_db)
        inspect_lay.addWidget(inspect_btn)

        layout.addWidget(inspect_group)
        layout.addStretch()

    def _browse_db(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Jellyfin 数据库", "",
            "SQLite 数据库 (*.db);;所有文件 (*.*)",
        )
        if path:
            self.db_path_input.setText(path)

    def _get_data_dir(self) -> str:
        db_path = self.db_path_input.text()
        return str(Path(db_path).parent)

    # ── 跨库转移 ──
    def _start_transfer(self) -> None:
        data_dir = self._get_data_dir()
        if not os.path.exists(os.path.join(data_dir, "jellyfin.db")):
            QMessageBox.warning(self, "错误", "所选路径下找不到 jellyfin.db！")
            return

        self.btn_transfer.setEnabled(False)
        self.btn_trans_stop.setEnabled(True)
        self.trans_log.clear()
        self.trans_progress.setValue(0)

        self._transfer_worker = CrossDBTransferWorker(data_dir)
        self._transfer_worker.log.connect(self.trans_log.log)
        self._transfer_worker.progress.connect(self.trans_progress.setValue)
        self._transfer_worker.finished.connect(self._on_transfer_done)
        self._transfer_worker.start()

    def _stop_transfer(self) -> None:
        if self._transfer_worker and self._transfer_worker.isRunning():
            self._transfer_worker.stop()
            self.btn_transfer.setEnabled(True)
            self.btn_trans_stop.setEnabled(False)

    def _on_transfer_done(self, stats: dict) -> None:
        self.btn_transfer.setEnabled(True)
        self.btn_trans_stop.setEnabled(False)
        self.trans_progress.setValue(100)

    # ── 数据库检查 ──
    def _inspect_db(self) -> None:
        if self._inspector and self._inspector.isRunning():
            return  # 防重复点击启动多个线程
        db_path = self.db_path_input.text()
        self._inspector = DBInspectWorker(db_path)
        self._inspector.result.connect(self.inspect_output.setPlainText)
        self._inspector.start()

    def shutdown(self) -> None:
        """应用退出时调用：停止所有后台线程。"""
        if self._transfer_worker and self._transfer_worker.isRunning():
            self._transfer_worker.stop()
            self._transfer_worker.wait(3000)
        if self._inspector and self._inspector.isRunning():
            self._inspector.stop()
            if not self._inspector.wait(5000):
                # 还没停就再等一轮；绝不让线程活着离开，否则退出即崩
                self._inspector.wait()
