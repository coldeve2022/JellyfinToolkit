"""自动化工作台后台线程。"""

from __future__ import annotations
from copy import deepcopy

from PySide6.QtCore import QThread, Signal

from config import ToolkitConfig
from automation.orchestrator import run_orchestrator


class AutomationWorker(QThread):
    """在后台运行整体自动化编排器，避免阻塞 UI。"""

    log = Signal(str)
    progress = Signal(int)
    report_ready = Signal(dict)
    error = Signal(str)
    finished = Signal()

    def __init__(self, cfg: ToolkitConfig, options: dict | None = None, parent=None):
        super().__init__(parent)
        self._cfg = deepcopy(cfg)
        self._options = deepcopy(options or {})
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        try:
            report = run_orchestrator(
                self._cfg,
                self._options,
                log=self.log.emit,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
            )
            self.report_ready.emit(report)
            if report.get("status") == "failed":
                self.error.emit(report.get("error") or "自动化失败")
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.finished.emit()
