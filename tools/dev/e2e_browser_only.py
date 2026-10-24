"""只测浏览器读取这一步：窗口可见性、当前 URL、toHtml 长度、失败原因。"""

from __future__ import annotations

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
    url = sys.argv[1] if len(sys.argv) > 1 else "https://supjav.com/?s=YMDD-365"
    print("代理:", app_main._inject_webengine_proxy(), flush=True)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    from ui.supjav_browser import prepare_supjav_session

    session = prepare_supjav_session()
    outcome: dict = {}
    started = time.monotonic()

    def worker() -> None:
        try:
            html = session.read(url, timeout=45.0)
            outcome["len"] = len(html)
            outcome["head"] = html[:200]
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            outcome["cost"] = time.monotonic() - started

    threading.Thread(target=worker, daemon=True).start()

    def tick() -> None:
        page = session._page
        page_url = page.url().toString() if page is not None else "(no page)"
        visible = session.window.isVisible()
        print(f"[{time.monotonic() - started:5.1f}s] visible={visible} "
              f"page={'yes' if page is not None else 'no'} url={page_url!r} "
              f"current={session._current is not None} "
              f"timer={session._timer.isActive()} "
              f"registered={session._registered} disposed={session._disposed}", flush=True)
        if page is not None:
            def dumped(html: str) -> None:
                low = html.lower()
                posts = low.count('class="post"')
                challenge = "just a moment" in low or "challenges.cloudflare" in low
                print(f"         toHtml len={len(html)} challenge={challenge} "
                      f"posts={posts} searchresults={'search-results' in low}", flush=True)
            page.toHtml(dumped)
        if "cost" in outcome:
            print("\n结果:", outcome.get("error") or f"成功 {outcome.get('len')} 字符", flush=True)
            print("耗时:", f"{outcome.get('cost', 0):.1f}s", flush=True)
            app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(3000)
    QTimer.singleShot(70000, app.quit)
    app.exec()
    if "cost" not in outcome:
        print("\n仍未完成（70s 上限）", flush=True)
    return 0 if "len" in outcome else 1


if __name__ == "__main__":
    raise SystemExit(main())
