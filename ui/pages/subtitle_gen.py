"""批量生成字幕页 —— 驱动第三方的 Faster-Whisper / TransWithAI 工具。

页面本身只是一个"编排器"：真正识别字幕的是用户自己装的 ``infer.exe``
（见 :mod:`utils.whisper_tool` 的说明）。所以这一页有三件事必须做好：

1. **工具位置可配、可自动探测**，并且把"去哪儿下、要什么前置"直接写在界面上；
2. **进度与日志要能看**，因为一个文件可能要跑几分钟；
3. **和库里的其它功能联动** —— 最实用的是「一键挑出库里缺字幕的视频」，
   不用手工一个个拖进来。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, InfoCard, LogPanel
from utils import subtitle_clean, whisper_tool
from workers.whisper_gen import WhisperGenWorker


class _MissingSubtitleScan(QThread):
    """在后台从媒体库里挑出「没有字幕的视频」—— 页面联动用。"""

    log = Signal(str)
    done = Signal(list)

    def __init__(self, cfg, formats, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.formats = formats or ["srt"]

    def run(self) -> None:
        from utils import library_source

        try:
            items = library_source.collect(self.cfg, log=self.log.emit)
        except Exception as e:  # noqa: BLE001  采集失败只该提示，不该崩页面
            self.log.emit(f"读取媒体库失败：{e}")
            self.done.emit([])
            return
        self.log.emit(f"库里共 {len(items)} 个条目，正在检查字幕…")
        missing = []
        checker = getattr(self.cfg, "subtitle_extensions", None) or [".srt"]
        want = [str(f).lstrip(".") for f in self.formats]
        for it in items:
            if not it.path or not Path(it.path).is_file():
                continue
            found = subtitle_clean.find_subtitles(
                it.path, want, self.cfg.whisper_output_dir or None)
            if not found:
                missing.append(it.path)
        self.log.emit(f"其中 {len(missing)} 个没有同名字幕文件"
                      f"（按扩展名 {'/'.join(want)} 判断；设置里的"
                      f"「字幕扩展名」共 {len(checker)} 项）")
        self.done.emit(missing)


class SubtitleGenPage(QWidget):
    """批量生成字幕。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.worker: WhisperGenWorker | None = None
        self.scanner: _MissingSubtitleScan | None = None
        #: 工具支持的选项集合；None = 还没探测过
        self.tool_options = None
        self._setup_ui()
        self._refresh_tool_status()

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("批量生成字幕")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        layout.addWidget(InfoCard(
            "给库里没字幕的视频批量生成字幕（识别 + 可选翻译由第三方引擎完成）。\n"
            "本程序**不包含**识别引擎：请先自行准备「Faster-Whisper / TransWithAI」"
            "打包版（下面有下载指引），然后用这里的「自动检测」或「浏览」告诉我在哪。\n"
            "联动用法：点「挑出库里缺字幕的视频」→ 确认清单 → 直接开始，"
            "比手工一个个拖进来快得多。"))

        layout.addWidget(self._build_tool_group())
        layout.addWidget(self._build_param_group())
        layout.addWidget(self._build_files_group(), 1)
        layout.addWidget(self._build_control_group())
        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(180)
        layout.addWidget(self.log_panel)

    def _build_tool_group(self) -> QGroupBox:
        box = QGroupBox("识别工具（第三方程序，需自行准备）")
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("infer.exe："))
        self.input_tool = QLineEdit(self.cfg.whisper_tool_path)
        self.input_tool.setPlaceholderText(
            "填 infer.exe 的完整路径，或它所在的文件夹；留空则自动查找")
        row.addWidget(self.input_tool, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_tool)
        row.addWidget(btn_browse)
        btn_detect = QPushButton("自动检测")
        btn_detect.clicked.connect(self._autodetect)
        row.addWidget(btn_detect)
        btn_probe = QPushButton("探测能力")
        btn_probe.setToolTip("读一次 infer.exe --help，确认这个版本支持哪些参数")
        btn_probe.clicked.connect(self._probe)
        row.addWidget(btn_probe)
        lay.addLayout(row)

        self.lbl_tool = QLabel()
        self.lbl_tool.setWordWrap(True)
        self.lbl_tool.setProperty("cssClass", "subtitle")
        lay.addWidget(self.lbl_tool)

        row2 = QHBoxLayout()
        btn_help = QPushButton("怎么获取？")
        btn_help.clicked.connect(self._show_hint)
        row2.addWidget(btn_help)
        btn_settings = QPushButton("打开设置")
        btn_settings.clicked.connect(self._goto_settings)
        row2.addWidget(btn_settings)
        row2.addStretch()
        lay.addLayout(row2)
        return box

    def _build_param_group(self) -> QGroupBox:
        box = QGroupBox("参数（留空/默认 = 交给引擎自己决定）")
        lay = QVBoxLayout(box)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("设备："))
        self.combo_device = QComboBox()
        for val, text in (("cuda", "GPU（cuda，快）"), ("cpu", "CPU（慢但不挑显卡）"),
                          ("auto", "自动")):
            self.combo_device.addItem(text, val)
        self._select(self.combo_device, self.cfg.whisper_device)
        r1.addWidget(self.combo_device)

        r1.addWidget(QLabel("模型："))
        self.combo_model = QComboBox()
        self.combo_model.setEditable(True)
        self.combo_model.addItem("引擎自带默认", "")
        for m in whisper_tool.MODEL_CHOICES[1:]:
            self.combo_model.addItem(m, m)
        self._select(self.combo_model, self.cfg.whisper_model)
        r1.addWidget(self.combo_model)

        r1.addWidget(QLabel("输出格式："))
        self.combo_formats = QComboBox()
        self.combo_formats.setEditable(True)
        for v in ("srt", "srt,vtt", "srt,vtt,txt", "srt,vtt,lrc"):
            self.combo_formats.addItem(v, v)
        self._select(self.combo_formats, self.cfg.whisper_sub_formats)
        r1.addWidget(self.combo_formats)
        r1.addStretch()
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("输出目录："))
        self.input_out = QLineEdit(self.cfg.whisper_output_dir)
        self.input_out.setPlaceholderText("留空 = 与视频同目录（推荐，Jellyfin 直接能读）")
        r2.addWidget(self.input_out, 1)
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(lambda: self._pick_dir(self.input_out))
        r2.addWidget(btn_out)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.chk_overwrite = QCheckBox("覆盖已有字幕")
        self.chk_overwrite.setChecked(self.cfg.whisper_overwrite)
        r3.addWidget(self.chk_overwrite)
        self.chk_batch = QCheckBox("批处理加速（显存不足就关掉）")
        self.chk_batch.setChecked(self.cfg.whisper_batching)
        r3.addWidget(self.chk_batch)
        r3.addWidget(QLabel("批大小："))
        self.spin_batch = QSpinBox()
        self.spin_batch.setRange(1, 64)
        self.spin_batch.setValue(max(1, int(self.cfg.whisper_batch_size or 8)))
        r3.addWidget(self.spin_batch)
        self.chk_cleanup = QCheckBox("生成后去重 / 拆长句")
        self.chk_cleanup.setChecked(self.cfg.whisper_cleanup)
        r3.addWidget(self.chk_cleanup)
        r3.addStretch()
        lay.addLayout(r3)
        return box

    def _build_files_group(self) -> QGroupBox:
        box = QGroupBox("待处理文件（可直接把文件/文件夹拖进来）")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        btn_add = QPushButton("添加文件")
        btn_add.clicked.connect(self._add_files)
        row.addWidget(btn_add)
        btn_dir = QPushButton("添加目录")
        btn_dir.clicked.connect(self._add_dir)
        row.addWidget(btn_dir)
        self.btn_scan = QPushButton("挑出库里缺字幕的视频")
        self.btn_scan.setToolTip("从媒体库读取全部条目，挑出没有同名字幕的，填进下面的清单")
        self.btn_scan.clicked.connect(self._scan_missing)
        row.addWidget(self.btn_scan)
        row.addStretch()
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self._remove_selected)
        row.addWidget(btn_remove)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._clear)
        row.addWidget(btn_clear)
        lay.addLayout(row)

        self.list_files = DragDropListWidget()
        self.list_files.files_dropped.connect(self._add_paths)
        lay.addWidget(self.list_files, 1)

        self.lbl_count = QLabel("共 0 个文件")
        self.lbl_count.setProperty("cssClass", "subtitle")
        lay.addWidget(self.lbl_count)
        return box

    def _build_control_group(self) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        lay.addWidget(self.progress)

        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 开始生成")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        row.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        row.addWidget(self.btn_stop)
        row.addStretch()
        self.lbl_status = QLabel("就绪")
        row.addWidget(self.lbl_status)
        lay.addLayout(row)
        return wrap

    # ── 小工具 ──
    def _select(self, combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx < 0 and combo.isEditable():
            combo.setEditText(value or "")
        elif idx >= 0:
            combo.setCurrentIndex(idx)

    def _pick_dir(self, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录", target.text().strip())
        if path:
            target.setText(path)

    def _browse_tool(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 infer.exe（或它所在的文件夹里的任意文件）",
            "", "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self.input_tool.setText(path)
            self.tool_options = None
            self._save_tool_path()
            self._refresh_tool_status()

    def _save_tool_path(self) -> None:
        """把路径即时写回配置 —— 用户不必记得再点一次"保存设置"。"""
        value = self.input_tool.text().strip()
        if value != self.cfg.whisper_tool_path:
            self.cfg.whisper_tool_path = value
            try:
                self.cfg.save()
            except Exception as e:  # noqa: BLE001
                self.log_panel.log_warning(f"路径已生效，但写入配置失败：{e}")

    def _autodetect(self) -> None:
        found = whisper_tool.find_infer_exe(self.input_tool.text().strip())
        if not found:
            self.log_panel.log_warning("自动检测没找到 infer.exe。")
            self._show_hint()
            return
        self.input_tool.setText(str(found))
        self.tool_options = None
        self._save_tool_path()
        self._refresh_tool_status()
        self.log_panel.log_success(f"已定位：{found}")

    def _probe(self) -> None:
        exe = whisper_tool.find_infer_exe(self.input_tool.text().strip())
        if not exe:
            self._show_hint()
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = whisper_tool.probe(exe)
        finally:
            QApplication.restoreOverrideCursor()
        if not info["ok"]:
            self.log_panel.log_error(f"探测失败：{info['error']}")
            return
        self.tool_options = info["options"]
        self.log_panel.log_success(
            f"探测成功：这个版本支持 {len(info['options'])} 个参数")
        for name in sorted(info["options"]):
            self.log_panel.log("    " + name)
        self._refresh_tool_status()

    def _refresh_tool_status(self) -> None:
        exe = whisper_tool.find_infer_exe(self.input_tool.text().strip())
        text = whisper_tool.describe_exe(exe)
        if exe and self.tool_options is None:
            text += "　（建议点一下「探测能力」，参数会按实际支持情况发送）"
        self.lbl_tool.setText("当前工具：" + text)

    def _show_hint(self) -> None:
        QMessageBox.information(self, "怎么获取识别工具",
                                whisper_tool.prerequisites_hint())

    def _goto_settings(self) -> None:
        window = self.window()
        navigate = getattr(window, "_navigate", None)
        if not callable(navigate):
            return
        from ui.main_window import NAV_LABELS
        if "设置" in NAV_LABELS:
            navigate(NAV_LABELS.index("设置"))

    # ── 文件清单 ──
    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择媒体文件", "",
            "媒体文件 (*.mp4 *.mkv *.avi *.mov *.ts *.wmv *.flv *.mp3 *.wav *.m4a);;所有文件 (*)")
        self._add_paths(paths)

    def _add_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录")
        if path:
            self._add_paths([path])

    def _add_paths(self, paths) -> None:
        exts = {it.lower() for it in (self.cfg.video_extensions or [])}
        exts |= {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"}
        existing = {self.list_files.item(i).text()
                    for i in range(self.list_files.count())}
        added = 0
        for raw in paths or []:
            p = Path(str(raw))
            if p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and f.suffix.lower() in exts and str(f) not in existing:
                        self.list_files.addItem(str(f))
                        existing.add(str(f))
                        added += 1
            elif p.is_file() and str(p) not in existing:
                self.list_files.addItem(str(p))
                existing.add(str(p))
                added += 1
        if added:
            self.log_panel.log_success(f"已添加 {added} 个文件")
        self._update_count()

    def _remove_selected(self) -> None:
        for item in self.list_files.selectedItems():
            self.list_files.takeItem(self.list_files.row(item))
        self._update_count()

    def _clear(self) -> None:
        self.list_files.clear()
        self._update_count()

    def _update_count(self) -> None:
        self.lbl_count.setText(f"共 {self.list_files.count()} 个文件")

    def _scan_missing(self) -> None:
        if self.scanner and self.scanner.isRunning():
            return
        self.btn_scan.setEnabled(False)
        self.log_panel.log_info("正在从媒体库挑出缺字幕的视频…")
        formats = [f.strip() for f in
                   (self.combo_formats.currentData() or "srt").split(",") if f.strip()]
        self.scanner = _MissingSubtitleScan(self.cfg, formats)
        self.scanner.log.connect(self.log_panel.log)
        self.scanner.done.connect(self._on_scan_done)
        self.scanner.finished.connect(lambda: self.btn_scan.setEnabled(True))
        self.scanner.start()

    def _on_scan_done(self, paths: list) -> None:
        if not paths:
            self.log_panel.log_warning("没有找到缺字幕的视频（或媒体库读取失败）。")
            return
        self._add_paths(paths)

    # ── 运行 ──
    def _start(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        exe = whisper_tool.find_infer_exe(self.input_tool.text().strip())
        if not exe:
            self._show_hint()
            return
        files = [self.list_files.item(i).text() for i in range(self.list_files.count())]
        if not files:
            QMessageBox.warning(self, "没有任务", "请先添加要处理的文件，"
                                             "或用「挑出库里缺字幕的视频」自动填充。")
            return
        self._apply_params()
        self.progress.setValue(0)
        self.worker = WhisperGenWorker(self.cfg, files, exe, self.tool_options)
        self.worker.log.connect(self.log_panel.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
        self._set_running(True)

    def _apply_params(self) -> None:
        self.cfg.whisper_device = self.combo_device.currentData() or "cuda"
        self.cfg.whisper_model = self.combo_model.currentData() or \
            self.combo_model.currentText().strip()
        self.cfg.whisper_sub_formats = self.combo_formats.currentData() or \
            self.combo_formats.currentText().strip()
        self.cfg.whisper_output_dir = self.input_out.text().strip()
        self.cfg.whisper_overwrite = self.chk_overwrite.isChecked()
        self.cfg.whisper_batching = self.chk_batch.isChecked()
        self.cfg.whisper_batch_size = self.spin_batch.value()
        self.cfg.whisper_cleanup = self.chk_cleanup.isChecked()
        try:
            self.cfg.save()
        except Exception as e:  # noqa: BLE001
            self.log_panel.log_warning(f"参数已生效，但写入配置失败：{e}")

    def _toggle_pause(self) -> None:
        if not self.worker:
            return
        if self.btn_pause.text().startswith("⏸"):
            self.worker.pause()
            self.btn_pause.setText("▶ 继续")
        else:
            self.worker.resume()
            self.btn_pause.setText("⏸ 暂停")

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.lbl_status.setText("正在停止…")

    def _on_file_done(self, path: str, ok: bool, note: str) -> None:
        if ok:
            self.lbl_status.setText(f"完成：{Path(path).name}")
        else:
            self.lbl_status.setText(f"失败：{Path(path).name}")

    def _on_finished(self, stats: dict) -> None:
        self._set_running(False)
        if stats.get("cancelled"):
            self.lbl_status.setText(f"已停止（成功 {stats['ok']} / 失败 {stats['fail']}）")
        else:
            self.lbl_status.setText(f"全部完成：成功 {stats['ok']} / 失败 {stats['fail']}")
            if stats.get("fails"):
                detail = "\n".join(f"· {Path(p).name}：{why}"
                                  for p, why in stats["fails"][:10])
                QMessageBox.warning(self, "部分文件失败",
                                    f"共 {len(stats['fails'])} 个失败：\n\n{detail}")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_scan.setEnabled(not running)
        self.chk_batch.setEnabled(not running)
        self.spin_batch.setEnabled(not running)
        if running:
            self.btn_pause.setText("⏸ 暂停")
            self.lbl_status.setText("正在运行…")

    # ── 退出时收尾 ──
    def shutdown(self) -> None:
        """主窗口关闭时调用：停掉正在跑的队列，避免留下后台进程。"""
        for w in (self.worker, self.scanner):
            if w and w.isRunning():
                stop = getattr(w, "stop", None)
                if callable(stop):
                    stop()
                w.wait(8000)

