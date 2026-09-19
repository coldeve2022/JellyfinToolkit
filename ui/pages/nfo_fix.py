"""NFO 关联修复页面 — 修复 .ts → .mp4 后的 NFO 文件和关联资源。"""

import os

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QFileDialog, QMessageBox,
)

from ui.widgets import LogPanel, InfoCard
from utils.nfo import read_text_any_encoding, fix_nfo_content, nfo_new_filename, safe_rename


class NFOWorker(QThread):
    """后台 NFO 修复线程。"""

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(int, int)  # 内容修正数, 关联文件重命名数

    def __init__(self, directory: str, parent=None):
        super().__init__(parent)
        self._dir = directory
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        fixed = 0
        renamed = 0

        all_files = []
        for root, dirs, files in os.walk(self._dir):
            for f in files:
                all_files.append((root, f))

        total = len(all_files)
        for i, (root, f) in enumerate(all_files):
            if self._stopped:
                return
            old_path = os.path.join(root, f)

            # 1) 重命名关联文件：xxx.ts.nfo / xxx.ts-poster.jpg → .mp4 版本
            new_name = nfo_new_filename(f)
            if new_name and new_name != f:
                new_path = os.path.join(root, new_name)
                try:
                    safe_rename(old_path, new_path)
                    self.log.emit(f"[重命名] {f} → {new_name}")
                    renamed += 1
                    old_path = new_path  # 后续按新路径处理
                except OSError as e:
                    self.log.emit(f"[重命名失败] {f}: {e}")

            # 2) 修正 NFO 文件内部内容
            if old_path.lower().endswith(".nfo"):
                content = read_text_any_encoding(old_path)
                if content is None:
                    self.log.emit(f"[跳过] 无法解码 {os.path.basename(old_path)}")
                else:
                    new_content = fix_nfo_content(content)
                    if new_content is not None:
                        try:
                            with open(old_path, "w", encoding="utf-8") as fh:
                                fh.write(new_content)
                            self.log.emit(f"[修正] {os.path.basename(old_path)}")
                            fixed += 1
                        except OSError as e:
                            self.log.emit(f"[写入失败] {os.path.basename(old_path)}: {e}")

            self.progress.emit(int((i + 1) / total * 100))

        self.finished.emit(fixed, renamed)


class NFOPage(QWidget):
    """NFO 关联修复页面 — 支持拖入目录。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        # 必须保存：_ai_check_nfo() 会读 self.cfg.ai_enabled / ai_base_url 等
        self.cfg = cfg
        self._dir: str | None = None
        self._worker: NFOWorker | None = None
        self.setAcceptDrops(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("NFO 关联修复")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        hint = InfoCard(
            "在 .ts 转码为 .mp4 后，关联的 NFO 文件、封面图片仍指向旧文件名。\n"
            "本工具会自动：① 重命名关联文件 (.ts.nfo → .mp4.nfo)\n"
            "② 修正 NFO 文件内部的 .ts 引用为 .mp4\n"
            "拖入媒体库根目录，或点击下方按钮选择目录。"
        )
        layout.addWidget(hint)

        dir_row = QHBoxLayout()
        self.dir_label = QLabel("未选择目录")
        self.dir_label.setProperty("cssClass", "path-display")
        dir_row.addWidget(self.dir_label, 1)

        browse_btn = QPushButton("选择目录")
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        layout.addWidget(QLabel("处理日志:"))
        self.log_panel = LogPanel()
        layout.addWidget(self.log_panel)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("开始修复")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        self.btn_start.setEnabled(False)
        btn_row.addWidget(self.btn_start)

        self.btn_ai = QPushButton("🤖 冲突 NFO 检查")
        self.btn_ai.setToolTip("扫描目录，找出同一视频存在多个 NFO 的目录，用 AI 判断哪个最优、哪些冗余（只给建议，不自动删）。")
        self.btn_ai.clicked.connect(self._ai_check_nfo)
        btn_row.addWidget(self.btn_ai)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    # ── AI 冲突 NFO 检查 ──
    def _ai_check_nfo(self) -> None:
        from workers.ai import query_llm
        if not self.cfg.ai_enabled:
            QMessageBox.information(self, "未启用 AI",
                                    "请在「设置」→「本地 AI 二次校验」启用本地 LLM。")
            return
        if not self._dir:
            QMessageBox.information(self, "未选目录", "请先选择媒体库目录。")
            return
        conflicts = []
        for root, _, files in os.walk(self._dir):
            nfos = [f for f in files if f.lower().endswith(".nfo")]
            if len(nfos) >= 2:
                conflicts.append((root, nfos))
        if not conflicts:
            QMessageBox.information(self, "无冲突", "未发现同一目录存在多个 NFO 的情况。")
            return
        self.btn_ai.setEnabled(False)
        self.log_panel.log_warning(
            f"发现 {len(conflicts)} 个含多个 NFO 的目录，AI 分析前 {min(len(conflicts), 8)} 个…")
        try:
            for root, nfos in conflicts[:8]:
                lines = [f"目录 {root}", "NFO 文件："] + [f"  - {n}" for n in nfos]
                prompt = (
                    "以下是同一视频目录下的多个 NFO 文件。请判断哪个 NFO 内容最完整/规范(最优)应保留，"
                    "哪些是冗余/空壳可移入回收站。只输出『最优: <文件名>；冗余: <文件名列表> - 一句话理由』。"
                    "基于文件名与常见刮削习惯判断。\n\n" + "\n".join(lines))
                text = query_llm(self.cfg.ai_base_url, self.cfg.ai_key, self.cfg.ai_model,
                                 [{"role": "user", "content": prompt}], timeout=90)
                self.log_panel.log(str(text)[:300])
        finally:
            self.btn_ai.setEnabled(True)
        self.log_panel.log_success("AI 冲突检查完成（建议仅供参考，不自动改动）。")

    # ── 拖拽支持 ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()


    def dropEvent(self, event):
        try:
            for url in event.mimeData().urls():
                p = url.toLocalFile()
                if p and os.path.isdir(p):
                    event.acceptProposedAction()
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(0, self, lambda: self._set_dir(p))
                    return
            event.ignore()
        except Exception:
            event.ignore()

    def _set_dir(self, d: str) -> None:
        self._dir = d
        self.dir_label.setText(d)
        self.btn_start.setEnabled(True)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择媒体库目录")
        if d:
            self._set_dir(d)

    def _start(self) -> None:
        if not self._dir:
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.log_panel.clear()
        self.progress.setValue(0)

        self._worker = NFOWorker(self._dir)
        self._worker.log.connect(self.log_panel.log)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def shutdown(self) -> None:
        """应用退出时调用：停止后台修复线程。"""
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)

    def _on_finished(self, fixed: int, renamed: int) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.log_panel.log("─" * 50)
        self.log_panel.log_success(f"✅ 完成！修正 NFO 内容: {fixed} | 重命名关联文件: {renamed}")
