"""Task completion restores local widget state, including interactive table headers."""
from threading import Event

import pytest

from PySide6.QtCore import QPoint, QThread, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QFrame, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui.pages.review import NumItem, ROLE_ITEM_ID
from ui.services import TaskManager


class GatedWorker(QThread):
    finished = Signal(dict)

    def __init__(self):
        super().__init__()
        self.gate = Event()

    def run(self):
        self.gate.wait(3)
        self.finished.emit({'ok': True})

    def stop(self):
        self.gate.set()


def finish_worker(manager, worker, qtbot, expected_status='succeeded'):
    worker.gate.set()
    qtbot.waitUntil(lambda: not manager.running(), timeout=3000)
    assert manager.records[-1]['status'] == expected_status
    assert not worker.isRunning()


@pytest.mark.parametrize('cancelled', [False, True], ids=['completed', 'cancelled'])
def test_finished_worker_restores_mouse_header_sort_resize_and_reorder(qapp, qtbot, cancelled):
    owner = QWidget()
    qtbot.addWidget(owner)
    layout = QVBoxLayout(owner)
    table = QTableWidget(2, 3)
    table.setHorizontalHeaderLabels(['Identity', 'Name', 'Score'])
    table.setColumnWidth(0, 120)
    layout.addWidget(table)
    for row, (identity, score) in enumerate((('low', 9), ('high', 100))):
        first = QTableWidgetItem(identity)
        first.setData(ROLE_ITEM_ID, identity)
        table.setItem(row, 0, first)
        table.setItem(row, 1, QTableWidgetItem(identity))
        value = NumItem(f'{score}%')
        value.setData(Qt.UserRole, score)
        table.setItem(row, 2, value)
    table.setSortingEnabled(True)
    header = table.horizontalHeader()
    header.setSectionsMovable(True)
    owner.resize(700, 340)
    owner.show()
    qapp.processEvents()
    manager, worker = TaskManager(), GatedWorker()
    try:
        assert table.isEnabled() and header.isEnabled()
        assert manager.submit(worker, owner, 'header lifecycle fixture')
        assert not table.isEnabled()
        if cancelled:
            manager.stop(next(iter(manager._active)))
        finish_worker(manager, worker, qtbot, 'cancelled' if cancelled else 'succeeded')
        assert table.isEnabled()
        assert header.isEnabled(), 'Header inherited task-disabled state from its table'
        score_center = QPoint(header.sectionViewportPosition(2) + header.sectionSize(2) // 2,
                              header.height() // 2)
        orders = []
        for _ in range(2):
            QTest.mouseClick(header.viewport(), Qt.LeftButton, Qt.NoModifier, score_center)
            qapp.processEvents()
            assert header.sortIndicatorSection() == 2
            values = [table.item(row, 2).data(Qt.UserRole) for row in range(2)]
            expected = [9, 100] if header.sortIndicatorOrder() == Qt.AscendingOrder else [100, 9]
            assert values == expected
            orders.append([table.item(row, 0).data(ROLE_ITEM_ID) for row in range(2)])
        assert orders[0] != orders[1]
        initial = header.sectionSize(0)
        boundary = QPoint(header.sectionViewportPosition(0) + initial - 1, header.height() // 2)
        target = boundary + QPoint(55, 0)
        QTest.mousePress(header.viewport(), Qt.LeftButton, Qt.NoModifier, boundary)
        QTest.mouseMove(header.viewport(), target, 30)
        QTest.mouseRelease(header.viewport(), Qt.LeftButton, Qt.NoModifier, target)
        qapp.processEvents()
        assert header.sectionSize(0) > initial + 30
        original_index = header.visualIndex(0)
        origin = QPoint(header.sectionViewportPosition(0) + header.sectionSize(0) // 2,
                        header.height() // 2)
        destination = QPoint(header.sectionViewportPosition(2) + header.sectionSize(2) // 2,
                             header.height() // 2)
        QTest.mousePress(header.viewport(), Qt.LeftButton, Qt.NoModifier, origin)
        QTest.mouseMove(header.viewport(), origin + QPoint(15, 0), 30)
        QTest.mouseMove(header.viewport(), destination, 30)
        QTest.mouseRelease(header.viewport(), Qt.LeftButton, Qt.NoModifier, destination)
        qapp.processEvents()
        assert header.visualIndex(0) != original_index
        assert {table.item(row, 0).data(ROLE_ITEM_ID): table.item(row, 2).data(Qt.UserRole)
                for row in range(2)} == {'low': 9, 'high': 100}
    finally:
        worker.stop()
        worker.wait(3000)
        owner.close()


def test_task_preserves_intentionally_disabled_header(qapp, qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    table = QTableWidget(owner)
    header = table.horizontalHeader()
    header.setEnabled(False)
    manager, worker = TaskManager(), GatedWorker()
    try:
        assert manager.submit(worker, owner, 'disabled header fixture')
        finish_worker(manager, worker, qtbot)
        assert table.isEnabled()
        assert not header.isEnabled()
        assert header.testAttribute(Qt.WA_ForceDisabled)
    finally:
        worker.stop()
        worker.wait(3000)


def test_task_preserves_local_states_under_disabled_ancestor(qapp, qtbot):
    owner = QWidget()
    qtbot.addWidget(owner)
    ancestor = QFrame(owner)
    table = QTableWidget(ancestor)
    inherited_button = QPushButton('inherited enabled', ancestor)
    explicit_button = QPushButton('explicitly disabled', ancestor)
    explicit_button.setEnabled(False)
    ancestor.setEnabled(False)
    manager, worker = TaskManager(), GatedWorker()
    try:
        assert manager.submit(worker, owner, 'disabled ancestor fixture')
        finish_worker(manager, worker, qtbot)
        assert not ancestor.isEnabled()
        assert not table.isEnabled() and not inherited_button.isEnabled()
        ancestor.setEnabled(True)
        assert table.isEnabled(), 'Inherited ancestor disabled state became a local override'
        assert table.horizontalHeader().isEnabled()
        assert inherited_button.isEnabled()
        assert not explicit_button.isEnabled()
    finally:
        worker.stop()
        worker.wait(3000)
