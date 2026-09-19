"""库数据采集后台线程 — 包装 utils.library_source。

数据来源由 ``utils.library_source`` 按配置决定（本地数据库 / 服务器 API），
Jellyfin 与 Emby 共用这一条路径。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from utils import library_source
from utils.library import summarize


class IngestWorker(QThread):
    """后台采集全库并汇总索引，emit log/progress/finished。

    ``source`` 传配置对象（``ToolkitConfig``）；也接受数据库路径字符串以兼容旧调用。
    """

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # {"items": list[Item], "summary": dict} 或 {"error": str}

    def __init__(self, source, exclude_keywords: list | None = None, parent=None):
        super().__init__(parent)
        self.source = source
        self.exclude_keywords = exclude_keywords or []
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        try:
            items = library_source.collect(
                self.source,
                log=self.log.emit,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
                exclude_keywords=self.exclude_keywords,
            )
            summary = summarize(items)
            self.finished.emit({"items": items, "summary": summary})
        except Exception as e:  # noqa: BLE001
            self.finished.emit({"error": str(e)})
