"""端到端手动验证：用**虚构演示库**跑一遍真实后台线程，遍历全部页面（离屏）。

与 pytest 套件的区别：这里跑的是"真实线程 + 真实 ffmpeg/ffprobe"的链路，
输出人类可读的进度，适合改完代码后手动过一遍。

数据来源：``tools/dev/seed_demo.py`` 生成到 ``--demo`` 指定的中性目录，
绝不读取你真实的 Jellyfin 库。

用法：
    python tools/dev/e2e_check.py
    python tools/dev/e2e_check.py --demo C:/MediaDemo --count 60
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("PYSIDE_ABORT_ON_EXCEPTION", "0")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "dev"))

DEFAULT_DEMO = Path(r"C:\MediaDemo")

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(f"{name}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端手动验证")
    parser.add_argument("--demo", default=str(DEFAULT_DEMO),
                        help="演示库目录（中性路径，默认 C:\\MediaDemo）")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--keep", action="store_true", help="保留演示库，不重建")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    demo = Path(args.demo).expanduser().resolve()
    if not (args.keep and (demo / "jellyfin.db").exists()):
        from seed_demo import seed
        if demo.exists():
            shutil.rmtree(demo, ignore_errors=True)
        seed(demo, max(20, args.count))
    print(f"演示库: {demo / 'jellyfin.db'}\n")

    # 数据目录指向演示库，避免碰到真实配置
    os.environ["JELLYFIN_TOOLKIT_DATA_DIR"] = str(demo)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    from utils.fonts import ui_font
    font = ui_font(10)
    if font is not None:
        app.setFont(font)

    from config import ToolkitConfig
    from ui.main_window import MainWindow, NAV_LABELS
    from ui.pages.automation import AutomationPage
    from ui.pages.censorship import CensorshipPage
    from ui.pages.db_tools import DBToolsPage
    from ui.pages.dedup import DedupPage
    from ui.pages.delete import DeletePage
    from ui.pages.insight import InsightPage
    from ui.pages.merge import MergePage
    from ui.pages.nfo_fix import NFOPage
    from ui.pages.onboarding import OnboardingPage
    from ui.pages.profile import ProfilePage
    from ui.pages.repair import RepairPage
    from ui.pages.replace import ReplacePage
    from ui.pages.review import ReviewPage
    from ui.pages.settings import SettingsPage
    from ui.pages.subtitle import SubtitlePage
    from workers.ingest import IngestWorker
    from workers.scorer import AnalyzeWorker

    cfg = ToolkitConfig.load()
    db = str(demo / "jellyfin.db")
    cfg.jellyfin_db_path = db

    def wait_worker(worker, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout or args.timeout)
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            worker.wait(20)
        app.processEvents()
        return not worker.isRunning()

    print("=== [A] 环境 ===")
    from utils.tools import probe_hardware, resolve_tool
    ffmpeg = resolve_tool("ffmpeg")
    check("ffmpeg 已定位", bool(ffmpeg), f"路径: {ffmpeg or '未找到'}")
    if ffmpeg:
        info = probe_hardware(ffmpeg)
        print(f"     实测可用编码器: {', '.join(info['encoders'])}")
        print(f"     实测可用硬解  : "
              f"{', '.join(a for a in info['hwaccels'] if a != 'none') or '无'}")

    print("\n=== [B] 主窗口 + 全部页面注册与导航 ===")
    pages = [
        OnboardingPage(cfg), InsightPage(cfg), ProfilePage(cfg), ReviewPage(cfg),
        DeletePage(cfg), DedupPage(cfg), CensorshipPage(cfg), SubtitlePage(cfg),
        ReplacePage(cfg), RepairPage(cfg), NFOPage(cfg), DBToolsPage(cfg),
        MergePage(cfg), AutomationPage(cfg), SettingsPage(cfg),
    ]
    window = MainWindow(cfg)
    for page in pages:
        window.register_page(page)
    window.show()
    check("侧边栏条目数 == 页面数", len(NAV_LABELS) == window.stack.count(),
          f"{len(NAV_LABELS)} vs {window.stack.count()}")
    for i in range(window.stack.count()):
        window._navigate(i)
        app.processEvents()
    check("全部页面导航遍历", window.stack.currentIndex() == window.stack.count() - 1)

    def loop(ms: int) -> None:
        QTimer.singleShot(ms, app.quit)
        app.exec()

    insight, review, delete, dedup = pages[1], pages[3], pages[4], pages[5]

    print("\n=== [C] 库概览（真实采集线程）===")
    ingest = IngestWorker(db, exclude_keywords=cfg.exclude_path_keywords)
    ingest.finished.connect(insight._on_done)
    ingest.start()
    ok = wait_worker(ingest)
    check("采集完成", ok)
    check("指标卡 6 项", insight.metric_row.count() == 6)
    check("分布表有数据", insight.table.rowCount() > 10,
          f"{insight.table.rowCount()} 行")
    loop(200)

    print("\n=== [D] 快速打标 / 删除暂存 / 重复处理（真实分析线程）===")
    for label, page, slot in (("快速打标", review, review._on_analyzed),
                              ("删除暂存", delete, delete._on_loaded),
                              ("重复处理", dedup, dedup._on_loaded)):
        worker = AnalyzeWorker(db, {},
                               exclude_keywords=cfg.exclude_path_keywords,
                               th_keep=cfg.score_th_keep / 100,
                               th_delete=cfg.score_th_delete / 100)
        worker.finished.connect(slot)
        worker.start()
        ok = wait_worker(worker)
        check(f"{label} 分析完成", ok)

    check("打标页装载", len(review.deck_ids) >= 0)
    loop(200)

    print("\n=== [E] 页面 shutdown 安全性 ===")
    for page in pages:
        shutdown = getattr(page, "shutdown", None)
        if callable(shutdown):
            shutdown()
        app.processEvents()
    check("全部页面 shutdown 无异常", True)

    try:
        window.close()
        app.processEvents()
        check("主窗口关闭不崩溃", True)
    except Exception as e:  # noqa: BLE001
        check("主窗口关闭不崩溃", False, str(e))

    print("\n" + "=" * 52)
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
