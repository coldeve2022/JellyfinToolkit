"""Late Qt events must not inspect already-cleared Python owner references."""
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QWidget, QTableWidget

from ui.workspace_preferences import WorkspacePreferences


@pytest.mark.parametrize('kind', [QEvent.Hide, QEvent.Close])
def test_late_owner_event_is_ignored_but_live_preferences_still_save(qapp, qtbot, monkeypatch, kind):
    owner = QWidget()
    qtbot.addWidget(owner)
    table = QTableWidget(0, 2, owner)
    preferences = WorkspacePreferences(owner, 'synthetic-lifetime', table)
    with monkeypatch.context() as cleared:
        cleared.delattr(preferences, 'owner')
        assert preferences.eventFilter(owner, QEvent(kind)) is False
    saved = []
    monkeypatch.setattr(preferences, 'save', lambda: saved.append(True))
    assert preferences.eventFilter(owner, QEvent(kind)) is False
    assert saved == [True]
