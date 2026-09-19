"""新手引导页 - 6 步向导，每步可跳转功能页；点「开始使用」保存标记并进入库概览。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QFrame,
)

from config import ToolkitConfig


class OnboardingPage(QWidget):
    """新手引导。"""

    navigate_requested = Signal(str)   # 要跳转的功能页标题（与 MainWindow.NAV_ITEMS 对应）

    def __init__(self, cfg: ToolkitConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._steps = self._build_steps()
        self._idx = 0
        self._setup_ui()
        self._show_step(0)

    def _build_steps(self):
        return [
            {
                "title": "欢迎使用 Jellyfin Toolkit 智能清理（Jellyfin / Emby 通用）",
                "body": (
                    "这套工具帮你把 5000+ 部作品按「是否像你喜欢」自动分层：\n\n"
                    "• 已保留（看过/收藏）= 真值，不动\n"
                    "• 可能喜欢 / 建议删除 = 系统自动判断\n"
                    "• 待复核 = 才需要你快速打标\n\n"
                    "全程只读媒体库，删除只进回收站（可恢复），绝不物理删。"
                ),
                "goto": None,
            },
            {
                "title": "① 先看库概览",
                "body": (
                    "点「库概览」→「扫描库」，看清全库分布：\n"
                    "• 作品总数、年份/厂商/演员分布\n"
                    "• 收藏/看过/未看（待预测池）规模\n"
                    "• 重复组数量；可在设置里按路径关键词排除不想纳入的目录。"
                ),
                "goto": "库概览",
            },
            {
                "title": "② 生成我的喜好画像",
                "body": (
                    "点「喜好画像」→「生成我的喜好画像」：\n"
                    "• 用条形图直观显示你的 Top 演员 / 厂商 / 类型\n"
                    "• 无码偏好、年份偏好、不喜欢特征\n"
                    "• 画像驱动的「未看推荐 Top20」\n\n"
                    "画像随你打标自动更新，越用越准。"
                ),
                "goto": "喜好画像",
            },
            {
                "title": "③ 快速打标（核心闭环）",
                "body": (
                    "点「快速打标」→「开始分析」，只处理待复核卡片：\n"
                    "→ 右箭头 = 喜欢/保留　← 左箭头 = 不喜欢\n"
                    "↓ 下箭头 = 跳过　空格 = 打开文件位置　Ctrl+Z = 撤销\n\n"
                    "每判一条全库自动重排——你标「不喜欢」的同类会沉到建议删除。"
                ),
                "goto": "快速打标",
            },
            {
                "title": "④ 安全删除 + 重复处理",
                "body": (
                    "建议删除清单分三类：垃圾/附属文件、零命中、重复差版本。\n"
                    "建议顺序：\n"
                    "1) 先只勾「垃圾/附属」移入回收站，验证可还原\n"
                    "2) 再清「零命中」作品\n"
                    "3) 最后在「重复处理」删差版本\n\n"
                    "⚠️ 首次务必先删 1 个测试文件，确认回收站能还原再批量。"
                ),
                "goto": "删除暂存",
            },
            {
                "title": "完成 🎉",
                "body": (
                    "核心流程：\n"
                    "库概览 → 喜好画像 → 快速打标 → 删除暂存 → 重复处理\n\n"
                    "以后每下载新片，重跑「快速打标」分析即可得到 留/删/复核 建议。\n"
                    "点「开始使用」进入库概览，之后可随时点侧边栏「新手引导」重看。"
                ),
                "goto": None,
            },
        ]

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(40, 36, 40, 32)
        root.setSpacing(18)

        self.stack = QStackedWidget()
        for s in self._steps:
            self.stack.addWidget(self._make_step_widget(s))
        root.addWidget(self.stack, 1)

        dots = QHBoxLayout()
        dots.setAlignment(Qt.AlignCenter)
        self._dots: list[QLabel] = []
        for _ in range(len(self._steps)):
            d = QLabel("●")
            d.setProperty("cssClass", "step-dot")
            self._dots.append(d)
            dots.addWidget(d)
        root.addLayout(dots)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_prev = QPushButton("← 上一步")
        self.btn_prev.clicked.connect(self._prev)
        btn_row.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一步 →")
        self.btn_next.setProperty("cssClass", "accent")
        self.btn_next.clicked.connect(self._next)
        btn_row.addWidget(self.btn_next)
        btn_row.addStretch()
        root.addLayout(btn_row)

    def _make_step_widget(self, s: dict) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(16)

        card = QFrame()
        card.setProperty("cssClass", "panel-card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 26, 28, 26)
        title = QLabel(s["title"])
        title.setProperty("cssClass", "panel-title")
        cl.addWidget(title)
        body = QLabel(s["body"])
        body.setProperty("cssClass", "text-muted")
        body.setStyleSheet("font-size:14px; line-height:1.7;")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        cl.addWidget(body)
        if s.get("goto"):
            g = QPushButton(s["goto"] + " →")
            g.setProperty("cssClass", "accent")
            g.clicked.connect(lambda _=False, label=s["goto"]: self.navigate_requested.emit(label))
            cl.addWidget(g)
        lay.addWidget(card, 1)
        return w

    def _show_step(self, i: int) -> None:
        self._idx = max(0, min(i, len(self._steps) - 1))
        self.stack.setCurrentIndex(self._idx)
        last = self._idx == len(self._steps) - 1
        if last:
            self.btn_next.setText("开始使用 →")
        else:
            self.btn_next.setText("下一步 →")
        self.btn_next.setEnabled(not self.cfg.onboarding_done or not last)
        self.btn_prev.setEnabled(self._idx > 0)
        for j, d in enumerate(self._dots):
            d.setProperty("cssClass", "step-dot-active" if j <= self._idx else "step-dot")

    def _next(self) -> None:
        if self._idx >= len(self._steps) - 1:
            # 完成：保存标记并进入库概览
            if not self.cfg.onboarding_done:
                self.cfg.onboarding_done = True
                self.cfg.save()
            self.btn_next.setEnabled(False)
            self.btn_next.setText("✓ 已开始使用")
            self.btn_prev.setEnabled(True)
            self.navigate_requested.emit("库概览")
            return
        self._show_step(self._idx + 1)

    def _prev(self) -> None:
        self._show_step(self._idx - 1)

    def apply_theme(self) -> None:
        return  # 样式全部走 cssClass，随主窗口 QSS 自动切换
