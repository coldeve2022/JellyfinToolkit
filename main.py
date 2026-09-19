"""Jellyfin Toolkit — 影视库管理工具箱。

统一整合所有 Jellyfin 管理工具的现代桌面应用。
支持深色/浅色主题切换、无窗口启动。

用法:
    python main.py               # 启动图形界面
    python main.py --doctor      # 环境自检（不启动界面）
    python main.py --version     # 打印版本
    python main.py --portable    # 强制便携模式（数据写在程序目录）
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime

# 关键：让 PySide6 槽内异常不直接 abort 进程（qFatal），而是回落到 excepthook 记录。
# 必须在创建 QApplication / import 交互前设置。
os.environ.setdefault("PYSIDE_ABORT_ON_EXCEPTION", "0")

from version import APP_NAME_CN, __version__


def _install_excepthook() -> None:
    """把未捕获异常写入 app-error.log，避免 PySide6 静默崩溃且便于诊断。

    日志写到用户数据目录（冻结模式下为 %APPDATA%/<APP_ID>），
    保证打包后仍可写且不会污染只读的 _internal 目录。
    """
    from config import DATA_DIR
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    log_path = DATA_DIR / "app-error.log"

    def hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {tb}\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _goto(window, label: str) -> None:
    """按页面标题跳转（供新手引导使用）。"""
    from ui.main_window import NAV_LABELS

    if label in NAV_LABELS:
        window._navigate(NAV_LABELS.index(label))


def _force_utf8_when_piped() -> None:
    """输出被管道/重定向时改用 UTF-8。

    冻结的 exe 在中文 Windows 上默认按 GBK 输出，
    ``JellyfinToolkit.exe --doctor > out.txt`` 会得到一份在 GitHub 上显示成乱码的
    文件 —— 而 Issue 模板恰好要求用户粘贴这段输出（实测确实乱码）。

    只在**非交互**（管道/重定向）时切换：直接跑在控制台里时保持系统编码，
    否则中文控制台自己的显示反而会花屏。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:          # GUI 版 exe 没有控制台时 stdout 可能是 None
            continue
        try:
            if stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue
def print_doctor() -> int:
    """打印环境自检结果（发布前/用户报障时收集环境用），不启动界面。"""
    from config import doctor

    info = doctor()
    print(f"{APP_NAME_CN} v{__version__}")
    print("-" * 52)
    print(f"{'data_dir':<12}: {info.get('data_dir', '')}")
    print(f"{'config_file':<12}: {info.get('config_file', '')}")
    print(f"{'mode':<12}: {'frozen/exe' if info.get('frozen') else 'source'}"
          f"{' (portable)' if info.get('portable') else ''}")
    print(f"{'python':<12}: {info.get('python', '')}")
    print(f"{'ffmpeg':<12}: {info.get('ffmpeg') or 'NOT FOUND'}")
    print(f"{'ffprobe':<12}: {info.get('ffprobe') or 'NOT FOUND'}")
    if info.get("encoders") is not None:
        print(f"{'encoders':<12}: {', '.join(info['encoders']) or '(libx264 only)'}")
        print(f"{'hwaccels':<12}: "
              f"{', '.join(a for a in info['hwaccels'] if a != 'none') or '(none)'}")
        print(f"{'recommend':<12}: {info.get('recommended_encoder')} / "
              f"{info.get('recommended_hwaccel')}")
    print("-" * 52)
    print("说明：编码器/硬解结果均由真实跑一次得出，而非 ffmpeg 的编译期列表。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME_CN, description="Jellyfin 影视库管理工具箱")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME_CN} {__version__}")
    parser.add_argument("--doctor", action="store_true",
                        help="打印环境自检结果后退出（不启动界面）")
    parser.add_argument("--portable", action="store_true",
                        help="强制便携模式：配置与数据写在程序目录下的 AppData/")
    parser.add_argument("--data-dir", default="",
                        help="自定义数据目录（等价于环境变量 JELLYFIN_TOOLKIT_DATA_DIR）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])

    # 数据目录必须在导入 config 之前定好
    if args.portable:
        os.environ["JELLYFIN_TOOLKIT_PORTABLE"] = "1"
    if args.data_dir:
        os.environ["JELLYFIN_TOOLKIT_DATA_DIR"] = args.data_dir

    if args.doctor:
        _force_utf8_when_piped()
        return print_doctor()

    _install_excepthook()

    from PySide6.QtWidgets import QApplication

    from config import ToolkitConfig
    from utils.fonts import ui_font
    from utils.tools import configure_tools
    from ui.main_window import MainWindow
    from ui.pages.onboarding import OnboardingPage
    from ui.pages.insight import InsightPage
    from ui.pages.profile import ProfilePage
    from ui.pages.review import ReviewPage
    from ui.pages.delete import DeletePage
    from ui.pages.dedup import DedupPage
    from ui.pages.censorship import CensorshipPage
    from ui.pages.subtitle import SubtitlePage
    from ui.pages.replace import ReplacePage
    from ui.pages.repair import RepairPage
    from ui.pages.nfo_fix import NFOPage
    from ui.pages.db_tools import DBToolsPage
    from ui.pages.merge import MergePage
    from ui.pages.automation import AutomationPage
    from ui.pages.settings import SettingsPage

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = ui_font(10)
    if font is not None:
        app.setFont(font)

    cfg = ToolkitConfig.load()
    # 用户配置里的 ffmpeg 路径要在任何页面构造前注入
    configure_tools(cfg.merge_ffmpeg_path, force=True)

    window = MainWindow(cfg)

    settings_page = SettingsPage(cfg)
    onboarding = OnboardingPage(cfg)
    onboarding.navigate_requested.connect(lambda label: _goto(window, label))

    window.register_page(onboarding)
    window.register_page(InsightPage(cfg))
    window.register_page(ProfilePage(cfg))
    window.register_page(ReviewPage(cfg))
    window.register_page(DeletePage(cfg))
    window.register_page(DedupPage(cfg))
    window.register_page(CensorshipPage(cfg))
    window.register_page(SubtitlePage(cfg))
    window.register_page(ReplacePage(cfg))
    window.register_page(RepairPage(cfg))
    window.register_page(NFOPage(cfg))
    window.register_page(DBToolsPage(cfg))
    window.register_page(MergePage(cfg))
    window.register_page(AutomationPage(cfg))
    window.register_page(settings_page)

    # 主题切换：设置页选择 → 主窗口立即应用（保存时才写入配置）
    settings_page.theme_changed.connect(window.apply_theme)

    # 首次运行自动进入新手引导
    if not cfg.onboarding_done:
        window._navigate(0)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
