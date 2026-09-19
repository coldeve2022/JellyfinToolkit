"""设置页面 — 配置管理。"""

import sys

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QSpinBox, QTextEdit, QGroupBox, QMessageBox,
    QFileDialog, QScrollArea, QComboBox, QCheckBox, QDoubleSpinBox,
    QApplication,
)

from config import ToolkitConfig
from ui import theme
from utils import library_source, media_server


class SettingsPage(QWidget):
    """全局配置页面。"""

    theme_changed = Signal(str)   # 用户切换主题时发射

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._setup_ui()

    def _setup_ui(self) -> None:
        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        title = QLabel("设置")
        title.setProperty("cssClass", "page-title")
        layout.addWidget(title)

        # ── 外观设置 ──
        look_group = QGroupBox("外观主题")
        lg = QVBoxLayout(look_group)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("界面主题:"))
        self.combo_theme = QComboBox()
        for name in theme.theme_names():
            self.combo_theme.addItem(theme.THEMES[name]["name"], name)
        idx = self.combo_theme.findData(self.cfg.theme)
        if idx >= 0:
            self.combo_theme.setCurrentIndex(idx)
        # 选择即预览（切换主题可随时保存）
        self.combo_theme.currentIndexChanged.connect(self._preview_theme)
        theme_row.addWidget(self.combo_theme)
        theme_row.addStretch()
        lg.addLayout(theme_row)

        apply_hint = QLabel("选择后立即预览，点击底部「保存设置」生效并记住。")
        apply_hint.setProperty("cssClass", "subtitle")
        lg.addWidget(apply_hint)
        layout.addWidget(look_group)

        # ── 媒体库数据源 ──
        path_group = QGroupBox("媒体库数据源（Jellyfin / Emby）")
        pg = QVBoxLayout(path_group)

        r0 = QHBoxLayout()
        r0.addWidget(QLabel("数据来源:"))
        self.combo_source = QComboBox()
        for value, label, tip in library_source.SOURCE_CHOICES:
            self.combo_source.addItem(label, value)
            self.combo_source.setItemData(
                self.combo_source.count() - 1, tip, Qt.ToolTipRole)
        idx = self.combo_source.findData(library_source.normalize_source(self.cfg.library_source))
        if idx >= 0:
            self.combo_source.setCurrentIndex(idx)
        self.combo_source.currentIndexChanged.connect(lambda _=0: self._refresh_source_hint())
        r0.addWidget(self.combo_source)
        r0.addSpacing(16)
        r0.addWidget(QLabel("服务端类型:"))
        self.combo_server_kind = QComboBox()
        for value, label in media_server.KIND_CHOICES:
            self.combo_server_kind.addItem(label, value)
        kidx = self.combo_server_kind.findData(media_server.normalize_kind(self.cfg.server_type))
        if kidx >= 0:
            self.combo_server_kind.setCurrentIndex(kidx)
        r0.addWidget(self.combo_server_kind)
        r0.addStretch()
        pg.addLayout(r0)

        r1 = QHBoxLayout()
        r1.addWidget(QLabel("数据库路径:"))
        self.input_db = QLineEdit(self.cfg.jellyfin_db_path)
        self.input_db.setPlaceholderText("留空 = 自动检测（Jellyfin / Emby 的常见安装位置都会扫）")
        r1.addWidget(self.input_db)
        b1 = QPushButton("浏览")
        b1.clicked.connect(lambda: self._browse(
            self.input_db, "选择 jellyfin.db / library.db", "SQLite (*.db);;*.*"))
        r1.addWidget(b1)
        self.btn_detect_db = QPushButton("自动检测")
        self.btn_detect_db.clicked.connect(self._detect_library)
        r1.addWidget(self.btn_detect_db)
        pg.addLayout(r1)

        r2 = QHBoxLayout()
        r2.addWidget(QLabel("数据目录:"))
        self.input_data = QLineEdit(self.cfg.jellyfin_data_dir)
        r2.addWidget(self.input_data)
        b2 = QPushButton("浏览")
        b2.clicked.connect(lambda: self._browse(self.input_data, "选择 data 目录"))
        r2.addWidget(b2)
        pg.addLayout(r2)

        self.lbl_source_now = QLabel("")
        self.lbl_source_now.setProperty("cssClass", "subtitle")
        self.lbl_source_now.setWordWrap(True)
        pg.addWidget(self.lbl_source_now)

        r2b = QHBoxLayout()
        r2b.addWidget(QLabel("清理排除目录关键词(逗号分隔):"))
        self.input_exclude = QLineEdit(", ".join(self.cfg.exclude_path_keywords))
        r2b.addWidget(self.input_exclude)
        pg.addLayout(r2b)

        layout.addWidget(path_group)

        # ── 评分阈值 ──
        th_group = QGroupBox("智能评分阈值")
        th_box = QVBoxLayout(th_group)
        th_row = QHBoxLayout()
        th_row.addWidget(QLabel("「可能喜欢」命中度 ≥ %:"))
        self.spin_th_keep = QSpinBox()
        self.spin_th_keep.setRange(40, 100)
        self.spin_th_keep.setValue(self.cfg.score_th_keep)
        th_row.addWidget(self.spin_th_keep)
        th_row.addSpacing(16)
        th_row.addWidget(QLabel("「建议删除」命中度 ≤ %:"))
        self.spin_th_delete = QSpinBox()
        self.spin_th_delete.setRange(5, 50)
        self.spin_th_delete.setValue(self.cfg.score_th_delete)
        th_row.addWidget(self.spin_th_delete)
        th_row.addStretch()
        th_box.addLayout(th_row)
        th_hint = QLabel('命中度 = 作品内容特征命中你"喜欢史"的比例。阈值越激进，建议删除越多、待复核越少。')
        th_hint.setProperty("cssClass", "subtitle")
        th_box.addWidget(th_hint)
        layout.addWidget(th_group)

        # ── 增强匹配开关 ──
        enh_group = QGroupBox("增强匹配（可对比优化前后）")
        enh_box = QVBoxLayout(enh_group)
        self.check_enhance = QCheckBox("启用：相似锚点 + 题材共现 + AI 语义注入 + 反馈循环")
        self.check_enhance.setChecked(self.cfg.enhance_match)
        enh_box.addWidget(self.check_enhance)

        eng_row = QHBoxLayout()
        eng_row.addWidget(QLabel("评分引擎:"))
        self.combo_engine = QComboBox()
        self.combo_engine.addItem("v2 · 分桶画像+置信度加权+邻域传播（推荐，更准）", "v2")
        self.combo_engine.addItem("v1 · 基础命中度（保留用于对比）", "v1")
        idx = self.combo_engine.findData(self.cfg.engine)
        if idx >= 0:
            self.combo_engine.setCurrentIndex(idx)
        eng_row.addWidget(self.combo_engine)
        eng_row.addStretch()
        enh_box.addLayout(eng_row)

        enh_hint = QLabel(
            "开着时：评分额外叠加「与你喜欢作品的整体相似(锚点) + 你常用题材组合的共现」，"
            "并自动用 AI 提炼的语义标签参与命中；你打标后的反馈立即重排融入画像。\n"
            "关掉后：评分退回仅基于基础命中度（不含上述增强）。\n"
            "方便你开/关各跑一次对比优化前后的准确度。")
        enh_hint.setProperty("cssClass", "subtitle")
        enh_hint.setWordWrap(True)
        enh_box.addWidget(enh_hint)
        layout.addWidget(enh_group)

        # ── 本地 AI 二次校验 ──
        ai_group = QGroupBox("本地 AI 二次校验（可选）")
        aib = QVBoxLayout(ai_group)
        ar1 = QHBoxLayout()
        self.ai_enabled = QCheckBox("启用（用本地 LLM 复核删除候选 / 生成画像深度报告）")
        self.ai_enabled.setChecked(self.cfg.ai_enabled)
        ar1.addWidget(self.ai_enabled)
        ar1.addStretch()
        aib.addLayout(ar1)
        ar2 = QHBoxLayout()
        ar2.addWidget(QLabel("接口地址:"))
        self.input_ai_url = QLineEdit(self.cfg.ai_base_url)
        ar2.addWidget(self.input_ai_url, 1)
        ar2.addWidget(QLabel("模型:"))
        self.input_ai_model = QLineEdit(self.cfg.ai_model)
        self.input_ai_model.setFixedWidth(200)
        ar2.addWidget(self.input_ai_model)
        self.btn_ai_detect = QPushButton("🔎 检测可用模型")
        self.btn_ai_detect.setToolTip(
            "读取 Ollama /api/tags，列出本机已拉取的模型；"
            "优先选中默认模型（若已拉取），避免填了不存在的模型名导致 404。")
        self.btn_ai_detect.clicked.connect(self._detect_models)
        ar2.addWidget(self.btn_ai_detect)
        aib.addLayout(ar2)
        ar3 = QHBoxLayout()
        ar3.addWidget(QLabel("API Key(可选, Ollama留空):"))
        self.input_ai_key = QLineEdit(self.cfg.ai_key)
        ar3.addWidget(self.input_ai_key, 1)
        aib.addLayout(ar3)
        ai_hint = QLabel('使用 OpenAI 兼容接口调用本地模型（如 Ollama: http://localhost:11434/v1）。未启用或连不上时，规则结果照常可用。')
        ai_hint.setProperty("cssClass", "subtitle")
        ai_hint.setWordWrap(True)
        aib.addWidget(ai_hint)
        layout.addWidget(ai_group)

        # ── 无码关键词 ──
        kw_group = QGroupBox("无码检测关键词（每行一个）")
        kw_lay = QVBoxLayout(kw_group)
        self.keywords_edit = QTextEdit()
        self.keywords_edit.setPlainText("\n".join(self.cfg.uncensored_keywords))
        self.keywords_edit.setMaximumHeight(180)
        kw_lay.addWidget(self.keywords_edit)
        layout.addWidget(kw_group)

        # ── 字幕/视频格式 ──
        fmt_group = QGroupBox("格式设置")
        fmt_lay = QVBoxLayout(fmt_group)

        r3 = QHBoxLayout()
        r3.addWidget(QLabel("字幕扩展名 (逗号分隔):"))
        self.input_subs = QLineEdit(", ".join(self.cfg.subtitle_extensions))
        r3.addWidget(self.input_subs)
        fmt_lay.addLayout(r3)

        r4 = QHBoxLayout()
        r4.addWidget(QLabel("视频扩展名 (逗号分隔):"))
        self.input_vids = QLineEdit(", ".join(self.cfg.video_extensions))
        r4.addWidget(self.input_vids)
        fmt_lay.addLayout(r4)

        r5 = QHBoxLayout()
        r5.addWidget(QLabel("最小视频大小 (MB):"))
        self.spin_min = QSpinBox()
        self.spin_min.setRange(0, 10240)
        self.spin_min.setValue(self.cfg.min_video_size_mb)
        r5.addWidget(self.spin_min)
        r5.addStretch()
        fmt_lay.addLayout(r5)

        layout.addWidget(fmt_group)

        # ── FFmpeg 设置 ──
        ff_group = QGroupBox("FFmpeg 转码设置")
        ff_lay = QVBoxLayout(ff_group)

        r5b = QHBoxLayout()
        r5b.addWidget(QLabel("ffmpeg 路径:"))
        self.input_ffmpeg_path = QLineEdit(self.cfg.merge_ffmpeg_path)
        self.input_ffmpeg_path.setPlaceholderText(
            "留空 = 自动查找（程序目录 / PATH / 常见安装位置）")
        r5b.addWidget(self.input_ffmpeg_path, 1)
        b_ff = QPushButton("浏览")
        b_ff.clicked.connect(self._browse_ffmpeg)
        r5b.addWidget(b_ff)
        b_ff_auto = QPushButton("自动检测")
        b_ff_auto.clicked.connect(self._autodetect_ffmpeg)
        r5b.addWidget(b_ff_auto)
        ff_lay.addLayout(r5b)

        # 显示当前**实际生效**的工具路径（不是配置里写了什么）
        self.lbl_tool_status = QLabel("")
        self.lbl_tool_status.setProperty("cssClass", "subtitle")
        self.lbl_tool_status.setWordWrap(True)
        ff_lay.addWidget(self.lbl_tool_status)
        self._refresh_tool_status()

        r6 = QHBoxLayout()
        r6.addWidget(QLabel("GPU 编码器:"))
        self.input_codec = QComboBox()
        self.input_codec.setEditable(True)
        self._fill_codec_combo(self.cfg.ffmpeg_gpu_codec)
        r6.addWidget(self.input_codec, 1)
        r6.addWidget(QLabel("硬解:"))
        self.input_hw = QComboBox()
        self.input_hw.setEditable(True)
        self._fill_hw_combo(self.cfg.ffmpeg_hardware_accel)
        r6.addWidget(self.input_hw, 1)
        b_probe = QPushButton("检测本机能力")
        b_probe.setToolTip("真跑一次编码/解码来确认本机确实支持，"
                           "而不是看 ffmpeg 的编译期列表（那会把没有的显卡也列成支持）")
        b_probe.clicked.connect(self._probe_hardware)
        r6.addWidget(b_probe)
        ff_lay.addLayout(r6)

        hw_hint = QLabel(
            "「自动」= 启动时实测本机能力后再选用；也可手动指定。\n"
            "手工填的编码器若在本机不可用，执行时会自动回退 libx264 并提示，不会直接失败。")
        hw_hint.setProperty("cssClass", "subtitle")
        hw_hint.setWordWrap(True)
        ff_lay.addWidget(hw_hint)

        r7 = QHBoxLayout()
        r7.addWidget(QLabel("转码质量 CRF (15-35):"))
        self.spin_crf = QSpinBox()
        self.spin_crf.setRange(15, 35)
        self.spin_crf.setValue(self.cfg.transcode_crf)
        r7.addWidget(self.spin_crf)

        r7.addWidget(QLabel("Restored 标记:"))
        self.input_marker = QLineEdit(self.cfg.restored_suffix_marker)
        r7.addWidget(self.input_marker)
        ff_lay.addLayout(r7)

        self.btn_doctor = QPushButton("🩺 环境自检")
        self.btn_doctor.setToolTip("打印数据目录、工具路径、实测可用编码器/硬解方式")
        self.btn_doctor.clicked.connect(self._run_doctor)
        r7.addWidget(self.btn_doctor)

        layout.addWidget(ff_group)

        # ── 分集合并设置 ──
        merge_group = QGroupBox("分集合并")
        mg = QVBoxLayout(merge_group)

        mr1 = QHBoxLayout()
        mr1.addWidget(QLabel("输出容器:"))
        self.combo_merge_container = QComboBox()
        for label, value in [("MP4（推荐）", "mp4"), ("MKV", "mkv"), ("TS", "ts"),
                             ("MOV", "mov"), ("AVI", "avi"), ("WebM", "webm")]:
            self.combo_merge_container.addItem(label, value)
        idx = self.combo_merge_container.findData(self.cfg.merge_output_container)
        if idx >= 0:
            self.combo_merge_container.setCurrentIndex(idx)
        mr1.addWidget(self.combo_merge_container)
        mr1.addSpacing(16)
        mr1.addWidget(QLabel("备份根目录:"))
        self.input_merge_backup = QLineEdit(self.cfg.merge_backup_root)
        self.input_merge_backup.setPlaceholderText("留空 = 数据目录下的 backups/")
        self.input_merge_backup.editingFinished.connect(self._refresh_backup_hint)
        mr1.addWidget(self.input_merge_backup, 1)
        b_mb = QPushButton("浏览")
        b_mb.clicked.connect(lambda: self._browse_dir(self.input_merge_backup))
        mr1.addWidget(b_mb)
        mr1.addWidget(QLabel("子目录:"))
        self.input_merge_subfolder = QLineEdit(self.cfg.merge_original_subfolder)
        mr1.addWidget(self.input_merge_subfolder)
        mg.addLayout(mr1)

        self.lbl_backup_hint = QLabel("")
        self.lbl_backup_hint.setProperty("cssClass", "subtitle")
        self.lbl_backup_hint.setWordWrap(True)
        mg.addWidget(self.lbl_backup_hint)
        self._refresh_backup_hint()

        mr2 = QHBoxLayout()
        self.check_merge_rename = QCheckBox("替换时把「番号 合并.mp4」改名为「番号.mp4」")
        self.check_merge_rename.setChecked(self.cfg.merge_rename_to_number)
        mr2.addWidget(self.check_merge_rename)
        mr2.addStretch()
        mr2.addWidget(QLabel("最大分集数:"))
        self.spin_merge_max_parts = QSpinBox()
        self.spin_merge_max_parts.setRange(2, 100)
        self.spin_merge_max_parts.setValue(self.cfg.merge_max_parts)
        mr2.addWidget(self.spin_merge_max_parts)
        mr2.addSpacing(16)
        mr2.addWidget(QLabel("最短分集秒数:"))
        self.spin_merge_min_part = QDoubleSpinBox()
        self.spin_merge_min_part.setRange(0.0, 600.0)
        self.spin_merge_min_part.setDecimals(1)
        self.spin_merge_min_part.setSingleStep(0.5)
        self.spin_merge_min_part.setValue(self.cfg.merge_min_part_seconds)
        mr2.addWidget(self.spin_merge_min_part)
        mr2.addSpacing(16)
        mr2.addWidget(QLabel("单文件超时(秒):"))
        self.spin_merge_timeout = QSpinBox()
        self.spin_merge_timeout.setRange(60, 86400)
        self.spin_merge_timeout.setValue(self.cfg.merge_timeout_per_file)
        mr2.addWidget(self.spin_merge_timeout)
        mg.addLayout(mr2)

        mr3 = QHBoxLayout()
        mr3.addWidget(QLabel("去重抽帧数:"))
        self.spin_merge_dup_frames = QSpinBox()
        self.spin_merge_dup_frames.setRange(1, 30)
        self.spin_merge_dup_frames.setValue(self.cfg.merge_dup_frame_count)
        mr3.addWidget(self.spin_merge_dup_frames)
        mr3.addSpacing(16)
        mr3.addWidget(QLabel("Hamming 距离:"))
        self.spin_merge_dup_distance = QSpinBox()
        self.spin_merge_dup_distance.setRange(1, 32)
        self.spin_merge_dup_distance.setValue(self.cfg.merge_dup_hash_distance)
        mr3.addWidget(self.spin_merge_dup_distance)
        mr3.addSpacing(16)
        mr3.addWidget(QLabel("相似阈值:"))
        self.spin_merge_dup_sim = QDoubleSpinBox()
        self.spin_merge_dup_sim.setRange(0.50, 1.00)
        self.spin_merge_dup_sim.setSingleStep(0.05)
        self.spin_merge_dup_sim.setDecimals(2)
        self.spin_merge_dup_sim.setValue(self.cfg.merge_dup_sim_threshold)
        mr3.addWidget(self.spin_merge_dup_sim)
        mr3.addStretch()
        mg.addLayout(mr3)

        merge_hint = QLabel(
            "抽帧数/距离/阈值只影响「疑似重复分集」的识别；正常 CD1/CD2 不会额外抽帧，"
            "因此合并本身仍是 -c copy 无损封装，不重编码、不耗 CPU。"
        )
        merge_hint.setProperty("cssClass", "subtitle")
        merge_hint.setWordWrap(True)
        mg.addWidget(merge_hint)
        layout.addWidget(merge_group)

        # 服务器同步（Jellyfin / Emby 通用：清洗脏前缀标签 + 全库刷新）
        jf_group = QGroupBox("服务器同步 · Jellyfin / Emby（清洗脏前缀标签 + 全库刷新）")
        jf_lay = QVBoxLayout(jf_group)
        j1 = QHBoxLayout()
        j1.addWidget(QLabel("服务器地址:"))
        self.input_jf_server = QLineEdit(self.cfg.jellyfin_server)
        self.input_jf_server.setPlaceholderText("http://localhost:8096")
        j1.addWidget(self.input_jf_server, 1)
        j1.addWidget(QLabel("API Key:"))
        self.input_jf_key = QLineEdit(self.cfg.jellyfin_api_key)
        self.input_jf_key.setEchoMode(QLineEdit.Password)
        self.input_jf_key.setPlaceholderText(
            "Jellyfin→控制台→高级→API 密钥；Emby→设置→高级→API 密钥")
        j1.addWidget(self.input_jf_key, 1)
        jf_lay.addLayout(j1)
        j2 = QHBoxLayout()
        self.btn_jf_test = QPushButton("🔌 测试连接")
        self.btn_jf_test.clicked.connect(self._test_jellyfin)
        j2.addWidget(self.btn_jf_test)
        self.btn_jf_clean = QPushButton("🧹 清洗脏标签 + 全库刷新")
        self.btn_jf_clean.setProperty("cssClass", "accent")
        self.btn_jf_clean.clicked.connect(self._clean_jellyfin)
        j2.addWidget(self.btn_jf_clean)
        self.btn_jf_restore = QPushButton("↩ 恢复上次清洗备份")
        self.btn_jf_restore.clicked.connect(self._restore_jellyfin)
        j2.addWidget(self.btn_jf_restore)
        j2.addStretch()
        jf_lay.addLayout(j2)
        jf_hint = QLabel(
            "「发行:/片商:/系列:」等是刮削源写进 genres/tags 的脏 token，会以可筛选项出现在服务器侧栏。\n"
            "清洗前会自动把受影响条目的原始标签备份到 data/（jellyfin_clean_backup_*.json），"
            "后悔可点「恢复上次清洗备份」一键回滚。请先填地址/Key 并「保存设置」。\n"
            "Jellyfin 与 Emby 用同一套接口与同一个认证头，填好即可；"
            "「数据来源」选「服务器 API」时，上面的库数据也从这里取。")
        jf_hint.setWordWrap(True)
        jf_lay.addWidget(jf_hint)
        layout.addWidget(jf_group)

        # 保存按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("💾 保存设置")
        save_btn.setProperty("cssClass", "accent")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self._reset)
        btn_row.addWidget(reset_btn)
        layout.addLayout(btn_row)

        layout.addStretch()

        scroll.setWidget(content)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(scroll)

        self._refresh_source_hint()

    # ── 媒体库数据源 ──
    def _refresh_source_hint(self) -> None:
        """显示**实际会生效**的数据来源，而不是配置里写了什么。

        用户最常踩的坑是"填了路径但文件不在那台机器上"——所以这里直接显示
        解析结果（含自动检测出来的路径），而不是复述输入框内容。
        """
        text = library_source.describe(self._pending_config())
        self.lbl_source_now.setText("当前生效：" + text)

    def _pending_config(self):
        """把界面上还没保存的值套到配置**副本**上，用于即时预览来源解析结果。

        用副本而不是直接改 self.cfg：预览不该有副作用，否则「点开设置页没保存」
        也可能悄悄改掉内存里的配置。
        """
        import copy

        cfg = copy.copy(self.cfg)
        try:
            cfg.library_source = self.combo_source.currentData() or "auto"
            cfg.server_type = self.combo_server_kind.currentData() or "auto"
            cfg.jellyfin_db_path = self.input_db.text().strip()
        except AttributeError:      # 界面尚未建完（构造早期）时直接用原配置
            pass
        return cfg

    def _detect_library(self) -> None:
        """扫描本机常见安装位置，帮用户把库文件路径填好。"""
        found = media_server.find_local_databases()
        if not found:
            QMessageBox.information(
                self, "未检测到库文件",
                "在本机的常见安装位置没有找到 jellyfin.db / library.db。\n\n"
                "两种解决办法：\n"
                "· 手动「浏览」到你的库文件所在目录；\n"
                "· 或者把「数据来源」改成「服务器 API」，在下方「服务器同步」里填"
                "服务器地址与 API Key——Emby 建议用这种方式。")
            return
        best = found[0]
        self.input_db.setText(str(best["path"]))
        self._refresh_source_hint()
        kind_cn = {"jellyfin": "Jellyfin", "emby": "Emby"}.get(best["kind"], "未知")
        lines = [f"✅ 已选中：{best['path']}", f"· 判定为：{kind_cn}"]
        if len(found) > 1:
            lines.append("\n本机还找到其它库文件：")
            lines += [f"· {r['path']}" for r in found[1:6]]
        lines.append("\n点「保存设置」后生效。")
        QMessageBox.information(self, "自动检测完成", "\n".join(lines))

    def _browse(self, target: QLineEdit, title: str, pattern: str = "") -> None:
        if pattern:
            path, _ = QFileDialog.getOpenFileName(self, title, "", pattern)
        else:
            path = QFileDialog.getExistingDirectory(self, title)
        if path:
            target.setText(path)

    def _browse_dir(self, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择目录", target.text().strip())
        if path:
            target.setText(path)

    def _browse_ffmpeg(self) -> None:
        pattern = ("可执行文件 (*.exe);;所有文件 (*)" if sys.platform == "win32"
                   else "所有文件 (*)")
        path, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg 可执行文件", "", pattern)
        if path:
            self.input_ffmpeg_path.setText(path)
            self._apply_tool_config()
            self._refresh_tool_status()

    def _apply_tool_config(self) -> None:
        """把界面上的 ffmpeg 路径注入探测层，让缓存失效并立即生效。"""
        from utils import tools
        tools.configure_tools(self.input_ffmpeg_path.text().strip(), force=True)

    # ── 工具状态 / 环境自检 ──
    def _refresh_tool_status(self) -> None:
        """显示**实际生效**的路径，而不是配置里写了什么。"""
        from utils import tools
        ffmpeg = tools.resolve_tool("ffmpeg", force=True)
        ffprobe = tools.resolve_tool("ffprobe", force=True)
        if ffmpeg:
            text = f"实际使用: {ffmpeg}"
            if ffprobe:
                text += f"  |  ffprobe: {ffprobe}"
            else:
                text += "  |  ⚠️ 未找到 ffprobe（扫描分集参数/损坏检测会不可用）"
        else:
            text = ("⚠️ 未找到 ffmpeg：视频修复 / 转码 / 分集合并的扫描将不可用。\n"
                    "请点「浏览」指定 ffmpeg 可执行文件，或把 ffmpeg 放进程序目录/bin。")
        self.lbl_tool_status.setText(text)

    def _fill_codec_combo(self, current: str) -> None:
        self.input_codec.clear()
        self.input_codec.addItem("自动（推荐：实测本机可用后再用）", "")
        for name in ("h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv",
                     "h264_amf", "hevc_amf", "h264_videotoolbox", "h264_vaapi",
                     "libx264", "libx265"):
            self.input_codec.addItem(name, name)
        self._set_combo_value(self.input_codec, current or "")

    def _fill_hw_combo(self, current: str) -> None:
        self.input_hw.clear()
        self.input_hw.addItem("自动（推荐）", "")
        for name in ("cuda", "d3d11va", "dxva2", "qsv", "amf", "vaapi",
                     "videotoolbox", "none"):
            self.input_hw.addItem(name, name)
        self._set_combo_value(self.input_hw, current or "")

    @staticmethod
    def _set_combo_value(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(value)

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        data = combo.currentData()
        if data is not None and combo.currentText() == combo.itemText(combo.currentIndex()):
            return str(data)
        return combo.currentText().strip()

    def _autodetect_ffmpeg(self) -> None:
        from utils import tools
        found = tools.find_binary("ffmpeg")
        if not found:
            QMessageBox.warning(
                self, "未找到 ffmpeg",
                "程序目录、PATH 和常见安装位置里都没有找到 ffmpeg。\n"
                "请点「浏览」手动指定，或先安装 FFmpeg。")
            return
        self.input_ffmpeg_path.setText(found)
        self._apply_tool_config()
        self._refresh_tool_status()
        QMessageBox.information(self, "已找到", f"ffmpeg: {found}")

    def _probe_hardware(self) -> None:
        from utils import tools
        self._apply_tool_config()
        ffmpeg = tools.resolve_tool("ffmpeg", force=True)
        if not ffmpeg:
            QMessageBox.warning(self, "未找到 ffmpeg", "无法探测硬件能力，请先指定 ffmpeg 路径。")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            tools.clear_probe_cache()
            info = tools.probe_hardware(ffmpeg)
        finally:
            QApplication.restoreOverrideCursor()
        encoders = info.get("encoders") or []
        hwaccels = info.get("hwaccels") or []
        self._fill_codec_combo(info.get("recommended_encoder", ""))
        self._fill_hw_combo(info.get("recommended_hwaccel", ""))
        QMessageBox.information(
            self, "本机实测结果",
            "以下结果由**真实编码/解码一次**得出，不是 ffmpeg 的编译期列表。\n\n"
            f"可用编码器：{', '.join(encoders) if encoders else '仅 libx264'}\n"
            f"可用硬解：{', '.join(a for a in hwaccels if a != 'none') or '无'}\n\n"
            f"已为你选中：{info.get('recommended_encoder', '')} / "
            f"{info.get('recommended_hwaccel', 'none')}\n（点「保存设置」生效）")

    def _run_doctor(self) -> None:
        from config import doctor
        info = doctor()
        lines = [
            f"数据目录: {info['data_dir']}",
            f"配置文件: {info['config_file']}",
            f"运行模式: {'打包(exe)' if info['frozen'] else '源码'}"
            f"{' · 便携模式' if info['portable'] else ''}",
            f"Python: {info['python']}",
            f"ffmpeg: {info['ffmpeg'] or '未找到'}",
            f"ffprobe: {info['ffprobe'] or '未找到'}",
        ]
        if info.get("encoders") is not None:
            lines.append(f"实测可用编码器: {', '.join(info['encoders']) or '仅 libx264'}")
            lines.append("实测可用硬解: "
                         + (", ".join(a for a in info["hwaccels"] if a != "none") or "无"))
        QMessageBox.information(self, "环境自检", "\n".join(lines))

    def _refresh_backup_hint(self) -> None:
        """提示备份文件**实际**会落到哪里（不再写死"H 盘"）。"""
        from utils.merge_archive import resolve_backup_root
        from types import SimpleNamespace
        probe = SimpleNamespace(
            merge_backup_root=self.input_merge_backup.text().strip(),
            merge_original_subfolder=self.input_merge_subfolder.text().strip()
            or "_merged_originals",
        )
        try:
            actual = resolve_backup_root(probe) / probe.merge_original_subfolder
        except Exception as e:  # noqa: BLE001 — 提示失敗不应影响页面
            self.lbl_backup_hint.setText(f"备份路径解析失败: {e}")
            return
        typed = self.input_merge_backup.text().strip()
        if not typed:
            self.lbl_backup_hint.setText(
                f"实际备份位置：{actual}（未指定备份盘时自动落在数据目录下，任何机器都能用）")
        else:
            self.lbl_backup_hint.setText(f"实际备份位置：{actual}")

    def _preview_theme(self) -> None:
        """选择主题时立即切换预览（不落盘，保存时才记住）。"""
        name = self.combo_theme.currentData()
        if name:
            self.theme_changed.emit(name)

    # ── AI 模型检测 ──
    def _detect_models(self) -> None:
        """读取 Ollama /api/tags，列出本机已拉取的模型并选中一个可用的。"""
        import json
        import urllib.request
        from config import DEFAULT_CONFIG

        fallback = str(DEFAULT_CONFIG.get("ai_model") or "")
        url = self.input_ai_url.text().strip().rstrip("/")
        base = url[:-3] if url.endswith("/v1") else url
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=8) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
            names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
            if not names:
                QMessageBox.information(
                    self, "检测结果",
                    "服务在线，但本机还没有拉取任何模型。\n"
                    f"请先执行: ollama pull {fallback or '<模型名>'}")
                return
            # 优先沿用默认模型（若本机已拉取），否则用列表里第一个可用的
            pick = fallback if fallback in names else names[0]
            self.input_ai_model.setText(pick)
            QMessageBox.information(
                self, "检测到模型",
                f"本机已拉取模型({len(names)})：{', '.join(names)}\n\n"
                f"已为您选中: {pick}\n（点「保存设置」生效）")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "检测失败",
                                f"无法访问 {base}/api/tags：{str(e)[:80]}")

    # ── Jellyfin 同步 ──
    def _test_jellyfin(self) -> None:
        from utils import jellyfin_api
        ok, msg = jellyfin_api.ping(
            self.input_jf_server.text().strip(), self.input_jf_key.text().strip())
        QMessageBox.information(
            self, "Jellyfin 测试",
            (f"✅ 连接成功：{msg}" if ok else f"❌ 连接失败：{msg}"))

    def _clean_jellyfin(self) -> None:
        server = self.input_jf_server.text().strip()
        key = self.input_jf_key.text().strip()
        if not server or not key:
            QMessageBox.warning(self, "未配置", "请先填写服务器地址与 API Key，并保存设置。")
            return
        if getattr(self, "_jf_worker", None) and self._jf_worker.isRunning():
            return
        self.btn_jf_clean.setEnabled(False)
        self._jf_msgs = []
        from workers.jellyfin_sync import JellyfinCleanWorker
        self._jf_worker = JellyfinCleanWorker(server, key)
        self._jf_worker.log.connect(self._jf_msgs.append)
        self._jf_worker.finished.connect(self._on_jf_done)
        self._jf_worker.start()
        QMessageBox.information(self, "开始清洗", "正在后台清洗脏标签并触发全库刷新，完成后会提示。")

    def _on_jf_done(self, data: dict) -> None:
        self.btn_jf_clean.setEnabled(True)
        if data.get("error"):
            QMessageBox.warning(self, "同步失败", data["error"])
            return
        if data.get("empty"):
            QMessageBox.information(self, "同步完成", "未发现脏前缀标签，无需清洗。")
            return
        msgs = "\n".join(getattr(self, "_jf_msgs", []))
        backup = data.get("backup") or ""
        QMessageBox.information(
            self, "同步完成",
            f"扫描 {data['scanned']} 条，清洗 {data['cleaned']} 条。\n"
            f"原始标签已备份：\n{backup}\n\n可用「恢复上次清洗备份」回滚。\n\n{msgs}")

    def _restore_jellyfin(self) -> None:
        server = self.input_jf_server.text().strip()
        key = self.input_jf_key.text().strip()
        if not server or not key:
            QMessageBox.warning(self, "未配置", "请先填写服务器地址与 API Key。")
            return
        from workers.jellyfin_sync import JellyfinRestoreWorker, latest_backup_path
        backup = latest_backup_path()
        if not backup:
            QMessageBox.warning(self, "无备份", "data/ 下没有找到清洗备份（jellyfin_clean_backup_*.json）。")
            return
        if getattr(self, "_jf_restore", None) and self._jf_restore.isRunning():
            return
        ans = QMessageBox.question(
            self, "确认恢复",
            f"将从备份恢复以下文件里的原始标签：\n{backup}\n\n确定恢复吗？")
        if ans != QMessageBox.Yes:
            return
        self.btn_jf_restore.setEnabled(False)
        self._jf_msgs = []
        self._jf_restore = JellyfinRestoreWorker(server, key, backup)
        self._jf_restore.log.connect(self._jf_msgs.append)
        self._jf_restore.finished.connect(self._on_jf_restore_done)
        self._jf_restore.start()

    def _on_jf_restore_done(self, data: dict) -> None:
        self.btn_jf_restore.setEnabled(True)
        if data.get("error"):
            QMessageBox.warning(self, "恢复失败", data["error"])
            return
        msgs = "\n".join(getattr(self, "_jf_msgs", []))
        QMessageBox.information(
            self, "恢复完成", f"已恢复 {data['restored']} 条原始标签。\n\n{msgs}")

    def _save(self) -> None:
        self.cfg.library_source = self.combo_source.currentData() or "auto"
        self.cfg.server_type = self.combo_server_kind.currentData() or "auto"
        self.cfg.jellyfin_db_path = self.input_db.text()
        self.cfg.jellyfin_data_dir = self.input_data.text()
        self.cfg.exclude_path_keywords = [
            k.strip() for k in self.input_exclude.text().split(",") if k.strip()
        ]
        self.cfg.score_th_keep = self.spin_th_keep.value()
        self.cfg.score_th_delete = self.spin_th_delete.value()
        self.cfg.uncensored_keywords = [
            kw.strip() for kw in self.keywords_edit.toPlainText().split("\n") if kw.strip()
        ]
        self.cfg.subtitle_extensions = [
            e.strip() for e in self.input_subs.text().split(",") if e.strip()
        ]
        self.cfg.video_extensions = [
            e.strip() for e in self.input_vids.text().split(",") if e.strip()
        ]
        self.cfg.min_video_size_mb = self.spin_min.value()
        # input_codec / input_hw 是 QComboBox（可编辑），必须取 currentData()
        # 而不是 .text() —— data 为 "" 表示"自动"，文本只是显示。
        self.cfg.ffmpeg_gpu_codec = self._combo_value(self.input_codec)
        self.cfg.ffmpeg_hardware_accel = self._combo_value(self.input_hw)
        self.cfg.transcode_crf = self.spin_crf.value()
        self.cfg.restored_suffix_marker = self.input_marker.text()
        # 保存分集合并设置
        self.cfg.merge_output_container = self.combo_merge_container.currentData() or "mp4"
        # 留空表示"自动落到数据目录下的 backups/"，不能塞一个本机可能不存在的盘符
        self.cfg.merge_backup_root = self.input_merge_backup.text().strip()
        self.cfg.merge_original_subfolder = self.input_merge_subfolder.text().strip() or "_merged_originals"
        self.cfg.merge_rename_to_number = self.check_merge_rename.isChecked()
        self.cfg.merge_max_parts = self.spin_merge_max_parts.value()
        self.cfg.merge_min_part_seconds = self.spin_merge_min_part.value()
        self.cfg.merge_timeout_per_file = self.spin_merge_timeout.value()
        self.cfg.merge_dup_frame_count = self.spin_merge_dup_frames.value()
        self.cfg.merge_dup_hash_distance = self.spin_merge_dup_distance.value()
        self.cfg.merge_dup_sim_threshold = self.spin_merge_dup_sim.value()
        # 保存 阈值 + 增强匹配 + 引擎
        self.cfg.enhance_match = self.check_enhance.isChecked()
        self.cfg.engine = self.combo_engine.currentData() or "v2"
        # 保存 AI 配置
        self.cfg.ai_enabled = self.ai_enabled.isChecked()
        self.cfg.ai_base_url = self.input_ai_url.text().strip()
        self.cfg.ai_model = self.input_ai_model.text().strip()
        self.cfg.ai_key = self.input_ai_key.text()
        # 保存 Jellyfin 同步配置
        self.cfg.jellyfin_server = self.input_jf_server.text().strip()
        self.cfg.jellyfin_api_key = self.input_jf_key.text().strip()
        # 保存主题
        selected_theme = self.combo_theme.currentData()
        if selected_theme:
            self.cfg.theme = selected_theme

        self.cfg.save()
        # 保存后立刻让新的 ffmpeg 路径生效（不必重启）
        self._apply_tool_config()
        self._refresh_tool_status()
        self._refresh_backup_hint()
        QMessageBox.information(self, "成功", "设置已保存！")

    def _reset(self) -> None:
        from config import DEFAULT_CONFIG
        theme_idx = self.combo_theme.findData(DEFAULT_CONFIG["theme"])
        if theme_idx >= 0:
            self.combo_theme.setCurrentIndex(theme_idx)
        si = self.combo_source.findData(DEFAULT_CONFIG["library_source"])
        if si >= 0:
            self.combo_source.setCurrentIndex(si)
        ki = self.combo_server_kind.findData(DEFAULT_CONFIG["server_type"])
        if ki >= 0:
            self.combo_server_kind.setCurrentIndex(ki)
        self.input_db.setText(DEFAULT_CONFIG["jellyfin_db_path"])
        self.input_data.setText(DEFAULT_CONFIG["jellyfin_data_dir"])
        self.input_exclude.setText(", ".join(DEFAULT_CONFIG["exclude_path_keywords"]))
        self.spin_th_keep.setValue(DEFAULT_CONFIG["score_th_keep"])
        self.spin_th_delete.setValue(DEFAULT_CONFIG["score_th_delete"])
        self.check_enhance.setChecked(DEFAULT_CONFIG["enhance_match"])
        ei = self.combo_engine.findData(DEFAULT_CONFIG["engine"])
        if ei >= 0:
            self.combo_engine.setCurrentIndex(ei)
        self.ai_enabled.setChecked(DEFAULT_CONFIG["ai_enabled"])
        self.input_ai_url.setText(DEFAULT_CONFIG["ai_base_url"])
        self.input_ai_model.setText(DEFAULT_CONFIG["ai_model"])
        self.input_ai_key.setText(DEFAULT_CONFIG["ai_key"])
        self.keywords_edit.setPlainText("\n".join(DEFAULT_CONFIG["uncensored_keywords"]))
        self.input_subs.setText(", ".join(DEFAULT_CONFIG["subtitle_extensions"]))
        self.input_vids.setText(", ".join(DEFAULT_CONFIG["video_extensions"]))
        self.spin_min.setValue(DEFAULT_CONFIG["min_video_size_mb"])
        self.input_codec.setText(DEFAULT_CONFIG["ffmpeg_gpu_codec"])
        self.input_hw.setText(DEFAULT_CONFIG["ffmpeg_hardware_accel"])
        self.spin_crf.setValue(DEFAULT_CONFIG["transcode_crf"])
        self.input_marker.setText(DEFAULT_CONFIG["restored_suffix_marker"])
        self.combo_merge_container.setCurrentIndex(
            self.combo_merge_container.findData(DEFAULT_CONFIG["merge_output_container"]))
        self.input_merge_backup.setText(DEFAULT_CONFIG["merge_backup_root"])
        self.input_merge_subfolder.setText(DEFAULT_CONFIG["merge_original_subfolder"])
        self.check_merge_rename.setChecked(DEFAULT_CONFIG["merge_rename_to_number"])
        self.spin_merge_max_parts.setValue(DEFAULT_CONFIG["merge_max_parts"])
        self.spin_merge_min_part.setValue(DEFAULT_CONFIG["merge_min_part_seconds"])
        self.spin_merge_timeout.setValue(DEFAULT_CONFIG["merge_timeout_per_file"])
        self.spin_merge_dup_frames.setValue(DEFAULT_CONFIG["merge_dup_frame_count"])
        self.spin_merge_dup_distance.setValue(DEFAULT_CONFIG["merge_dup_hash_distance"])
        self.spin_merge_dup_sim.setValue(DEFAULT_CONFIG["merge_dup_sim_threshold"])
        self.input_jf_server.setText(DEFAULT_CONFIG["jellyfin_server"])
        self.input_jf_key.setText(DEFAULT_CONFIG["jellyfin_api_key"])
        QMessageBox.information(self, "提示", '已恢复默认设置，点击「保存」生效。')
