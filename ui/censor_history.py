"""Read all history in the background, waiting for each batch to render."""
from threading import Event

from PySide6.QtCore import QThread, Signal


class HistoryLoadWorker(QThread):
    page_ready = Signal(object)
    result = Signal(object)
    error = Signal(str)

    def __init__(self, store, page_size=500, parent=None):
        super().__init__(parent)
        self.store = store
        self.page_size = page_size
        self._stopped = Event()
        self._rendered = Event()

    def stop(self):
        self._stopped.set()
        self._rendered.set()

    def acknowledge(self):
        self._rendered.set()

    def run(self):
        summary = dict(read=0, loaded=0, total=0, cancelled=False, error='')
        pages = self.store.iter_history_pages(self.page_size)
        try:
            for page in pages:
                if self._stopped.is_set():
                    break
                self._rendered.clear()
                summary.update(read=page['next_offset'], total=page['total'])
                summary['loaded'] += len(page['records'])
                self.page_ready.emit(page)
                while not self._stopped.is_set() and not self._rendered.wait(.05):
                    pass
                if self._stopped.is_set():
                    break
        except Exception as exc:
            summary['error'] = str(exc)
            self.error.emit(str(exc))
        finally:
            pages.close()
            summary['cancelled'] = self._stopped.is_set()
            self.result.emit(summary)
