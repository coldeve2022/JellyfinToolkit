"""Library analysis and reranking consume the same pure scoring snapshot as automation."""
from __future__ import annotations
from copy import deepcopy
from PySide6.QtCore import QThread, Signal
from utils import library_source
from utils.scoring_service import score_snapshot
from config import ToolkitConfig


def __getattr__(name):
    if name == "DATA_DIR":
        import config
        return config.DATA_DIR
    raise AttributeError(name)


class AnalyzeWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)

    def __init__(self, source, labels=None, exclude_keywords=None, th_keep=None, th_delete=None, parent=None):
        super().__init__(parent)
        self.source = deepcopy(source)
        self.labels = None if labels is None else dict(labels)
        self.exclude_keywords = list(exclude_keywords or [])
        self.th_keep, self.th_delete = th_keep, th_delete
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()

    def run(self):
        try:
            items = library_source.collect(self.source, log=self.log.emit, progress=self.progress.emit,
                                           should_stop=lambda: self._stopped,
                                           exclude_keywords=self.exclude_keywords)
            if self._stopped:
                raise library_source.CollectionCancelled("分析已取消；部分结果不参与分类")
            cfg = self.source if not isinstance(self.source, str) else ToolkitConfig.load()
            result = score_snapshot(items, cfg, self.labels, th_keep=self.th_keep, th_delete=self.th_delete)
            if self._stopped:
                raise library_source.CollectionCancelled("分析已取消；部分结果不参与分类")
            for warning in result["warnings"]:
                self.log.emit(warning)
            self.finished.emit(result)
        except library_source.CollectionCancelled as exc:
            self.finished.emit({"error": str(exc), "cancelled": True, "status": "cancelled"})
        except Exception as exc:
            self.finished.emit({"error": str(exc), "status": "failed"})


class RerankWorker(QThread):
    finished = Signal(dict)

    def __init__(self, items, labels, th_keep=None, th_delete=None, parent=None, source=None):
        super().__init__(parent)
        self.items, self.labels = deepcopy(items), dict(labels)
        self.source = deepcopy(source) if source is not None else None
        self.th_keep, self.th_delete = th_keep, th_delete
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()

    def run(self):
        try:
            if self._stopped:
                raise library_source.CollectionCancelled("重排已取消")
            cfg = self.source if self.source is not None else ToolkitConfig.load()
            result = score_snapshot(self.items, cfg, self.labels, th_keep=self.th_keep, th_delete=self.th_delete)
            if self._stopped:
                raise library_source.CollectionCancelled("重排已取消")
            self.finished.emit(result)
        except library_source.CollectionCancelled as exc:
            self.finished.emit({"error": str(exc), "cancelled": True, "status": "cancelled"})
        except Exception as exc:
            self.finished.emit({"error": str(exc), "status": "failed"})
