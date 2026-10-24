"""用有头 QtWebEngine 打开 Supjav，把渲染后的 HTML 写盘 —— 判断"软件自己的浏览器"能不能过 CF。

必须在真实 GUI 会话里跑（不是 offscreen）：
    set QTWEBENGINE_CHROMIUM_FLAGS=--proxy-server=http://127.0.0.1:7897
    python tools/dev/webengine_probe.py --url "https://supjav.com/?s=YMDD-365" --out /tmp/probe.html --wait 30
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://supjav.com/?s=YMDD-365")
    parser.add_argument("--out", default=str(Path(os.environ.get("TEMP", "/tmp")) / "probe.html"))
    parser.add_argument("--wait", type=float, default=30.0)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    view.resize(1200, 800)
    if not args.no_window:
        view.show()          # 关键：有头窗口才会带上完整浏览器指纹

    state = {"loaded": False, "done": False}

    def on_loaded(ok: bool) -> None:
        state["loaded"] = bool(ok)
        print(f"loadFinished ok={ok} url={view.url().toString()}", flush=True)

    def dump() -> None:
        if state["done"]:
            return
        state["done"] = True

        def write(html: str) -> None:
            path = Path(args.out)
            path.write_text(html, encoding="utf-8", errors="replace")
            low = html.lower()
            print(f"写入 {path} · {len(html)} 字符", flush=True)
            print("含挑战页标记:", ("just a moment" in low) or ("challenges.cloudflare" in low),
                  flush=True)
            print("含 supjav 文章链接:", f"{html.count('.html')} 处 .html", flush=True)
            app.quit()

        view.page().toHtml(write)

    view.loadFinished.connect(on_loaded)
    view.load(QUrl(args.url))
    QTimer.singleShot(int(args.wait * 1000), dump)
    QTimer.singleShot(int(args.wait * 1000) + 8000, app.quit)
    app.exec()
    if not state["done"]:
        print("超时未取到 HTML", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
