"""主窗口 — 侧边栏导航 + 堆叠页面路由 + 主题管理。"""

from __future__ import annotations

from dataclasses import asdict

from PySide6.QtWidgets import (
    QScrollArea,
    QMainWindow,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QStackedWidget,
    QPushButton,
    QLabel,
    QFrame,
    QMessageBox,
    QSizePolicy,
)
from PySide6.QtCore import Qt, QFileInfo, QTimer
from ui.services import (
    events,
    TaskManager,
    page_workers,
    stop_page,
    refresh_configuration,
    state_context,
    busy,
)
from ui.layout import compact_forms, dock_actions, collapse_advanced

from config import ToolkitConfig
from ui import theme
from ui.styles import build_stylesheet, SIDEBAR_WIDTH
from utils.fonts import ui_font
from version import APP_NAME_CN, __version__, window_title


class SidebarButton(QPushButton):
    """侧边栏导航按钮 — 带图标和文本，支持选中态（颜色跟随主题）。"""

    def __init__(self, icon_text: str, label: str, parent=None):
        super().__init__(parent)
        self.setText(f"  {icon_text}  {label}")
        self.setCheckable(True)
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._refresh_style(False)

    def _refresh_style(self, active: bool) -> None:
        """根据当前主题与选中态刷新样式。"""
        t = theme.theme()
        accent = t["accent"]
        hover = t["bg_hover"]
        if active:
            bg, color = accent, t["selection_text"]
        else:
            bg, color = "transparent", t["text_secondary"]
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                border: none;
                border-radius: 8px;
                padding-left: 16px;
                text-align: left;
                color: {color};
                font-size: 13px;
                font-weight: {"bold" if active else "normal"};
                margin: 2px 10px;
            }}
            QPushButton:hover {{
                background-color: {accent if active else hover};
                color: {t["selection_text"] if active else t["text_primary"]};
            }}
        """)

    def set_active(self, active: bool) -> None:
        self.setChecked(active)
        self._refresh_style(active)


class MainWindow(QMainWindow):
    """Jellyfin Toolkit 主窗口。"""

    NAV_ITEMS = [
        ("🚀", "新手引导"),
        ("📊", "库概览"),
        ("❤️", "喜好画像"),
        ("🏷️", "快速打标"),
        ("🗑️", "删除暂存"),
        ("🔁", "重复处理"),
        ("🎬", "视频有码检测"),
        ("📝", "字幕缺失检测"),
        ("✍️", "批量生成字幕"),
        ("🔄", "破解视频替换"),
        ("🔓", "马赛克破解"),
        ("🔧", "视频修复转码"),
        ("📋", "NFO 关联修复"),
        ("🗄️", "数据库工具"),
        ("🧩", "分集合并"),
        ("🤖", "自动化工作台"),
        ("⚙️", "设置"),
        ("📌", "任务中心"),
    ]

    def __init__(self, cfg: ToolkitConfig | None = None):
        super().__init__()
        self.cfg = cfg if cfg is not None else ToolkitConfig.load()

        # 应用配置中的主题
        theme.set_current(self.cfg.theme)

        self.setWindowTitle(window_title())
        self.resize(1100, 720)
        self.setMinimumSize(900, 600)

        # ffmpeg 路径每次启动时按「用户配置 > 程序目录 > PATH > 常见位置」解析
        from utils.tools import resolve_tool

        self.ffmpeg_path = resolve_tool("ffmpeg")

        self._setup_ui()
        self._last_config_values = asdict(self.cfg)
        self._last_config_context = state_context(self.cfg)
        self.apply_stylesheet()

        self._nav_buttons[0].set_active(True)
        self.stack.setCurrentIndex(0)
        events.config_changed.connect(self._config_changed)
        TaskManager.instance().changed.connect(self._task_summary)

        if not self.ffmpeg_path:
            QMessageBox.warning(
                self,
                "未找到 FFmpeg",
                "未检测到 ffmpeg，视频修复 / 转码 / 分集合并扫描将不可用。\n\n"
                "解决方式（任选其一）：\n"
                "1. 打开「设置 → FFmpeg 转码设置」，点「浏览」指定 ffmpeg 可执行文件；\n"
                "2. 把 ffmpeg/ffprobe 放进程序目录下的 bin\\ 子目录；\n"
                "3. 安装 FFmpeg 并确保它在系统 PATH 中。",
            )
        elif not QFileInfo(self.ffmpeg_path).isExecutable():
            QMessageBox.warning(
                self,
                "FFmpeg 不可用",
                f"解析到的 ffmpeg 路径不可执行：\n{self.ffmpeg_path}\n"
                "请在「设置 → FFmpeg 转码设置」里重新指定。",
            )

    # ── 主题管理 ──
    def apply_theme(self, theme_name: str | None = None) -> None:
        """切换主题并立即生效。

        Args:
            theme_name: 主题名（dark/light）；None 使用当前主题
        """
        if theme_name:
            theme.set_current(theme_name)
        self.apply_stylesheet()
        # 通知各页面刷新自绘/内联样式的动态色（无此方法的页面自动跳过）
        for page in self._pages:
            apply_theme = getattr(page, "apply_theme", None)
            if callable(apply_theme):
                apply_theme()
        # 侧边栏按钮样式自管理，需手动刷新
        for i, btn in enumerate(self._nav_buttons):
            btn._refresh_style(i == self.stack.currentIndex())

    def apply_stylesheet(self) -> None:
        self.setStyleSheet(build_stylesheet())

    # ── UI 构建 ──
    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 16, 0, 16)
        sidebar_layout.setSpacing(4)

        logo = QLabel(f"🎬 {APP_NAME_CN}")
        logo.setObjectName("logo")
        logo_font = ui_font(16, bold=True)
        if logo_font is not None:
            logo.setFont(logo_font)
        logo.setFixedHeight(70)
        sidebar_layout.addWidget(logo)

        sep = QFrame()
        sep.setObjectName("sidebar-sep")
        sep.setFrameShape(QFrame.HLine)
        sidebar_layout.addWidget(sep)
        sidebar_layout.addSpacing(12)

        self._nav_buttons: list[SidebarButton] = [None] * len(self.NAV_ITEMS)
        groups = [
            ("首页与连接", (0, 16)),
            ("内容审核", (1, 2, 3, 4)),
            ("版本与分集", (5, 14)),
            ("字幕管理", (7, 8)),
            ("媒体处理", (6, 10, 9, 11)),
            ("工作流与任务", (15, 17)),
            ("高级维护", (12, 13)),
        ]
        nav_content = QWidget()
        nav_layout = QVBoxLayout(nav_content)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)
        for name, indices in groups:
            heading = QLabel(name)
            heading.setProperty("cssClass", "nav-group")
            nav_layout.addWidget(heading)
            for index in indices:
                icon, label = self.NAV_ITEMS[index]
                btn = SidebarButton(icon, label)
                btn.clicked.connect(lambda checked, idx=index: self._navigate(idx))
                self._nav_buttons[index] = btn
                nav_layout.addWidget(btn)

        nav_layout.addStretch()
        navigation = QScrollArea()
        navigation.setObjectName("navigation-scroll")
        navigation.setWidgetResizable(True)
        navigation.setFrameShape(QFrame.NoFrame)
        navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        navigation.setWidget(nav_content)
        sidebar_layout.addWidget(navigation, 1)

        sidebar_layout.addStretch()

        ver = QLabel(f"v{__version__}")
        ver.setObjectName("version-label")
        ver.setAlignment(Qt.AlignCenter)
        sidebar_layout.addWidget(ver)
        sidebar_layout.addSpacing(8)

        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        main_content = QWidget()
        content_layout = QVBoxLayout(main_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        from ui.scope_selector import ScopeBar

        self.scope_bar = ScopeBar(self.cfg, self)
        content_layout.addWidget(self.scope_bar)
        content_layout.addWidget(self.stack, 1)
        root.addWidget(main_content, 1)
        #: 真实的功能页面（stack 里放的是套了滚动区的容器，见 register_page）
        self._pages: list = []

    def _navigate(self, idx: int) -> None:
        """切换页面，更新按钮选中状态。"""
        for i, btn in enumerate(self._nav_buttons):
            btn.set_active(i == idx)
        self.stack.setCurrentIndex(idx)

    def _config_changed(self):
        current, context = asdict(self.cfg), state_context(self.cfg)
        previous = self._last_config_values
        if current == previous and context == self._last_config_context:
            return
        scope_only = all(value == previous.get(key) for key, value in current.items()
                         if not key.startswith("library_scope_"))
        self._last_config_values, self._last_config_context = current, context
        for page in self._pages:
            module = type(page).__module__.rsplit(".", 1)[-1]
            if scope_only and module not in {"insight", "profile", "review", "delete", "dedup", "automation"}:
                continue
            changed = getattr(page, "on_config_changed", None)
            if callable(changed):
                changed()
            elif hasattr(page, "cfg"):
                refresh_configuration(page)
        self._update_scope_pages()

    def _update_scope_pages(self):
        ready = self.scope_bar.selection_ready()
        for page in self._pages:
            module = type(page).__module__.rsplit(".", 1)[-1]
            if module in {"insight", "profile", "review", "delete", "dedup", "automation"}:
                page.setEnabled(ready)
                footer = getattr(page, "_action_footer", None)
                if footer is not None:
                    footer.setEnabled(ready)
            elif module in {"subtitle_gen", "lada"}:
                page.btn_scan.setEnabled(ready and not busy(page))

    def _task_summary(self):
        manager = TaskManager.instance()
        active = [entry["record"] for entry in manager._active.values()]
        if active:
            self.statusBar().showMessage(
                f"{len(active)} 个任务进行中 · {active[-1]['title']} · {active[-1]['progress']}% · 在任务中心查看/停止"
            )
        elif manager.records:
            last = manager.records[-1]
            self.statusBar().showMessage(
                last["title"]
                + " · "
                + {"succeeded": "完成", "failed": "失败", "cancelled": "已取消"}.get(
                    last["status"], last["status"]
                )
            )

    def register_page(self, page: QWidget) -> None:
        """注册页面；快速打标使用内部滚动工作区，其余表单套外层滚动区。

        为什么要滚动区：默认窗口只有 1100x720，左边栏固定占掉约 200px，
        内容区实际不到 900px。密集的参数行（一行 4~6 个控件）在这个宽度下
        放不下，Qt 会把控件**压到比最小尺寸还小** —— 结果是标签被截断、
        控件互相叠压。套上滚动区之后，内容按自己的最小宽度排版，
        窗口不够宽就出现滚动条，控件不再被挤压。

        快速打标的结果表/标签/卡片自行滚动，不能再套外层滚动或搬走工具栏。
        注意 ``stack`` 里放的是容器而不是页面本身，所以另外用 ``_pages``
        记住真实页面 —— ``closeEvent`` 要按页面调 ``shutdown()``。
        """
        workspace = type(page).__module__ == "ui.pages.review"
        scroll = None
        if not workspace:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setWidget(page)
        self._pages.append(page)
        if hasattr(page, "cfg"):
            page._state_context = state_context(page.cfg)
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(page if workspace else scroll, 1)
        if not workspace:
            compact_forms(page)
            collapse_advanced(page)
            dock_actions(page, layout)
        self.stack.addWidget(container)
        self._update_scope_pages()

    def closeEvent(self, event):
        """保存配置并停止所有后台线程，避免 "QThread destroyed while running" 崩溃。"""
        self._closing = True
        TaskManager.instance().stop_all()
        for page in self._pages:
            stop_page(page)
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        if TaskManager.instance().running() or any(
            w.isRunning() for page in self._pages for w in page_workers(page)
        ):
            event.ignore()
            self.statusBar().showMessage(
                "正在停止任务。后台线程结束后窗口才会关闭，可在任务中心查看。"
            )
            if not getattr(self, "_close_pending", False):
                self._close_pending = True
                QTimer.singleShot(100, self, self._retry_close)
            return
        # 短暂泵事件循环，让停止信号送达
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        self.cfg.save()
        super().closeEvent(event)

    def _retry_close(self):
        self._close_pending = False
        self.close()


NAV_LABELS = [label for _icon, label in MainWindow.NAV_ITEMS]
