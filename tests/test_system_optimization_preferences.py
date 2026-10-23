from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter, QTableWidget, QVBoxLayout, QWidget


def test_workspace_preferences_restore_columns_splitter_and_reset(qapp, qtbot):
    from ui.workspace_preferences import WorkspacePreferences
    def build():
        owner = QWidget()
        qtbot.addWidget(owner)
        layout = QVBoxLayout(owner)
        splitter = QSplitter()
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Name", "Status", "Count"])
        table.setColumnWidth(0, 180)
        splitter.addWidget(table)
        splitter.addWidget(QWidget())
        layout.addWidget(splitter)
        pref = WorkspacePreferences(owner, "synthetic", table, splitter=splitter)
        return owner, table, splitter, pref
    owner, table, splitter, pref = build()
    table.setColumnHidden(2, True)
    table.horizontalHeader().moveSection(0, 1)
    table.setColumnWidth(0, 310)
    splitter.setOrientation(Qt.Vertical)
    pref.state["font_size"] = 14
    pref.state["auto_next"] = False
    pref.save()
    _, other, second, restored = build()
    assert other.isColumnHidden(2)
    assert other.horizontalHeader().visualIndex(0) == 1
    assert other.columnWidth(0) == 310
    assert second.orientation() == Qt.Vertical
    assert other.font().pointSize() == 14
    assert restored.state["auto_next"] is False
    restored.reset()
    assert not other.isColumnHidden(2)
    assert other.horizontalHeader().visualIndex(0) == 0


def test_gallery_reflows_at_narrow_width(qapp, qtbot):
    from ui.censor_gallery import EvidenceGallery
    from censor.models import FrameCandidate
    gallery = EvidenceGallery()
    qtbot.addWidget(gallery)
    gallery.resize(225, 400)
    gallery.show_candidates([FrameCandidate(timestamp=float(i), score=0) for i in range(4)])
    gallery.show()
    qapp.processEvents()
    assert gallery.grid.columnCount() == 1 or all(
        gallery.grid.itemAtPosition(row, 1) is None for row in range(gallery.grid.rowCount()))


def test_auto_next_preference_keeps_current_item(qapp, qtbot, tmp_path):
    from ui.pages.censorship import CensorshipPage
    from config import ToolkitConfig
    from censor import CensorState, CensorOptions
    page = CensorshipPage(ToolkitConfig(censor_visual_enabled=False, release_auto_lookup=False))
    qtbot.addWidget(page)
    for number in ("ABC-123", "DEF-456"):
        path = tmp_path / (number + ".mp4")
        path.write_bytes(b"synthetic")
        page._on_item(page._service.probe(path, CensorOptions(detect_visual=False)))
    page.table.selectRow(0)
    page._on_row_selected()
    current = page._current.path
    page.workspace.state["auto_next"] = False
    page._label(CensorState.UNCENSORED)
    assert page._current.path == current
