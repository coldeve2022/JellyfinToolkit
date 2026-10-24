"""Task cleanup tolerates a stopped worker destroyed with its owning page."""

import pytest
from PySide6.QtCore import QObject, QThread
from shiboken6 import delete, isValid

from ui.services import TaskManager


@pytest.mark.parametrize(('stats', 'stopped', 'expected'), [
    ({'ok': True}, False, 'succeeded'),
    ({'error': 'real task failure'}, False, 'failed'),
    ({'cancelled': True}, False, 'cancelled'),
    ({'ok': True}, True, 'cancelled'),
])
def test_poll_finalizes_destroyed_parent_owned_worker(qapp, monkeypatch, stats, stopped, expected):
    class Worker(QThread):
        def run(self):
            pass

    owner = QObject()
    worker = Worker(owner)
    worker.stats = stats
    worker._stopped = stopped
    worker.setProperty('taskAutoDelete', True)
    manager = TaskManager()
    manager._timer.stop()
    saved = []
    monkeypatch.setattr(manager, '_save_history', lambda: saved.append(True))
    try:
        assert manager.submit(worker, owner, 'worker lifetime fixture')
        assert worker.wait(3000)
        task_id = next(iter(manager._active))
        # Isolate finalization after the queued result-delivery barrier.
        manager._active[task_id]['delivery_barrier_passed'] = True
        delete(owner)
        assert not isValid(worker)
        manager._poll()
        assert not manager._active
        record = manager.records[-1]
        assert record['status'] == expected
        assert record['ended']
        assert saved == [True]
        if expected == 'failed':
            assert record['error'] == 'real task failure'
            assert record['progress'] == 0
        if expected == 'succeeded':
            assert record['progress'] == 100
    finally:
        if isValid(owner):
            worker.wait(3000)
            delete(owner)
        delete(manager)
