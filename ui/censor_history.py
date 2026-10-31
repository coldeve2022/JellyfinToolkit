"""Read all history in the background, waiting for each batch to render."""
import time
from threading import Event

from PySide6.QtCore import QThread, Signal

#: 等界面确认"这一页已经渲染完"的最长时间（秒）。
#:
#: 为什么需要它：这个 worker 是"发一页 → 阻塞等 ack"的生产者-消费者协议，
#: 而 ack 由页面在满足条件时发出（`_history_loading` 为真且 `_pending_results` 为空）。
#: 只要页面因为视图切换、重扫、异常路径而永远不发 ack，这里就会**无限等待**。
#: 更糟的是它会一直占着 `TaskManager` 里"本页任务"的槽位 —— 之后这个页面
#: 再点扫描/导入，任务只会排队不启动，用户看到的就是"点了没反应"。
#: 实测症状：全量测试里 `test_censorship_page_classifies` 偶发 30 秒超时。
ACK_TIMEOUT_SECONDS = 5.0


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
        #: 有过多少次"界面没在超时内确认"，用于诊断；正常流程应恒为 0
        self.ack_timeouts = 0

    def stop(self):
        self._stopped.set()
        self._rendered.set()

    def acknowledge(self):
        self._rendered.set()

    def run(self):
        summary = dict(read=0, loaded=0, total=0, cancelled=False, error='',
                       ack_timeouts=0)
        pages = self.store.iter_history_pages(self.page_size)
        try:
            for page in pages:
                if self._stopped.is_set():
                    break
                self._rendered.clear()
                summary.update(read=page['next_offset'], total=page['total'])
                summary['loaded'] += len(page['records'])
                self.page_ready.emit(page)
                deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
                while not self._stopped.is_set() and not self._rendered.wait(.05):
                    if time.monotonic() >= deadline:
                        # 界面迟迟不确认：不再等。宁可少一次"等渲染"的节流，
                        # 也不能把整个页面的任务队列堵死。
                        self.ack_timeouts += 1
                        summary['ack_timeouts'] = self.ack_timeouts
                        break
                if self._stopped.is_set():
                    break
        except Exception as exc:
            summary['error'] = str(exc)
            self.error.emit(str(exc))
        finally:
            pages.close()
            summary['cancelled'] = self._stopped.is_set()
            self.result.emit(summary)
