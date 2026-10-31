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

PAGE_SPECS = [
    ("ui.pages.onboarding", "OnboardingPage"),
    ("ui.pages.insight", "InsightPage"),
    ("ui.pages.profile", "ProfilePage"),
    ("ui.pages.review", "ReviewPage"),
    ("ui.pages.delete", "DeletePage"),
    ("ui.pages.dedup", "DedupPage"),
    ("ui.pages.censorship", "CensorshipPage"),
    ("ui.pages.censor_replace", "CensorReplacePage"),
    ("ui.pages.subtitle", "SubtitlePage"),
    ("ui.pages.subtitle_gen", "SubtitleGenPage"),
    ("ui.pages.replace", "ReplacePage"),
    ("ui.pages.lada", "LadaPage"),
    ("ui.pages.repair", "RepairPage"),
    ("ui.pages.nfo_fix", "NFOPage"),
    ("ui.pages.db_tools", "DBToolsPage"),
    ("ui.pages.merge", "MergePage"),
    ("ui.pages.automation", "AutomationPage"),
    ("ui.pages.settings", "SettingsPage"),
    ("ui.pages.tasks", "TaskCenterPage"),
]


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
    """按页面标题跳转（供新手引导使用）。

    走 ``MainWindow.goto_label``：范围未定时会直接打开范围选择框，
    避免把用户送进一个"主按钮全灰"的页面。
    """
    goto = getattr(window, "goto_label", None)
    if callable(goto):
        goto(label)
        return
    from ui.main_window import NAV_LABELS

    if label in NAV_LABELS:
        window._navigate(NAV_LABELS.index(label))


def _wire_media_upgrades(window, replace_page):
    from ui.services import events
    def send_originals(paths):
        replace_page.receive_originals(paths)
        _goto(window, "视频版本管理")
    events.media_upgrade_requested.connect(send_originals)
    return send_originals


def _wire_release_lookup(window, censor_replace_page):
    """检查页 →「替换版本检索」：只查用户在检查页挑出来的那些。

    业务流程是"先确认有没有可替换的版本，再决定替不替代"，所以从检查页出去的
    第一步是核查，不是替换。这里只做转发，范围判定在目标页里。
    """
    from ui.services import events

    def send_for_lookup(paths):
        censor_replace_page.receive_requests(paths)
        _goto(window, "替换版本检索")

    events.release_lookup_requested.connect(send_for_lookup)
    return send_for_lookup


def _force_utf8_when_piped() -> None:
    """输出被管道/重定向时改用 UTF-8（实现在 utils.console，供各入口脚本共用）。

    冻结的 exe 在中文 Windows 上默认按 GBK 输出，``--doctor > out.txt`` 会得到
    一份在 GitHub 上显示成乱码的文件 —— 而 Issue 模板恰好要求粘贴这段输出。
    """
    from utils.console import force_utf8_stdout

    force_utf8_stdout()


def print_doctor() -> int:
    """打印环境自检结果（发布前/用户报障时收集环境用），不启动界面。"""
    from config import doctor

    info = doctor()
    print(f"{APP_NAME_CN} v{__version__}")
    print("-" * 52)
    print(f"{'data_dir':<12}: {info.get('data_dir', '')}")
    print(f"{'config_file':<12}: {info.get('config_file', '')}")
    print(
        f"{'mode':<12}: {'frozen/exe' if info.get('frozen') else 'source'}"
        f"{' (portable)' if info.get('portable') else ''}"
    )
    print(f"{'python':<12}: {info.get('python', '')}")
    print(f"{'ffmpeg':<12}: {info.get('ffmpeg') or 'NOT FOUND'}")
    print(f"{'ffprobe':<12}: {info.get('ffprobe') or 'NOT FOUND'}")
    if info.get("encoders") is not None:
        print(f"{'encoders':<12}: {', '.join(info['encoders']) or '(libx264 only)'}")
        print(
            f"{'hwaccels':<12}: {', '.join(a for a in info['hwaccels'] if a != 'none') or '(none)'}"
        )
        print(
            f"{'recommend':<12}: {info.get('recommended_encoder')} / "
            f"{info.get('recommended_hwaccel')}"
        )
    print("-" * 52)
    print("说明：编码器/硬解结果均由真实跑一次得出，而非 ffmpeg 的编译期列表。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME_CN, description="Jellyfin 影视库管理工具箱")
    parser.add_argument("--version", action="version", version=f"{APP_NAME_CN} {__version__}")
    parser.add_argument(
        "--doctor", action="store_true", help="打印环境自检结果后退出（不启动界面）"
    )
    parser.add_argument(
        "--portable", action="store_true", help="便携模式（默认）：配置与 data 保存在程序文件夹内"
    )
    parser.add_argument(
        "--data-dir", default="", help="已有资料库根目录（包含 config.json 和 data 子目录）"
    )
    parser.add_argument('--init-profile', action='store_true', help='明确初始化新资料库；已有身份文件的缺失库不会重建')
    parser.add_argument('--profile-info', action='store_true', help='只读显示实际资料库位置和状态后退出')
    return parser


def _inject_webengine_proxy() -> str:
    """把 HTTP 层解析到的代理解析结果同步给 QtWebEngine。

    必须在 QApplication 构造前调用 —— Chromium 的代理是启动参数，
    进程起来之后再改就无效了。
    """
    # 受限账户/安全软件下 Chromium 沙箱可能起不来，表现为"浏览器窗口开了但
    # 页面一直空白"。允许用环境变量开启兼容模式（只影响 Supjav 只读会话）。
    if str(os.environ.get("JELLYFIN_TOOLKIT_WEBENGINE_NO_SANDBOX", "")).strip().lower() in {
            "1", "true", "yes", "on"}:
        os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    try:
        from censor.releases.transport import detect_proxy
        from config import ToolkitConfig

        proxy = detect_proxy(str(getattr(ToolkitConfig.load(), "release_proxy", "") or ""))
    except Exception:  # noqa: BLE001 - 代理注入失败不应该挡住启动
        return ""
    if not proxy:
        return ""
    flags = str(os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")).strip()
    if "--proxy-server" in flags:
        return proxy
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --proxy-server={proxy}".strip()
    return proxy


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])

    # 数据目录必须在导入 config 之前定好
    if args.portable:
        os.environ["JELLYFIN_TOOLKIT_PORTABLE"] = "1"
    if args.data_dir:
        os.environ["JELLYFIN_TOOLKIT_DATA_DIR"] = args.data_dir

    if args.profile_info:
        _force_utf8_when_piped()
        import config
        from utils.data_profile import format_profile_diagnostics, profile_diagnostics, ProfileError
        try:
            print(format_profile_diagnostics(profile_diagnostics(config.BASE_DIR, config.program_dir() / 'data-profile.json')))
            return 0
        except (ProfileError, OSError) as exc:
            print(str(exc))
            return 1

    if args.doctor:
        _force_utf8_when_piped()
        return print_doctor()

    # Only profile-selection widgets may be created before proxy selection;
    # WebEngine imports and browser profiles follow the validated data root.
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    from ui.data_profiles import prepare_startup_profile, release_startup_profile
    if not prepare_startup_profile(create=args.init_profile):
        return 1
    app.aboutToQuit.connect(release_startup_profile)
    # No WebEngine modules or profiles have been loaded yet. Read the proxy
    # only after the selected profile has been validated and locked.
    _inject_webengine_proxy()
    _install_excepthook()

    from config import ToolkitConfig
    from utils.fonts import ui_font
    from utils.tools import configure_tools, resolve_tool
    from ui.main_window import MainWindow
    from ui.pages.onboarding import OnboardingPage
    from ui.pages.insight import InsightPage
    from ui.pages.profile import ProfilePage
    from ui.pages.review import ReviewPage
    from ui.pages.delete import DeletePage
    from ui.pages.dedup import DedupPage
    from ui.pages.censorship import CensorshipPage
    from ui.pages.censor_replace import CensorReplacePage
    from ui.pages.subtitle import SubtitlePage
    from ui.pages.subtitle_gen import SubtitleGenPage
    from ui.pages.replace import ReplacePage
    from ui.pages.lada import LadaPage
    from ui.pages.repair import RepairPage
    from ui.pages.nfo_fix import NFOPage
    from ui.pages.db_tools import DBToolsPage
    from ui.pages.merge import MergePage
    from ui.pages.automation import AutomationPage
    from ui.pages.settings import SettingsPage
    from ui.pages.tasks import TaskCenterPage
    from ui.services import events

    app.setStyle("Fusion")
    font = ui_font(10)
    if font is not None:
        app.setFont(font)

    cfg = ToolkitConfig.load()
    if cfg.release_enabled and 'supjav' in cfg.release_sources and cfg.release_supjav_mode != 'index':
        from censor.releases.transport import offline
        if not offline():
            from ui.supjav_browser import prepare_supjav_session
            prepare_supjav_session()
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
    censor_replace_page = CensorReplacePage(cfg)
    window.register_page(censor_replace_page)
    subtitle_page = SubtitlePage(cfg)
    subtitle_gen_page = SubtitleGenPage(cfg)
    replace_page = ReplacePage(cfg)
    window.register_page(subtitle_page)
    window.register_page(subtitle_gen_page)
    window.register_page(replace_page)

    def send_subtitles(paths):
        subtitle_gen_page._add_paths(paths)
        _goto(window, "批量生成字幕")

    def send_restored(paths, originals):
        replace_page.receive_outputs(paths, originals)
        _goto(window, "视频版本管理")

    events.subtitle_ready.connect(send_subtitles)
    events.restored_ready.connect(send_restored)
    def send_candidates(paths, originals):
        replace_page.receive_candidates(paths, originals)
        _goto(window, "视频版本管理")
    events.replacement_ready.connect(send_candidates)
    _wire_media_upgrades(window, replace_page)
    # 复用第 273 行已注册的那个实例 —— 新建一个会导致"信号连到 A、界面显示 B"
    _wire_release_lookup(window, censor_replace_page)
    # Lada 需要 ffprobe 校验产出时长，用与 ffmpeg 同一套解析逻辑取它的路径
    lada_page = LadaPage(cfg)
    def send_lada(paths):
        lada_page._add_paths(paths)
        _goto(window, "画面修复（Lada）")
    events.lada_ready.connect(send_lada)
    lada_page.set_ffprobe(resolve_tool("ffprobe") or "ffprobe")
    window.register_page(lada_page)
    window.register_page(RepairPage(cfg))
    window.register_page(NFOPage(cfg))
    window.register_page(DBToolsPage(cfg))
    window.register_page(MergePage(cfg))
    window.register_page(AutomationPage(cfg))
    window.register_page(settings_page)
    window.register_page(TaskCenterPage(cfg))

    # 主题切换：设置页选择 → 主窗口立即应用（保存时才写入配置）
    settings_page.theme_changed.connect(window.apply_theme)

    # 首次运行自动进入新手引导
    if not cfg.onboarding_done:
        window._navigate(0)
    else:
        window._navigate(1)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
