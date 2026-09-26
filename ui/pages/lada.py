"""马赛克破解页（Lada）—— 驱动第三方的 ``lada-cli.exe``。

设计要点
--------
- **不写死任何预设/模型/设备名**：点「读取能力」后，下拉项全部来自
  ``lada-cli.exe`` 在这台机器上的真实输出（预设、检测模型、修复模型、设备）。
  用户看到的就是能用的，不会选了之后才发现名字不存在。
- **产出直接落在视频同目录**（默认），文件名 ``xxx.restored.mp4`` ——
  这样现有的「破解视频替换」页能直接认出它，两页串成一条流水线。
- **做完成校验**：Lada 偶尔会以退出码 0 结束却没写出可用文件，
  只信退出码会把"静默失败"当成功，所以产出后用 ffprobe 对一下时长。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from config import ToolkitConfig
from ui.widgets import DragDropListWidget, InfoCard, LogPanel
from utils import cpu_affinity, gpu, lada_tool
from workers.lada_restore import LadaProbeWorker, LadaRestoreWorker


class _CensoredScan(QThread):
    """从媒体库里挑出「有码」的视频（与「视频有码检测」页同一套判定）。"""

    log = Signal(str)
    done = Signal(list)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self) -> None:
        from utils import library_source

        try:
            items = library_source.collect(self.cfg, log=self.log.emit)
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"读取媒体库失败：{e}")
            self.done.emit([])
            return
        picked = []
        for it in items:
            if not it.path or not Path(it.path).is_file():
                continue
            if getattr(it, "is_uncensored", False):
                continue
            # 已经破解过的就不再重复排队（默认命名规则）
            out_dir = Path(self.cfg.lada_output_dir) if self.cfg.lada_output_dir \
                else Path(it.path).parent
            if lada_tool.expected_output(out_dir, it.path).is_file():
                continue
            picked.append(it.path)
        self.log.emit(f"库里共 {len(items)} 个条目，其中 {len(picked)} 个判定为有码且尚未破解")
        self.done.emit(picked)


class LadaPage(QWidget):
    """马赛克破解（需要用户自行准备 Lada）。"""

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.worker: LadaRestoreWorker | None = None
        self.probe_worker: LadaProbeWorker | None = None
        self.scanner: _CensoredScan | None = None
        self.tool_options = None
        self._ffprobe = "ffprobe"
        self._setup_ui()
        self.input_affinity.setEnabled(
            (self.cfg.lada_cpu_affinity or "auto") == "custom")
        self._refresh_tool_status()

    # ── 界面 ──
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("马赛克破解（Lada）")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        layout.addWidget(InfoCard(
            "把库里的有码视频批量送去破解，产出 `原文件名.restored.mp4`，"
            "随后可以用「破解视频替换」页替换回库中对应位置。\n"
            "本程序**不包含**破解引擎：请先自行准备第三方工具 Lada，"
            "然后用下面的「自动检测」告诉我在哪。\n"
            "第一次用建议先点「读取能力」——下拉里会填上你这台机器真正可用的"
            "编码预设、检测模型与设备。"))

        layout.addWidget(self._build_tool_group())
        layout.addWidget(self._build_param_group())
        layout.addWidget(self._build_files_group(), 1)
        layout.addWidget(self._build_control_group())
        self.log_panel = LogPanel()
        self.log_panel.setMaximumHeight(180)
        layout.addWidget(self.log_panel)

    def _build_tool_group(self) -> QGroupBox:
        box = QGroupBox("破解工具（第三方程序，需自行准备）")
        lay = QVBoxLayout(box)

        row = QHBoxLayout()
        row.addWidget(QLabel("lada-cli.exe："))
        self.input_tool = QLineEdit(self.cfg.lada_cli_path)
        self.input_tool.setPlaceholderText(
            "填 lada-cli.exe 的完整路径、或 Lada 解压后的文件夹；留空则自动查找")
        row.addWidget(self.input_tool, 1)
        for text, slot in (("浏览…", self._browse_tool),
                           ("自动检测", self._autodetect),
                           ("读取能力", self._probe)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
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
        box = QGroupBox("参数（下拉项来自「读取能力」的真实结果）")
        lay = QVBoxLayout(box)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("设备："))
        self.combo_device = QComboBox()
        self._fill_combo(self.combo_device, [], self.cfg.lada_device)
        r1.addWidget(self.combo_device)

        r1.addWidget(QLabel("编码预设："))
        self.combo_preset = QComboBox()
        self._fill_combo(self.combo_preset, [], self.cfg.lada_encoding_preset,
                         empty_label="Lada 默认")
        r1.addWidget(self.combo_preset)
        r1.addStretch()
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("检测模型："))
        self.combo_detect = QComboBox()
        self._fill_combo(self.combo_detect, [], self.cfg.lada_detection_model,
                         empty_label="Lada 默认")
        r2.addWidget(self.combo_detect)

        r2.addWidget(QLabel("修复模型："))
        self.combo_restore = QComboBox()
        self._fill_combo(self.combo_restore, [], self.cfg.lada_restoration_model,
                         empty_label="Lada 默认")
        r2.addWidget(self.combo_restore)

        r2.addWidget(QLabel("片段长度："))
        self.spin_clip = QSpinBox()
        self.spin_clip.setRange(0, 2000)
        self.spin_clip.setSpecialValueText("默认")
        self.spin_clip.setValue(int(self.cfg.lada_max_clip_length or 0))
        self.spin_clip.setToolTip("0 = 用 Lada 默认（180 帧）。调高提升时序稳定性，更吃显存")
        r2.addWidget(self.spin_clip)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        self.chk_fp16 = QCheckBox("FP16（省显存）")
        self.chk_fp16.setChecked(self.cfg.lada_fp16)
        r3.addWidget(self.chk_fp16)
        self.chk_face = QCheckBox("跳过面部马赛克")
        self.chk_face.setChecked(self.cfg.lada_detect_face_mosaics)
        self.chk_face.setToolTip("v3 及更新的检测模型支持；可能降低对 NSFW 区域的识别率")
        r3.addWidget(self.chk_face)
        self.chk_validate = QCheckBox("产出后校验时长")
        self.chk_validate.setChecked(self.cfg.lada_validate_output)
        self.chk_validate.setToolTip("用 ffprobe 对比输入/输出时长，防止把静默失败的产物当成功")
        r3.addWidget(self.chk_validate)
        r3.addWidget(QLabel("并发："))
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 4)
        self.spin_workers.setValue(int(self.cfg.lada_parallel_workers or 1))
        self.spin_workers.setToolTip("每个并发各占一份显存；显存不够就保持 1")
        r3.addWidget(self.spin_workers)
        r3.addStretch()
        lay.addLayout(r3)

        r4 = QHBoxLayout()
        r4.addWidget(QLabel("输出目录："))
        self.input_out = QLineEdit(self.cfg.lada_output_dir)
        self.input_out.setPlaceholderText("留空 = 与视频同目录（推荐，便于「破解视频替换」直接接手）")
        r4.addWidget(self.input_out, 1)
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(lambda: self._pick_dir(self.input_out))
        r4.addWidget(btn_out)
        lay.addLayout(r4)

        # ── 性能相关的两项：都只对特定硬件有意义，所以默认保守、可关、可手填 ──
        r5 = QHBoxLayout()
        self.chk_gate = QCheckBox("显存门控")
        self.chk_gate.setChecked(self.cfg.lada_vram_gate)
        self.chk_gate.setToolTip(
            "开着时：显存占用超过「高水位」就暂缓启动新任务，低于「低水位」再继续。\n"
            "适合边玩游戏边挂机，代价是任务会看起来「停住了」。\n"
            "默认关闭 —— 阈值与显卡容量有关，可点右边的按钮按本机推算。")
        r5.addWidget(self.chk_gate)
        r5.addWidget(QLabel("高水位："))
        self.spin_high = QDoubleSpinBox()
        self.spin_high.setRange(1.0, 96.0)
        self.spin_high.setSingleStep(0.5)
        self.spin_high.setValue(float(self.cfg.lada_vram_high or 10.5))
        self.spin_high.setSuffix(" GB")
        r5.addWidget(self.spin_high)
        r5.addWidget(QLabel("低水位："))
        self.spin_low = QDoubleSpinBox()
        self.spin_low.setRange(0.5, 96.0)
        self.spin_low.setSingleStep(0.5)
        self.spin_low.setValue(float(self.cfg.lada_vram_low or 8.5))
        self.spin_low.setSuffix(" GB")
        r5.addWidget(self.spin_low)
        btn_rec = QPushButton("按本机显卡推荐")
        btn_rec.setToolTip("读取显卡总显存，按比例推算一组阈值（会同时填好上下两个值）")
        btn_rec.clicked.connect(self._recommend_vram)
        r5.addWidget(btn_rec)
        r5.addStretch()
        lay.addLayout(r5)

        r6 = QHBoxLayout()
        r6.addWidget(QLabel("CPU 绑定："))
        self.combo_affinity = QComboBox()
        self.combo_affinity.addItem("自动探测 P 核", "auto")
        self.combo_affinity.addItem("不绑定（交给系统）", "off")
        self.combo_affinity.addItem("自定义核心", "custom")
        idx = self.combo_affinity.findData(self.cfg.lada_cpu_affinity or "auto")
        self.combo_affinity.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_affinity.setToolTip(
            "把破解进程绑定到指定 CPU 核心。\n"
            "· 只对 Intel 12 代及以后的混合架构（P 核 + E 核）有明显效果；\n"
            "· 其他 CPU 上会自动退回「全部核心」（等同于不绑）；\n"
            "· 绑错了可能更慢 —— 拿不准就选「不绑定」。")
        self.combo_affinity.currentIndexChanged.connect(self._on_affinity_changed)
        r6.addWidget(self.combo_affinity)

        self.input_affinity = QLineEdit(self.cfg.lada_cpu_affinity_cores)
        self.input_affinity.setPlaceholderText("如 0-11 或 0-5,12-15")
        self.input_affinity.setToolTip("线程号从 0 开始；范围和逗号都可以写")
        r6.addWidget(self.input_affinity, 1)
        self.btn_detect_affinity = QPushButton("检测")
        self.btn_detect_affinity.setToolTip("读一次本机 CPU 拓扑，看看自动模式会绑到哪些核心")
        self.btn_detect_affinity.clicked.connect(self._detect_affinity)
        r6.addWidget(self.btn_detect_affinity)
        r6.addStretch()
        lay.addLayout(r6)

        self.lbl_perf = QLabel()
        self.lbl_perf.setWordWrap(True)
        self.lbl_perf.setProperty("cssClass", "subtitle")
        lay.addWidget(self.lbl_perf)
        self._refresh_perf_hint()
        return box

    def _build_files_group(self) -> QGroupBox:
        box = QGroupBox("待破解视频（可直接拖入）")
        lay = QVBoxLayout(box)
        row = QHBoxLayout()
        for text, slot in (("添加文件", self._add_files), ("添加目录", self._add_dir)):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        self.btn_scan = QPushButton("挑出库里判定为有码的视频")
        self.btn_scan.setToolTip("与「视频有码检测」同一套判定；已破解过的会自动跳过")
        self.btn_scan.clicked.connect(self._scan)
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
        self.btn_start = QPushButton("▶ 开始破解")
        self.btn_start.setProperty("cssClass", "accent")
        self.btn_start.clicked.connect(self._start)
        row.addWidget(self.btn_start)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        row.addWidget(self.btn_stop)
        self.btn_replace = QPushButton("去「破解视频替换」")
        self.btn_replace.setToolTip("破解完成后用它把产物替换回库中对应位置")
        self.btn_replace.clicked.connect(lambda: self._goto("破解视频替换"))
        row.addWidget(self.btn_replace)
        row.addStretch()
        self.lbl_status = QLabel("就绪")
        row.addWidget(self.lbl_status)
        lay.addLayout(row)
        return wrap

    # ── 工具定位与探测 ──
    # ── 性能相关的小工具（只做「告诉你 + 帮你填」，不替用户拍板）──
    def _on_affinity_changed(self) -> None:
        mode = self.combo_affinity.currentData()
        self.input_affinity.setEnabled(mode == "custom")
        if mode == "custom" and not self.input_affinity.text().strip():
            info = cpu_affinity.detect_affinity("auto")
            self.input_affinity.setText(cpu_affinity.format_core_spec(info.get("cores")))
        self._refresh_perf_hint()

    def _detect_affinity(self) -> None:
        info = cpu_affinity.detect_affinity("auto")
        self.log_panel.log_info("CPU 拓扑检测：" + cpu_affinity.describe_affinity(info))
        self.log_panel.log("    " + info.get("reason", ""))
        if info.get("cores"):
            self.log_panel.log(
                "    如果这个结果不对，把「CPU 绑定」改成「自定义核心」，"
                "填上你想用的线程号（如 0-11）。")
        self._refresh_perf_hint()

    def _recommend_vram(self) -> None:
        total = gpu.vram_total_gb()
        if total <= 0:
            self.log_panel.log_warning(
                "读不到显卡显存（需要 NVIDIA 驱动自带的 nvidia-smi）。"
                "请按自己显卡的容量手动填 —— 参考比例：高水位约 87%、低水位约 71%。")
            return
        high, low = gpu.recommend_vram_thresholds(total)
        self.spin_high.setValue(high)
        self.spin_low.setValue(low)
        used = gpu.vram_used_gb()
        self.log_panel.log_success(
            f"按本机显卡（{total:.0f} GB）推算：高水位 {high:.1f} GB / 低水位 {low:.1f} GB"
            + (f"；当前已用 {used:.1f} GB" if used >= 0 else ""))

    def _refresh_perf_hint(self) -> None:
        """把这两项"程序实际会怎么做"写在界面上 —— 用户才知道要不要改。"""
        info = cpu_affinity.detect_affinity(
            self.combo_affinity.currentData() or "auto", self.input_affinity.text())
        total = gpu.vram_total_gb()
        vram = f"显卡 {total:.0f} GB" if total > 0 else "显存读不到"
        gate = "开启" if self.chk_gate.isChecked() else "关闭"
        self.lbl_perf.setText(
            "这两项都只对特定硬件有意义，拿不准就用默认值：\n"
            f"· CPU 绑定 → {cpu_affinity.describe_affinity(info)}\n"
            f"· 显存门控 → {gate}（{vram}；阈值可点「按本机显卡推荐」自动填）")

    def _fill_combo(self, combo: QComboBox, rows: list, current: str,
                    empty_label: str = "（默认）") -> None:
        combo.clear()
        combo.addItem(empty_label, "")
        for row in rows:
            name = row.get("name") or ""
            if not name:
                continue
            desc = row.get("description") or ""
            combo.addItem(f"{name} — {desc[:40]}" if desc else name, name)
        idx = combo.findData(current)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif current:
            combo.addItem(current, current)
            combo.setCurrentIndex(combo.count() - 1)

    def _pick_dir(self, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录", target.text().strip())
        if path:
            target.setText(path)

    def _browse_tool(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 lada-cli.exe", "", "可执行文件 (*.exe);;所有文件 (*)")
        if not path:
            return
        self.input_tool.setText(path)
        self.tool_options = None
        self._save_tool_path()
        self._refresh_tool_status()

    def _save_tool_path(self) -> None:
        value = self.input_tool.text().strip()
        if value != self.cfg.lada_cli_path:
            self.cfg.lada_cli_path = value
            try:
                self.cfg.save()
            except Exception as e:  # noqa: BLE001
                self.log_panel.log_warning(f"路径已生效，但写入配置失败：{e}")

    def _autodetect(self) -> None:
        """三档查找 + 把过程写进日志（原实现只认固定目录名，实际找不到）。"""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.log_panel.log_info("正在查找 lada-cli.exe（会扫一遍各盘的浅层目录，通常几秒）…")
        QApplication.processEvents()
        try:
            info = lada_tool.search_lada_cli(self.input_tool.text().strip(),
                                             log=self.log_panel.log)
        finally:
            QApplication.restoreOverrideCursor()

        found = info.get("path")
        if not found:
            self.log_panel.log_warning(
                f"没有找到 lada-cli.exe（扫过 {info.get('scanned', 0)} 个目录，"
                f"用时 {info.get('elapsed', 0):.1f} 秒）。下面告诉你手动怎么指定。")
            self._show_hint()
            return
        self.input_tool.setText(str(found))
        self.tool_options = None
        self._save_tool_path()
        self._refresh_tool_status()
        self.log_panel.log_success(f"已定位：{found}（{info.get('source', '')}）")

    def _probe(self) -> None:
        cli = lada_tool.find_lada_cli(self.input_tool.text().strip())
        if not cli:
            self._show_hint()
            return
        if self.probe_worker and self.probe_worker.isRunning():
            return
        self._save_tool_path()
        self.btn_start.setEnabled(False)
        self.probe_worker = LadaProbeWorker(cli)
        self.probe_worker.log.connect(self.log_panel.log)
        self.probe_worker.done.connect(self._on_probe_done)
        self.probe_worker.finished.connect(lambda: self.btn_start.setEnabled(True))
        self.probe_worker.start()

    def _on_probe_done(self, info: dict) -> None:
        if not info.get("ok") and not info.get("presets"):
            self.log_panel.log_error(info.get("error") or "读取能力失败")
            return
        self.tool_options = info.get("options") or None
        self._fill_combo(self.combo_preset, info.get("presets", []),
                         self.cfg.lada_encoding_preset, "Lada 默认")
        self._fill_combo(self.combo_detect, info.get("detection_models", []),
                         self.cfg.lada_detection_model, "Lada 默认")
        self._fill_combo(self.combo_restore, info.get("restoration_models", []),
                         self.cfg.lada_restoration_model, "Lada 默认")
        self._fill_combo(self.combo_device, info.get("devices", []),
                         self.cfg.lada_device, "Lada 默认")
        for row in info.get("presets", [])[:20]:
            self.log_panel.log(f"    预设 {row['name']}：{row['description'][:70]}")
        for row in info.get("devices", []):
            self.log_panel.log(f"    设备 {row['name']}：{row['description']}")
        self._refresh_tool_status()
        self.log_panel.log_success("能力已刷新：下拉项现在都是这台机器上真实可用的。")

    def _refresh_tool_status(self) -> None:
        cli = lada_tool.find_lada_cli(self.input_tool.text().strip())
        text = lada_tool.describe_cli(cli)
        if cli and self.tool_options is None:
            text += "　（建议点一下「读取能力」以填充可用的预设/模型）"
        self.lbl_tool.setText("当前工具：" + text)

    def _show_hint(self) -> None:
        QMessageBox.information(self, "怎么获取破解工具", lada_tool.prerequisites_hint())

    def _goto(self, label: str) -> None:
        window = self.window()
        navigate = getattr(window, "_navigate", None)
        if not callable(navigate):
            return
        from ui.main_window import NAV_LABELS
        if label in NAV_LABELS:
            navigate(NAV_LABELS.index(label))

    def _goto_settings(self) -> None:
        self._goto("设置")

    # ── 文件清单 ──
    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择视频", "",
            "视频 (*.mp4 *.mkv *.avi *.mov *.ts *.wmv *.flv *.webm);;所有文件 (*)")
        self._add_paths(paths)

    def _add_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录")
        if path:
            self._add_paths([path])

    def _add_paths(self, paths) -> None:
        exts = {e.lower() for e in (self.cfg.video_extensions or [])}
        exts |= {".mp4", ".mkv", ".avi", ".mov", ".ts", ".wmv", ".flv", ".webm", ".m4v"}
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

    def _scan(self) -> None:
        if self.scanner and self.scanner.isRunning():
            return
        self.btn_scan.setEnabled(False)
        self.log_panel.log_info("正在从媒体库挑出有码视频…")
        self.scanner = _CensoredScan(self.cfg)
        self.scanner.log.connect(self.log_panel.log)
        self.scanner.done.connect(self._on_scan_done)
        self.scanner.finished.connect(lambda: self.btn_scan.setEnabled(True))
        self.scanner.start()

    def _on_scan_done(self, paths: list) -> None:
        if not paths:
            self.log_panel.log_warning("没有找到待破解的视频（或媒体库读取失败）。")
            return
        self._add_paths(paths)

    # ── 运行 ──
    def _start(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        cli = lada_tool.find_lada_cli(self.input_tool.text().strip())
        if not cli:
            self._show_hint()
            return
        files = [self.list_files.item(i).text() for i in range(self.list_files.count())]
        if not files:
            QMessageBox.warning(self, "没有任务", "请先添加视频，"
                                             "或用「挑出库里判定为有码的视频」自动填充。")
            return
        if int(self.spin_workers.value()) > 1:
            ok = QMessageBox.question(
                self, "并发提醒",
                f"你将同时跑 {self.spin_workers.value()} 个破解任务。\n"
                "每个任务各占一份显存，显存不足时反而会更慢甚至失败。\n\n"
                "确定继续吗？")
            if ok != QMessageBox.Yes:
                return
        self._apply_params()
        self.progress.setValue(0)
        self.worker = LadaRestoreWorker(self.cfg, files, cli, self.tool_options,
                                        self._ffprobe)
        self.worker.log.connect(self.log_panel.log)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.file_progress.connect(self._on_file_progress)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
        self._set_running(True)

    def _apply_params(self) -> None:
        self.cfg.lada_device = self.combo_device.currentData() or self.cfg.lada_device
        self.cfg.lada_encoding_preset = self.combo_preset.currentData() or ""
        self.cfg.lada_detection_model = self.combo_detect.currentData() or ""
        self.cfg.lada_restoration_model = self.combo_restore.currentData() or ""
        self.cfg.lada_max_clip_length = int(self.spin_clip.value())
        self.cfg.lada_fp16 = self.chk_fp16.isChecked()
        self.cfg.lada_detect_face_mosaics = self.chk_face.isChecked()
        self.cfg.lada_validate_output = self.chk_validate.isChecked()
        self.cfg.lada_parallel_workers = int(self.spin_workers.value())
        self.cfg.lada_cpu_affinity = self.combo_affinity.currentData() or "auto"
        self.cfg.lada_cpu_affinity_cores = self.input_affinity.text().strip()
        self.cfg.lada_output_dir = self.input_out.text().strip()
        self.cfg.lada_vram_gate = self.chk_gate.isChecked()
        self.cfg.lada_vram_high = float(self.spin_high.value())
        self.cfg.lada_vram_low = float(self.spin_low.value())
        try:
            self.cfg.save()
        except Exception as e:  # noqa: BLE001
            self.log_panel.log_warning(f"参数已生效，但写入配置失败：{e}")

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.lbl_status.setText("正在停止（会连同子进程一起结束）…")

    def _on_file_progress(self, path: str, pct: int) -> None:
        self.lbl_status.setText(f"{Path(path).name} — {pct}%")

    def _on_file_done(self, path: str, ok: bool, note: str) -> None:
        if not ok and note:
            self.log_panel.log_error(f"    {Path(path).name}：{note}")

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
                                f"共 {len(stats['fails'])} 个失败：\n\n{detail}\n\n"
                                "产物若已生成会保留，可自行确认。")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_scan.setEnabled(not running)
        self.btn_replace.setEnabled(not running)
        for w in (self.combo_device, self.combo_preset, self.combo_detect,
                  self.combo_restore, self.spin_clip, self.spin_workers,
                  self.combo_affinity, self.btn_detect_affinity):
            w.setEnabled(not running)
        self.input_affinity.setEnabled(
            not running and self.combo_affinity.currentData() == "custom")
        if running:
            self.lbl_status.setText("正在运行…")

    def set_ffprobe(self, path: str) -> None:
        """主窗口在探测到 ffprobe 后注入（产出校验用）。"""
        self._ffprobe = path or "ffprobe"

    def shutdown(self) -> None:
        """主窗口关闭时调用：停掉队列与探测，避免留下后台进程。"""
        for w in (self.worker, self.probe_worker, self.scanner):
            if w and w.isRunning():
                stop = getattr(w, "stop", None)
                if callable(stop):
                    stop()
                w.wait(8000)
