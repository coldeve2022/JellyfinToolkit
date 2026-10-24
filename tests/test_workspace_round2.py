"""Presentation recovery and image viewing with synthetic, local fixtures only."""
import base64
import json

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QDialog, QLineEdit, QPushButton, QSplitter, QTableWidget, QVBoxLayout, QWidget,
)

from censor.models import FrameCandidate
from ui.censor_gallery import EvidenceGallery, EvidenceImage, _EvidenceDialog
from ui.workspace_preferences import WorkspacePreferences


def _preferences(qtbot, saved):
    import config

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "workspace_preferences.json").write_text(
        json.dumps({"round2": saved}), encoding="utf-8")
    owner = QWidget()
    qtbot.addWidget(owner)
    layout = QVBoxLayout(owner)
    splitter = QSplitter()
    table = QTableWidget(0, 3)
    table.setHorizontalHeaderLabels(["Identity", "Status", "Count"])
    splitter.addWidget(table)
    gallery = EvidenceGallery()
    splitter.addWidget(gallery)
    layout.addWidget(splitter)
    search = QLineEdit()
    layout.addWidget(search)
    owner.btn_next = QPushButton("Next", owner)
    layout.addWidget(owner.btn_next)
    pref = WorkspacePreferences(owner, "round2", table, splitter=splitter,
                                gallery=gallery, filters={"search": search})
    return owner, table, splitter, gallery, search, pref


@pytest.mark.parametrize("field", ["header", "splitter"])
@pytest.mark.parametrize("broken", ["a", [1], {"invalid": True}])
def test_bad_layout_state_does_not_skip_other_preferences(qapp, qtbot, field, broken):
    owner, table, _, gallery, search, pref = _preferences(qtbot, {
        field: broken, "font_size": 17, "row_height": 43, "tile_width": 240,
        "columns": 3, "filters": {"search": "synthetic"},
        "shortcuts": {"next": "Ctrl+N"},
    })
    assert owner.font().pointSize() == 17
    assert table.verticalHeader().defaultSectionSize() == 43
    assert gallery._tile_width == 240 and gallery._columns == 3
    assert search.text() == "synthetic"
    assert owner.btn_next.shortcut().toString() == "Ctrl+N"
    assert not pref._applying


@pytest.mark.parametrize("broken", [None, [], {"next": None}])
def test_bad_shortcuts_are_normalized_before_customize(qapp, qtbot, monkeypatch, broken):
    owner, _, _, _, _, pref = _preferences(qtbot, {"shortcuts": broken})
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Rejected)
    pref.customize()
    assert owner.btn_next.shortcut().toString() == "Alt+3"
    assert pref.state["shortcuts"]["next"] == "Alt+3"
    pref.save()
    persisted = json.loads(pref.path.read_text(encoding="utf-8"))
    assert persisted["round2"]["shortcuts"]["next"] == "Alt+3"


def test_saved_hidden_identity_column_is_restored_visible(qapp, qtbot):
    source = QTableWidget(0, 3)
    qtbot.addWidget(source)
    source.hideColumn(0)
    source.hideColumn(2)
    state = base64.b64encode(bytes(source.horizontalHeader().saveState())).decode()
    _, table, _, _, _, pref = _preferences(qtbot, {"header": state})
    assert not table.isColumnHidden(0)
    assert table.isColumnHidden(2)
    menu = pref.menu()
    column_action = menu.actions()[0]
    columns = column_action.menu()
    action = columns.actions()[0]
    assert action.isChecked() and not action.isEnabled()


@pytest.fixture
def synthetic_image(tmp_path, qapp):
    path = tmp_path / "solid.png"
    pixmap = QPixmap(1600, 800)
    pixmap.fill(QColor("#4268b0"))
    assert pixmap.save(str(path), "PNG")
    return str(path)


@pytest.mark.parametrize("replacement", ["missing.png", "broken.png"])
def test_replacing_image_with_unreadable_file_clears_previous_pixels(
        qapp, qtbot, tmp_path, synthetic_image, replacement):
    label = EvidenceImage("Unavailable")
    qtbot.addWidget(label)
    EvidenceGallery._set_image(label, synthetic_image)
    assert not label.pixmap().isNull()
    path = tmp_path / replacement
    if replacement == "broken.png":
        path.write_bytes(b"not an image")
    EvidenceGallery._set_image(label, str(path))
    assert label.pixmap().isNull()
    assert label.text()


def test_expanded_images_rerender_after_resize(qapp, qtbot, synthetic_image):
    candidate = FrameCandidate(timestamp=0, score=0, thumb_path=synthetic_image,
                               crop_path=synthetic_image)
    dialog = _EvidenceDialog(None, candidate, 0, [candidate])
    qtbot.addWidget(dialog)
    dialog.show()
    qapp.processEvents()
    before = dialog.frame.pixmap().size()
    dialog.resize(640, 360)
    qapp.processEvents()
    after = dialog.frame.pixmap().size()
    assert after != before
    assert after.width() <= dialog.frame.width()
    assert after.height() <= dialog.frame.height()
    assert abs(after.width() / after.height() - 2) < .03


def test_expanded_dialog_fits_small_available_screen(qapp, qtbot, monkeypatch,
                                                   synthetic_image):
    class SmallScreen:
        def availableGeometry(self):
            return QRect(0, 0, 640, 360)

    monkeypatch.setattr(_EvidenceDialog, "screen", lambda self: SmallScreen())
    candidate = FrameCandidate(timestamp=0, score=0, thumb_path=synthetic_image)
    dialog = _EvidenceDialog(None, candidate, 0, [candidate])
    qtbot.addWidget(dialog)
    dialog.show()
    qapp.processEvents()
    assert dialog.width() <= 640
    assert dialog.height() <= 360
    assert dialog.frame.geometry().right() < dialog.width()
    assert dialog.crop.geometry().right() < dialog.width()


def test_expanded_navigation_clears_missing_image_and_keeps_close_gesture(
        qapp, qtbot, synthetic_image):
    candidates = [
        FrameCandidate(timestamp=0, score=0, thumb_path=synthetic_image),
        FrameCandidate(timestamp=1, score=0),
    ]
    dialog = _EvidenceDialog(None, candidates[0], 0, candidates)
    qtbot.addWidget(dialog)
    dialog.show()
    qapp.processEvents()
    qtbot.keyClick(dialog, Qt.Key_Right)
    dialog.resize(600, 340)
    qapp.processEvents()
    assert dialog.frame.pixmap().isNull()
    assert dialog.frame.text()
    qtbot.keyClick(dialog, Qt.Key_Left)
    assert not dialog.frame.pixmap().isNull()
    qtbot.mouseDClick(dialog.frame, Qt.LeftButton)
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.Accepted


def test_narrow_gallery_shrinks_tiles_without_forgetting_preferred_width(
        qapp, qtbot, synthetic_image):
    gallery = EvidenceGallery()
    qtbot.addWidget(gallery)
    gallery.set_preferences(400, 2)
    gallery.resize(175, 320)
    gallery.show_candidates([
        FrameCandidate(timestamp=float(i), score=0, thumb_path=synthetic_image)
        for i in range(4)])
    gallery.show()
    qapp.processEvents()
    assert gallery.labels[0].width() < 175
    assert gallery.body.width() <= gallery.viewport().width()
    assert gallery.labels[0].mapTo(gallery.viewport(), gallery.labels[0].rect().topRight()).x() < gallery.viewport().width()
    gallery.resize(950, 500)
    qapp.processEvents()
    assert gallery.labels[0].width() == 400
    assert gallery.grid.itemAtPosition(0, 1) is not None
    assert gallery._columns == 2
