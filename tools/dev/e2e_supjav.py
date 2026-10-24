"""端到端验证 Supjav 检索：真实网络 + 有头浏览器 + 代理。

模拟软件真实用法：检索在后台线程，浏览器在 GUI 线程（read() 禁止从 GUI 线程发起）。

    python tools/dev/e2e_supjav.py --number YMDD-365 --wait 120
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as app_main  # noqa: E402
from utils.console import force_utf8_stdout  # noqa: E402

force_utf8_stdout()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--number", default="YMDD-365")
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--mode", default="auto")
    args = parser.parse_args()

    # 与正式启动顺序一致：先注入代理，再建 QApplication
    proxy = app_main._inject_webengine_proxy()
    print(f"代理：{proxy or '(未探测到)'}", flush=True)
    import os

    print("QTWEBENGINE_CHROMIUM_FLAGS =", os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""), flush=True)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])

    from config import ToolkitConfig
    from ui.supjav_browser import prepare_supjav_session

    cfg = ToolkitConfig.load()
    try:
        cfg.release_supjav_mode = args.mode
    except AttributeError:
        pass
    prepare_supjav_session()
    print("浏览器会话已注册（窗口不抢焦点）", flush=True)

    outcome: dict = {}

    def worker() -> None:
        try:
            from censor.releases.service import ReleaseOptions, ReleaseService

            cfg.release_supjav_mode = args.mode
            service = ReleaseService(cfg)
            started = time.monotonic()
            report = service.lookup(
                args.number,
                options=ReleaseOptions(use_cache=False, force=True),
                log=lambda m: print(f"   [log] {m}", flush=True))
            outcome["report"] = report
            outcome["cost"] = time.monotonic() - started
        except Exception as exc:  # noqa: BLE001 - 诊断要看到全部
            import traceback

            outcome["error"] = f"{type(exc).__name__}: {exc}"
            outcome["trace"] = traceback.format_exc()

    threading.Thread(target=worker, daemon=True).start()

    def finish() -> None:
        app.quit()

    QTimer.singleShot(int(args.wait * 1000), finish)
    app.exec()

    print("\n" + "=" * 60, flush=True)
    if "error" in outcome:
        print("检索抛错：", outcome["error"], flush=True)
        print(outcome.get("trace", ""), flush=True)
        return 1
    report = outcome.get("report")
    if report is None:
        print("超时：没有拿到报告（可能卡在浏览器等待）", flush=True)
        return 1
    print(f"耗时 {outcome.get('cost', 0):.1f}s", flush=True)
    print(f"番号 {report.number} · 结论 {report.availability.value} · {report.as_text()}", flush=True)
    print(f"来源状态：{json.dumps(report.sources, ensure_ascii=False, indent=2)}", flush=True)
    print(f"候选 {len(report.candidates)} 条：", flush=True)
    for item in report.candidates[:8]:
        print(f"  - [{item.source}] {item.title[:70]}", flush=True)
        print(f"    uncensored={item.uncensored} conf={item.uncensored_confidence} "
              f"reason={item.uncensored_reason}", flush=True)
        print(f"    url={item.page_url}", flush=True)
    return 0 if report.candidates else 2


if __name__ == "__main__":
    raise SystemExit(main())
