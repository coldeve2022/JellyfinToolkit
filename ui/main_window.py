"""主窗口 — 侧边栏导航 + 堆叠页面路由 + 主题管理。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QScrollArea,
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QStackedWidget, QPushButton, QLabel, QFrame,
    QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QFileInfo

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
                font-weight: {'bold' if active else 'normal'};
                margin: 2px 10px;
            }}
            QPushButton:hover {{
                background-color: {accent if active else hover};
                color: {t['selection_text'] if active else t['text_primary']};
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
        self.apply_stylesheet()

        self._nav_buttons[0].set_active(True)
        self.stack.setCurrentIndex(0)

        if not self.ffmpeg_path:
            QMessageBox.warning(
                self, "未找到 FFmpeg",
                "未检测到 ffmpeg，视频修复 / 转码 / 分集合并扫描将不可用。\n\n"
                "解决方式（任选其一）：\n"
                "1. 打开「设置 → FFmpeg 转码设置」，点「浏览」指定 ffmpeg 可执行文件；\n"
                "2. 把 ffmpeg/ffprobe 放进程序目录下的 bin\\ 子目录；\n"
                "3. 安装 FFmpeg 并确保它在系统 PATH 中。"
            )
        elif not QFileInfo(self.ffmpeg_path).isExecutable():
            QMessageBox.warning(
                self, "FFmpeg 不可用",
                f"解析到的 ffmpeg 路径不可执行：\n{self.ffmpeg_path}\n"
                "请在「设置 → FFmpeg 转码设置」里重新指定。")

    # ── 主题管理 ──
    def apply_theme(self, theme_name: str | None = None) -> None:
        """切换主题并立即生效。

        Args:
            theme_name: 主题名（dark/light）；None 使用当前主题
        """
        if theme_name:
            theme.set_current(theme_name)
            self.cfg.theme = theme_name
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

        logo = QLabel(f"🎬 {APP_NAME_CN}\n  Toolkit")
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

        self._nav_buttons: list[SidebarButton] = []
        for icon, label in self.NAV_ITEMS:
            btn = SidebarButton(icon, label)
            btn.clicked.connect(lambda checked, idx=len(self._nav_buttons): self._navigate(idx))
            self._nav_buttons.append(btn)
            sidebar_layout.addWidget(btn)

        sidebar_layout.addStretch()

        ver = QLabel(f"v{__version__}")
        ver.setObjectName("version-label")
        ver.setAlignment(Qt.AlignCenter)
        sidebar_layout.addWidget(ver)
        sidebar_layout.addSpacing(8)

        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        #: 真实的功能页面（stack 里放的是套了滚动区的容器，见 register_page）
        self._pages: list = []

    def _navigate(self, idx: int) -> None:
        """切换页面，更新按钮选中状态。"""
        for i, btn in enumerate(self._nav_buttons):
            btn.set_active(i == idx)
        self.stack.setCurrentIndex(idx)

    def register_page(self, page: QWidget) -> None:
        """向堆叠组件注册功能页面（自动套一层滚动区）。

        为什么要滚动区：默认窗口只有 1100x720，左边栏固定占掉约 200px，
        内容区实际不到 900px。密集的参数行（一行 4~6 个控件）在这个宽度下
        放不下，Qt 会把控件**压到比最小尺寸还小** —— 结果是标签被截断、
        控件互相叠压。套上滚动区之后，内容按自己的最小宽度排版，
        窗口不够宽就出现滚动条，控件不再被挤压。

        注意 ``stack`` 里放的是滚动区而不是页面本身，所以另外用 ``_pages``
        记住真实页面 —— ``closeEvent`` 要按页面调 ``shutdown()``。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self._pages.append(page)
        self.stack.addWidget(scroll)

    def closeEvent(self, event):
        """保存配置并停止所有后台线程，避免 "QThread destroyed while running" 崩溃。"""
        # 通知各页面优雅停止后台线程
        for page in self._pages:
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        # 短暂泵事件循环，让停止信号送达
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()
        self.cfg.save()
        super().closeEvent(event)


NAV_LABELS = [label for _icon, label in MainWindow.NAV_ITEMS]
