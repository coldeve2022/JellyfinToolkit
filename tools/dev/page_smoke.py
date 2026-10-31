"""离屏构造所有页面，抓「打开就报错 / 页面根本没建起来」这类问题。

用法::

    python tools/dev/page_smoke.py            # 构造全部页面并打印体检表
    python tools/dev/page_smoke.py -v         # 额外打印每个页面的控件/按钮统计
    python tools/dev/page_smoke.py --json     # 机器可读

为什么需要它：1863 个用例里真正 new 出全部 19 个页面的只有 `test_gui_smoke` 一处，
而它用的是 fixture 里的最小配置。真实启动路径（main.py）会按配置注入 ffmpeg 路径、
准备 Supjav 会话、构造 ScopeBar —— 这些分支任何一条抛异常，用户看到的就是
"双击 exe 没反应"。这个脚本走的是**接近真实**的构造路径，但把数据目录重定向到
临时目录，不碰用户的真数据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _isolate_data_dir() -> Path:
    """把数据目录指到临时目录，避免冒烟测试改到用户的真库/真配置。"""
    tmp = Path(tempfile.mkdtemp(prefix="jt-smoke-"))
    os.environ["JELLYFIN_TOOLKIT_DATA_DIR"] = str(tmp)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 关掉 WebEngine 的 GPU 与沙箱，离屏环境下这两样最容易 fsck 崩溃
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    return tmp


def _iter_controls(widget):
    from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit

    yield from widget.findChildren(QAbstractButton)
    yield from widget.findChildren(QComboBox)
    yield from widget.findChildren(QLineEdit)


def _page_report(page) -> dict:
    """统计页面上的控件状态。

    注意：**这里查不出「按钮没连槽」**。PySide6 的 Python 信号连接不进 Qt 的
    receiver 列表（``QObject.receivers("clicked()")`` 恒为 0，实测过），
    ``SignalInstance`` 也没有 ``.object`` / ``.signal`` 可以反查。连没连槽属于
    静态可判定问题，交给 ``tools/dev/handler_check.py`` 用 AST 查。
    """
    from PySide6.QtWidgets import QAbstractButton

    buttons = list(page.findChildren(QAbstractButton))
    disabled = [b.text() or b.objectName() or type(b).__name__
                for b in buttons if not b.isEnabled()]
    nameless = [type(b).__name__ for b in buttons
                if not (b.text() or b.toolTip() or b.objectName())]
    return {
        "buttons": len(buttons),
        "enabled": sum(1 for b in buttons if b.isEnabled()),
        "disabled": disabled,
        "nameless": nameless,
    }


def run(verbose: bool = False) -> dict:
    tmp = _isolate_data_dir()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    from config import ToolkitConfig

    cfg = ToolkitConfig.load()

    # 与 main.py 保持同一份「页面清单」，避免这里漏测
    page_specs = [
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

    results = []
    for module_name, class_name in page_specs:
        entry = {"module": module_name, "class": class_name, "ok": False, "error": ""}
        try:
            module = __import__(module_name, fromlist=[class_name])
            cls = getattr(module, class_name)
            page = cls(cfg)
            app.processEvents()
            entry["ok"] = True
            entry.update(_page_report(page))
            # 关掉页面自己的线程，避免脚本退出时 "QThread destroyed while running"
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
            page.deleteLater()
            app.processEvents()
        except Exception:  # noqa: BLE001 - 冒烟测试要抓的就是任何异常
            entry["error"] = traceback.format_exc(limit=6)
        results.append(entry)

    # 主窗口整体构造（会连带 ScopeBar / 侧边栏 / 状态栏）
    window_entry = {"module": "ui.main_window", "class": "MainWindow", "ok": False, "error": ""}
    try:
        from ui.main_window import MainWindow

        win = MainWindow(cfg)
        app.processEvents()
        window_entry["ok"] = True
        window_entry["nav_items"] = len(win.NAV_ITEMS)
        win.close()
        app.processEvents()
    except Exception:  # noqa: BLE001
        window_entry["error"] = traceback.format_exc(limit=6)
    results.append(window_entry)

    return {"data_dir": str(tmp), "results": results}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离屏构造全部页面")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = run(verbose=args.verbose)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        failed = 0
        print(f"{'页面':<26} {'结果':<5} {'按钮':>4} {'可用':>4} {'禁用':>4}  备注")
        print("-" * 82)
        for row in report["results"]:
            name = row["class"]
            if row["ok"]:
                note = ""
                if row.get("nameless"):
                    note = f"{len(row['nameless'])} 个控件既无文本也无提示"
                print(f"{name:<26} {'OK':<5} {row.get('buttons', 0):>4} "
                      f"{row.get('enabled', 0):>4} {len(row.get('disabled', [])):>4}  {note}")
            else:
                failed += 1
                first = (row["error"].strip().splitlines() or [""])[-1]
                print(f"{name:<26} {'FAIL':<5} {'-':>4} {'-':>4} {'-':>4}  {first}")
        print("-" * 82)
        print(f"共 {len(report['results'])} 项，失败 {failed} 项；数据目录 {report['data_dir']}")
        if failed and args.verbose:
            for row in report["results"]:
                if not row["ok"]:
                    print(f"\n===== {row['class']} =====")
                    print(row["error"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
