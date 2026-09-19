"""离屏渲染全部页面并保存 PNG，用于 README 截图。

要点（踩过的坑，别删）：
- ``QT_QPA_PLATFORM=offscreen`` 下 Qt 找不到系统字体，中文会整屏渲染成 ``□``；
  必须同时设 ``QT_QPA_FONTDIR=C:\\Windows\\Fonts``。
- 截图前先 ``resize()`` + 少量 ``processEvents()`` 让布局生效。
- **演示数据必须放在中性路径**（默认 ``C:\\MediaDemo``），绝不能让项目目录或
  用户名出现在截图里；否则 README 就泄露了构建环境。
- 数据由 ``tools/dev/seed_demo.py`` 生成，全部为虚构编号。

用法：python tools/dev/make_screenshots.py [--out docs/images]
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

# 中性演示路径：不带用户名、不带构建目录
DEMO_DIR = Path(r"C:\MediaDemo")

SLUGS = [
    "onboarding", "insight", "profile", "review", "delete", "dedup",
    "censorship", "subtitle", "replace", "repair", "nfo_fix", "db_tools",
    "merge", "automation", "settings",
]


def _prepare_demo() -> Path:
    """生成一份虚构演示库到中性路径（每次重建，保证截图内容一致）。"""
    sys.path.insert(0, str(ROOT / "tools" / "dev"))
    from seed_demo import seed  # noqa: PLC0415

    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR, ignore_errors=True)
    seed(DEMO_DIR, 90)
    return DEMO_DIR


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 README 截图")
    parser.add_argument("--out", default=str(ROOT / "docs" / "images"))
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    demo = _prepare_demo()
    # 数据目录指向演示库，绝不碰用户真实配置
    os.environ["JELLYFIN_TOOLKIT_DATA_DIR"] = str(demo)

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from config import ToolkitConfig
    from utils.fonts import ui_font
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

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    font = ui_font(10)
    if font is not None:
        app.setFont(font)

    cfg = ToolkitConfig.load()
    db_path = str(demo / "jellyfin.db")
    cfg.jellyfin_db_path = db_path

    ins = InsightPage(cfg)
    rev = ReviewPage(cfg)
    dele = DeletePage(cfg)
    ded = DedupPage(cfg)

    def wait_worker(worker, timeout: float = 120.0) -> bool:
        deadline = time.monotonic() + timeout
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            worker.wait(20)
        app.processEvents()
        return not worker.isRunning()

    # 先让各页真正跑一遍，截出来的才是有数据的界面
    ingest = IngestWorker(db_path, exclude_keywords=cfg.exclude_path_keywords)
    ingest.finished.connect(ins._on_done)
    ingest.start()
    wait_worker(ingest)

    for page, slot in ((rev, rev._on_analyzed), (dele, dele._on_loaded), (ded, ded._on_loaded)):
        worker = AnalyzeWorker(
            db_path, {},
            exclude_keywords=cfg.exclude_path_keywords,
            th_keep=cfg.score_th_keep / 100,
            th_delete=cfg.score_th_delete / 100,
        )
        worker.finished.connect(slot)
        worker.start()
        wait_worker(worker)

    window = MainWindow(cfg)
    pages = [
        OnboardingPage(cfg), ins, ProfilePage(cfg), rev, dele, ded,
        CensorshipPage(cfg), SubtitlePage(cfg), ReplacePage(cfg), RepairPage(cfg),
        NFOPage(cfg), DBToolsPage(cfg), MergePage(cfg), AutomationPage(cfg),
        SettingsPage(cfg),
    ]
    for page in pages:
        window.register_page(page)

    window.show()
    window.resize(args.width, args.height)

    def spin(ms: int) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    for idx, _label in enumerate(NAV_LABELS):
        window._navigate(idx)
        spin(260)
        slug = SLUGS[idx] if idx < len(SLUGS) else f"page_{idx:02d}"
        path = out_dir / f"{idx:02d}_{slug}.png"
        window.grab().save(str(path))
        print("saved", path.name)

    print(f"\n共 {len(NAV_LABELS)} 张 -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
