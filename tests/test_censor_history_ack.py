"""历史读取的 ack 握手必须有超时兜底。

`HistoryLoadWorker` 是"发一页 → 阻塞等界面确认"的生产者-消费者结构，ack 由页面
在满足条件时发出。一旦页面因视图切换/重扫/异常路径而不发 ack，这里会**无限等待**，
并且一直占着 `TaskManager` 里本页任务的槽位 —— 之后这个页面点扫描只会排队不启动，
用户看到的是"点了没反应"。

实测对应：全量测试里 `test_gui_smoke::test_censorship_page_classifies` 偶发
30 秒超时（单独跑必过）。
"""

from __future__ import annotations

import time

from censor.store import VerdictStore
from ui import censor_history


def _seeded_store(tmp_path, rows=6):
    store = VerdictStore(tmp_path / "results.db")
    with store._connect() as conn:
        conn.executemany(
            "INSERT INTO censor_probe(path, status) VALUES (?, 'completed')",
            [(f"p{index}.mp4",) for index in range(rows)],
        )
    return store


def test_worker_finishes_when_ui_never_acknowledges(qapp, tmp_path, monkeypatch):
    """界面不发 ack 时，worker 也必须自己收尾，不能永久挂着。"""
    monkeypatch.setattr(censor_history, "ACK_TIMEOUT_SECONDS", 0.2)
    store = _seeded_store(tmp_path)
    worker = censor_history.HistoryLoadWorker(store, page_size=2)
    # 故意不连 page_ready → 永远不会 acknowledge()
    worker.start()
    try:
        assert worker.wait(10000), "worker 不该无限等待界面确认"
        assert worker.ack_timeouts >= 1, "超时次数应被记录，便于诊断"
    finally:
        worker.stop()
        worker.wait(3000)


def test_worker_proceeds_without_timeout_when_ack_arrives(qapp, tmp_path, qtbot):
    """正常路径不应触发超时（5 秒阈值远大于真实渲染时间）。"""
    store = _seeded_store(tmp_path)
    worker = censor_history.HistoryLoadWorker(store, page_size=2)
    worker.page_ready.connect(lambda _page: worker.acknowledge())
    worker.start()
    try:
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=15000)
        assert worker.ack_timeouts == 0
    finally:
        worker.stop()
        worker.wait(3000)


def test_page_ready_still_emitted_for_every_page(qapp, tmp_path, monkeypatch):
    """加了超时后，页数不能少发 —— 否则界面会缺记录。"""
    monkeypatch.setattr(censor_history, "ACK_TIMEOUT_SECONDS", 0.2)
    store = _seeded_store(tmp_path, rows=6)
    worker = censor_history.HistoryLoadWorker(store, page_size=2)
    pages = []
    worker.page_ready.connect(lambda page: pages.append(page))
    worker.start()
    deadline = time.monotonic() + 15
    while worker.isRunning() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    worker.wait(3000)
    qapp.processEvents()
    assert worker.ack_timeouts >= 1, "前提：本用例确实走了超时分支"
    assert len(pages) == 3, f"6 行 / 每页 2 行应为 3 页，实际 {len(pages)}"
