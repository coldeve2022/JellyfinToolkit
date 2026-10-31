"""任务条目必须能自愈：worker 消失了就不能继续占着全局写锁。

真实现场：某个页面的 worker 跟着页面一起被销毁，`finished` 永远不会到，
条目就卡在 `TaskManager._active` 里。而 `submit()` 判断"是否还有写任务在跑"
是**全局**的 —— 于是别的页面点"开始/删除/合并"只会弹一句"等待当前任务结束"，
用户看到的是"所有按钮都点不动了"。

测试里对应的是那条"单跑必过、全量跑必挂"的用例：上一个用例留下的僵尸条目
让下一个用例的 `start_task` 失败 → 弹模态框 → 事件循环卡住。
"""

from __future__ import annotations

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QWidget
from shiboken6 import delete as shiboken_delete

from ui.services import TaskManager


def _zombie_entry(owner, *, mutating=True):
    thread = QThread()
    shiboken_delete(thread)  # 模拟"随页面一起销毁"
    return {
        "worker": thread,
        "owner": owner,
        "record": {
            "id": "zombie",
            "title": "僵尸任务",
            "status": "running",
            "mutating": mutating,
            "progress": 0,
            "detail": "",
            "logs": [],
        },
        "controls": {},
        "buttons": {},
        "resources": set(),
        "stopping": False,
    }


def test_destroyed_worker_is_released_by_poll(qapp):
    manager = TaskManager.instance()
    page = QWidget()
    entry = _zombie_entry(page)
    manager._active["zombie"] = entry
    try:
        manager._poll()
        assert "zombie" not in manager._active, "已销毁的 worker 必须被清理"
        assert entry["record"]["status"] == "interrupted"
        assert not manager.running()
    finally:
        manager._active.clear()
        page.deleteLater()


def test_zombie_entry_does_not_block_mutating_submit(qapp):
    """清理之后，"是否有写任务在跑"这个前置条件必须变回"没有"。

    `submit()` 拒绝写任务的判据是：
      `self.running(owner) or any(mutating and entry.mutating for entry in _active)`
    僵尸条目会让第二个条件永远成立。这里直接断言那份判据，而不真的提交一个
    QThread —— 用例里启动后立刻销毁线程会让进程在退出阶段崩掉，
    测的就不是这条逻辑了。
    """
    manager = TaskManager.instance()
    blocked_page = QWidget()
    manager._active["zombie"] = _zombie_entry(blocked_page)
    try:
        before = any(
            entry["record"]["mutating"] for entry in manager._active.values()
        )
        assert before, "前提：僵尸条目确实占着写锁"

        manager._poll()

        after = any(
            entry["record"]["mutating"] for entry in manager._active.values()
        )
        assert not after, "僵尸条目清理后，写锁必须释放"
        assert not manager.running(blocked_page)
    finally:
        manager._active.clear()
        blocked_page.deleteLater()
