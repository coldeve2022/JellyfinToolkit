"""库数据采集后台线程 — 包装 utils.library_source。

数据来源由 ``utils.library_source`` 按配置决定（本地数据库 / 服务器 API），
Jellyfin 与 Emby 共用这一条路径。
"""
from __future__ import annotations
from copy import deepcopy

from PySide6.QtCore import QThread, Signal

from utils import library_source
from utils.library import summarize
from utils.state_context import resolved_identity, state_context


class IngestWorker(QThread):
    """后台采集全库并汇总索引，emit log/progress/finished。

    ``source`` 传配置对象（``ToolkitConfig``）；也接受数据库路径字符串以兼容旧调用。
    """

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # {"items": list[Item], "summary": dict} 或 {"error": str}

    def __init__(self, source, exclude_keywords: list | None = None, parent=None):
        super().__init__(parent)
        self.source = deepcopy(source)
        self.exclude_keywords = list(exclude_keywords or [])
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
            if self._stopped:
                raise library_source.CollectionCancelled("采集已取消；未发布部分统计")
            summary = summarize(items)
            user, source = resolved_identity(items, self.source)
            self.finished.emit({"items": items, "summary": summary, "status": "succeeded",
                                "source_context": state_context(self.source, items),
                                "resolved_user_id": user, "resolved_source_identity": source})
        except library_source.CollectionCancelled as e:
            self.finished.emit({"cancelled": True, "status": "cancelled", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.finished.emit({"error": str(e), "status": "failed"})
