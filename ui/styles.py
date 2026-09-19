"""统一样式 — 根据主题 token 动态生成 QSS。

所有颜色从 ui.theme 获取，禁止硬编码。切换主题只需重新应用
build_stylesheet(theme_name) 生成的 QSS。

字体族按平台**探测**后给出（见 utils.fonts），不能写死 "Microsoft YaHei" ——
没有该字体的机器上 Qt 会静默回退，中文可能变方块。
"""

from ui import theme
from utils.fonts import mono_family, ui_family

SIDEBAR_WIDTH = 220


def font_family() -> str:
    """界面正文字体族（每次调用重新探测，Qt 就绪前后结果可能不同）。"""
    return f"{ui_family()}, sans-serif"


def font_mono() -> str:
    """等宽字体族。"""
    return f"{mono_family()}, monospace"


def build_stylesheet(theme_name: str | None = None) -> str:
    """根据主题 token 生成完整 QSS 样式表。

    Args:
        theme_name: 主题名（dark/light），None 使用当前主题

    Returns:
        QSS 样式表字符串
    """
    t = theme.theme(theme_name)

    accent = t["accent"]
    accent_hover = t["accent_hover"]
    accent_dark = t["accent_dark"]
    success = t["success"]
    warning = t["warning"]
    danger = t["danger"]
    bg_darkest = t["bg_darkest"]
    bg_dark = t["bg_dark"]
    bg_card = t["bg_card"]
    bg_hover = t["bg_hover"]
    bg_input = t["bg_input"]
    border = t["border"]
    text_primary = t["text_primary"]
    text_secondary = t["text_secondary"]
    text_muted = t["text_muted"]
    title_color = t["title_color"]
    logo_color = t["logo_color"]
    selection_text = t["selection_text"]

    return f"""
    QWidget {{
        font-family: "{font_family()}";
        font-size: 13px;
        color: {text_primary};
        background-color: {bg_dark};
    }}

    QMainWindow {{ background-color: {bg_dark}; }}

    /* ── 侧边栏 ── */
    #sidebar {{ background-color: {bg_darkest}; }}
    #logo {{
        color: {logo_color};
        font-size: 16px;
        font-weight: bold;
        padding: 12px 16px;
    }}
    #version-label {{ color: {text_muted}; font-size: 11px; }}
    #sidebar-sep {{ color: {text_muted}; }}

    /* ── 按钮（统一高度，保证视觉 token 一致）── */
    QPushButton {{
        background-color: {bg_hover};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 8px 18px;
        min-height: 22px;
        color: {text_primary};
        font-weight: 500;
    }}
    QPushButton:hover {{ background-color: #3B4450; border-color: {accent};
                        color: {text_primary}; }}
    QPushButton:pressed {{ background-color: {bg_card}; }}
    QPushButton:disabled {{
        background-color: {bg_card};
        color: {text_muted};
        border-color: {border};
    }}

    QPushButton[cssClass="accent"] {{
        background-color: {accent};
        color: {selection_text};
        border: none;
        font-weight: bold;
    }}
    QPushButton[cssClass="accent"]:hover {{ background-color: {accent_hover};
                                            color: {selection_text}; }}
    QPushButton[cssClass="accent"]:disabled {{
        background-color: {bg_hover};
        color: {text_muted};
    }}

    QPushButton[cssClass="danger"] {{
        background-color: {danger};
        color: #FFFFFF;
        border: none;
    }}
    QPushButton[cssClass="danger"]:hover {{ background-color: {danger}; }}
    QPushButton[cssClass="danger"]:disabled {{
        background-color: {bg_hover};
        color: {text_muted};
    }}

    QPushButton[cssClass="success"] {{
        background-color: {success};
        color: #FFFFFF;
        border: none;
        font-weight: bold;
    }}
    QPushButton[cssClass="success"]:hover {{ background-color: {success}; }}
    QPushButton[cssClass="success"]:disabled {{
        background-color: {bg_hover};
        color: {text_muted};
    }}

    /* ── 标签 ── */
    QLabel {{ color: {text_primary}; background: transparent; }}

    QLabel[cssClass="page-title"] {{
        font-size: 18px;
        font-weight: bold;
        color: {title_color};
        padding-bottom: 4px;
    }}
    QLabel[cssClass="subtitle"] {{ color: {text_secondary}; font-size: 12px; }}
    QLabel[cssClass="muted-label"] {{ color: {text_secondary}; }}

    QLabel[cssClass="info-card"] {{
        background-color: {bg_card};
        border: 2px dashed {border};
        border-radius: 10px;
        padding: 16px;
        color: {text_secondary};
        font-size: 13px;
    }}

    QLabel[cssClass="path-display"] {{
        color: {text_secondary};
        padding: 8px;
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
    }}

    /* ── 输入框 ── */
    QLineEdit, QSpinBox, QDoubleSpinBox {{
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 7px 12px;
        color: {text_primary};
        selection-background-color: {accent};
    }}
    QLineEdit:focus, QSpinBox:focus {{ border-color: {accent}; }}

    /* ── 列表/表格 ── */
    QListWidget, QTableWidget {{
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
        gridline-color: {border};
        outline: none;
        color: {text_primary};
    }}
    QListWidget::item, QTableWidget::item {{
        padding: 6px 10px;
        border-bottom: 1px solid {border};
        color: {text_primary};
    }}
    QListWidget::item:selected, QTableWidget::item:selected {{
        background-color: {accent_dark};
        color: #FFFFFF;
    }}
    QListWidget::item:hover, QTableWidget::item:hover {{
        background-color: {bg_hover};
    }}
    QTableWidget QHeaderView::section {{
        background-color: {bg_card};
        border: 1px solid {border};
        padding: 6px 10px;
        font-weight: bold;
        color: {text_secondary};
    }}

    /* ── 文本框 ── */
    QTextEdit, QPlainTextEdit {{
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 8px;
        color: {text_primary};
        font-family: "{font_mono()}";
        font-size: 12px;
    }}
    QTextEdit:focus {{ border-color: {accent}; }}

    /* ── 进度条 ── */
    QProgressBar {{
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
        height: 18px;
        text-align: center;
        color: {text_primary};
    }}
    QProgressBar::chunk {{
        background-color: {accent};
        border-radius: 5px;
    }}

    /* ── 滚动条 ── */
    QScrollBar:vertical {{
        background: {bg_dark};
        width: 10px;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background: {bg_hover};
        border-radius: 5px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: #4A5568; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}

    /* ── 复选框 ── */
    QCheckBox {{ spacing: 8px; color: {text_primary}; }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border: 2px solid {border};
        border-radius: 4px;
        background-color: {bg_input};
    }}
    QCheckBox::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
    }}

    /* ── 选项卡 ── */
    QTabWidget::pane {{
        border: 1px solid {border};
        background: {bg_dark};
        border-radius: 6px;
    }}
    QTabBar::tab {{
        background: {bg_card};
        border: 1px solid {border};
        padding: 8px 20px;
        margin-right: 2px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        color: {text_secondary};
    }}
    QTabBar::tab:selected {{
        background: {bg_dark};
        border-bottom: 2px solid {accent};
        color: {text_primary};
    }}
    QTabBar::tab:hover {{ background: {bg_hover}; }}

    /* ── 分组框 ── */
    QGroupBox {{
        border: 1px solid {border};
        border-radius: 8px;
        margin-top: 12px;
        padding-top: 16px;
        font-weight: bold;
        color: {text_primary};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 14px;
        padding: 0 6px;
        color: {accent};
    }}

    /* ── 下拉框 ── */
    QComboBox {{
        background-color: {bg_input};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 7px 12px;
        color: {text_primary};
    }}
    QComboBox:focus {{ border-color: {accent}; }}
    QComboBox QAbstractItemView {{
        background-color: {bg_card};
        border: 1px solid {border};
        color: {text_primary};
        selection-background-color: {accent_dark};
        selection-color: #FFFFFF;
    }}

    /* ── 提示框 ── */
    QToolTip {{
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 4px 8px;
        color: {text_primary};
    }}

    /* ── 菜单 ── */
    QMenu {{
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 4px;
        color: {text_primary};
    }}
    QMenu::item {{ padding: 8px 24px; border-radius: 4px; }}
    QMenu::item:selected {{ background-color: {accent_dark}; color: #FFFFFF; }}

    /* ── 卡片 / 指标 / 条形图 / 步骤点（随主题切换） ── */
    QFrame[cssClass="panel-card"] {{
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 12px;
    }}
    QFrame[cssClass="metric-card"] {{
        background-color: {bg_card};
        border: 1px solid {border};
        border-radius: 10px;
    }}
    QLabel[cssClass="metric-value"] {{
        font-size: 22px; font-weight: bold; color: {accent};
    }}
    QLabel[cssClass="metric-label"] {{
        font-size: 12px; color: {text_secondary};
    }}
    QLabel[cssClass="panel-title"] {{
        font-size: 20px; font-weight: bold; color: {title_color};
    }}
    QLabel[cssClass="text-success"] {{ color: {success}; }}
    QLabel[cssClass="text-danger"] {{ color: {danger}; }}
    QLabel[cssClass="text-warning"] {{ color: {warning}; }}
    QLabel[cssClass="text-muted"] {{ color: {text_secondary}; }}
    QLabel[cssClass="step-dot"] {{ font-size: 12px; color: {text_secondary}; }}
    QLabel[cssClass="step-dot-active"] {{ font-size: 12px; color: {accent}; }}
    QFrame#barRow {{ background: transparent; }}
    QLabel#barLabel {{ color: {text_primary}; font-weight: 500; }}
    QLabel#barValue {{ color: {text_secondary}; }}
    """
