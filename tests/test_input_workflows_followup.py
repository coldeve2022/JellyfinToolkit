"""Real Qt input dispatch and end-to-end file queue regression tests."""

from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication


def drop_urls(widget, paths, actions=Qt.CopyAction):
    """Dispatch at the actual viewport; calling the Python handler hides Qt flags."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    surface = widget.viewport() if hasattr(widget, "viewport") else widget
    enter = QDragEnterEvent(QPoint(12, 12), actions, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(surface, enter)
    event = QDropEvent(QPointF(12, 12), actions, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(surface, event)
    return enter.isAccepted(), event.isAccepted()


def test_viewport_drop_reaches_canonical_queue(qapp, qtbot, tmp_path):
    from ui.widgets import DragDropListWidget

    media = tmp_path / "movie.mp4"
    media.write_bytes(b"fixture")
    widget = DragDropListWidget()
    widget.files_dropped.connect(
        lambda paths: [widget.add_item(Path(path).name, path) for path in paths]
    )
    widget.show()
    qapp.processEvents()
    enter, drop = drop_urls(widget, [media])
    assert enter and drop, "Qt viewport must accept the OS URL drag before a handler can run"
    qtbot.waitUntil(lambda: widget.get_all_paths() == [str(media)], timeout=2000)


def test_disabled_viewport_does_not_receive_dropped_input(qapp, qtbot, tmp_path):
    from ui.widgets import DragDropListWidget

    media = tmp_path / "movie.mp4"
    media.write_bytes(b"fixture")
    widget = DragDropListWidget()
    received = []
    widget.files_dropped.connect(received.extend)
    widget.show()
    widget.setEnabled(False)
    qapp.processEvents()
    enter, drop = drop_urls(widget, [media])
    qapp.processEvents()
    assert not enter and not drop
    assert received == []


@pytest.fixture
def cfg(monkeypatch):
    from config import ToolkitConfig
    from utils import library_source, lada_tool, whisper_tool, tools
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(library_source, "describe", lambda cfg: "Synthetic input library")
    monkeypatch.setattr(library_source, "resolve", lambda *a, **k: ("db", {"path": ""}))
    monkeypatch.setattr(tools, "resolve_tool", lambda *a, **k: "")
    monkeypatch.setattr(lada_tool, "find_lada_cli", lambda *a, **k: None)
    monkeypatch.setattr(whisper_tool, "find_infer_exe", lambda *a, **k: None)
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    value = ToolkitConfig()
    value.save = lambda: None
    return value


PAGES = [
    ("replace", "ReplacePage", "list_restored", "restored"),
    ("replace", "ReplacePage", "list_library", "library"),
    ("repair", "RepairPage", "file_list", "main"),
    ("merge", "MergePage", "file_list", "main"),
    ("subtitle", "SubtitlePage", "dir_list", "main"),
    ("subtitle_gen", "SubtitleGenPage", "list_files", "main"),
    ("lada", "LadaPage", "list_files", "main"),
    ("censorship", "CensorshipPage", "table_censored", "main"),
]


def make_page(module, name, cfg):
    from importlib import import_module

    return getattr(import_module("ui.pages." + module), name)(cfg)


def queue_paths(page, view):
    widget = getattr(page, view)
    if hasattr(widget, "gather_all_paths"):
        return sorted(
            page.table_censored.gather_all_paths() | page.table_uncensored.gather_all_paths()
        )
    return widget.get_all_paths()


@pytest.mark.parametrize("module,name,view,role", PAGES)
def test_visible_file_picker_to_queue(
    qapp, qtbot, cfg, tmp_path, monkeypatch, module, name, view, role
):
    from PySide6.QtWidgets import QFileDialog, QPushButton
    from ui.services import busy

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"synthetic video")
    page = make_page(module, name, cfg)
    page.resize(900, 600)
    page.show()
    qapp.processEvents()
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(video)], ""))
    button = page.findChild(QPushButton, "input-files-" + role)
    assert button is not None, "File selection must be an explicit visible action"
    assert button.isVisible()
    button.click()
    qtbot.waitUntil(lambda: str(video) in queue_paths(page, view), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)


@pytest.mark.parametrize("module,name,view,role", PAGES)
def test_folder_picker_and_viewport_drop_share_queue(
    qapp, qtbot, cfg, tmp_path, monkeypatch, module, name, view, role
):
    from PySide6.QtWidgets import QFileDialog, QPushButton
    from ui.services import busy

    first = tmp_path / "first"
    first.mkdir()
    one = first / "one.mp4"
    one.write_bytes(b"first video")
    second = tmp_path / "second"
    second.mkdir()
    two = second / "two.mp4"
    two.write_bytes(b"second video")
    page = make_page(module, name, cfg)
    page.resize(900, 600)
    page.show()
    qapp.processEvents()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(first))
    button = page.findChild(QPushButton, "input-folders-" + role)
    assert button is not None, "Folder selection must not depend on dragging"
    button.click()
    expected_first = str(first) if module in ("replace", "merge", "subtitle") else str(one)
    qtbot.waitUntil(lambda: expected_first in queue_paths(page, view), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    enter, dropped = drop_urls(getattr(page, view), [second])
    assert enter and dropped
    expected_second = str(second) if module in ("replace", "merge", "subtitle") else str(two)
    qtbot.waitUntil(lambda: expected_second in queue_paths(page, view), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert expected_first in queue_paths(page, view)


def test_busy_page_background_rejects_drag(qapp, qtbot, cfg, tmp_path):
    from threading import Event
    from ui.pages.repair import RepairPage
    from ui.services import CallWorker, TaskManager, busy

    page = RepairPage(cfg)
    page.show()
    qapp.processEvents()
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    gate = Event()
    worker = CallWorker(lambda cancel: gate.wait(3))
    manager = TaskManager.instance()
    assert manager.submit(worker, page, "controlled busy task")
    try:
        enter, dropped = drop_urls(page, [video])
        assert not enter and not dropped
        assert page.file_list.count() == 0
    finally:
        gate.set()
        qtbot.waitUntil(lambda: not busy(page), timeout=4000)


def test_existing_long_path_is_not_silently_discarded(qapp, qtbot, tmp_path):
    from ui.widgets import DragDropListWidget

    folder = tmp_path
    while len(str(folder / "video.mp4")) < 245:
        folder = folder / ("directory-" + "x" * 18)
    folder.mkdir(parents=True)
    video = folder / "video.mp4"
    video.write_bytes(b"fixture")
    widget = DragDropListWidget()
    received = []
    widget.files_dropped.connect(received.extend)
    widget.show()
    qapp.processEvents()
    enter, dropped = drop_urls(widget, [video])
    assert enter and dropped
    qtbot.waitUntil(lambda: received == [str(video)], timeout=2000)


def test_nfo_folder_picker_and_drop_reach_readonly_preview(qapp, qtbot, cfg, tmp_path, monkeypatch):
    from ui.pages.nfo_fix import NFOPage
    from PySide6.QtWidgets import QPushButton, QFileDialog
    from ui.services import busy

    first = tmp_path / "one"
    first.mkdir()
    (first / "movie.ts.nfo").write_text("<movie><title>movie.ts</title></movie>")
    second = tmp_path / "two"
    second.mkdir()
    (second / "second.ts.nfo").write_text("<movie><title>second.ts</title></movie>")
    page = NFOPage(cfg)
    page.show()
    qapp.processEvents()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(first))
    button = page.findChild(QPushButton, "input-folders-main")
    assert button is not None
    button.click()
    assert page._dir == str(first)
    enter, dropped = drop_urls(page, [second])
    assert enter and dropped
    qtbot.waitUntil(lambda: page._dir == str(second), timeout=2000)
    page.btn_preview.click()
    qtbot.waitUntil(lambda: bool(page._plan), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert any(entry["source"] == str(second / "second.ts.nfo") for entry in page._plan)
    assert (second / "second.ts.nfo").exists(), "Preview must not rename a selected file"


def test_replace_folder_drop_reaches_same_pairing_preview(qapp, qtbot, cfg, tmp_path):
    from ui.pages.replace import ReplacePage
    from ui.services import busy

    restored = tmp_path / "restored"
    restored.mkdir()
    source = restored / "Movie.restored.mp4"
    source.write_bytes(b"new fixture")
    library = tmp_path / "library"
    library.mkdir()
    target = library / "Movie.mp4"
    target.write_bytes(b"old fixture")
    page = ReplacePage(cfg)
    page.show()
    qapp.processEvents()
    assert all(drop_urls(page.list_restored, [restored]))
    qtbot.waitUntil(lambda: page.list_restored.count() == 1)
    assert all(drop_urls(page.list_library, [library]))
    qtbot.waitUntil(lambda: page.list_library.count() == 1)
    page.btn_preview.click()
    qtbot.waitUntil(lambda: page.pairs.rowCount() == 1, timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert page._plan[0]["source"] == str(source)
    assert page._plan[0]["target"] == str(target)
    assert page._plan[0]["status"] == "ready"
    assert page.btn_start.isEnabled()
    assert source.read_bytes() == b"new fixture"
    assert target.read_bytes() == b"old fixture"


def test_subtitle_file_picker_does_not_expand_to_sibling_files(
    qapp, qtbot, cfg, tmp_path, monkeypatch
):
    from ui.pages.subtitle import SubtitlePage
    from PySide6.QtWidgets import QPushButton, QFileDialog
    from ui.services import busy

    chosen = tmp_path / "chosen.mp4"
    chosen.write_bytes(b"video")
    sibling = tmp_path / "not-selected.mp4"
    sibling.write_bytes(b"video")
    page = SubtitlePage(cfg)
    page.show()
    qapp.processEvents()
    page.spin_size.setValue(0)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(chosen)], ""))
    button = page.findChild(QPushButton, "input-files-main")
    assert button is not None
    button.click()
    page.btn_start.click()
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert page.result_list.get_all_paths() == [str(chosen)]


def test_missing_drop_path_reports_visible_reason(qapp, tmp_path):
    from ui.widgets import DragDropListWidget

    widget = DragDropListWidget()
    widget.show()
    qapp.processEvents()
    enter, dropped = drop_urls(widget, [tmp_path / "vanished.mp4"])
    qapp.processEvents()
    assert not dropped
    assert widget.property("input_error")
    assert widget.input_feedback.isVisible()
    assert "vanished.mp4" in widget.input_feedback.text()


def test_nfo_multiple_directories_are_not_silently_truncated(qapp, qtbot, cfg, tmp_path):
    from ui.pages.nfo_fix import NFOPage

    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    page = NFOPage(cfg)
    page.show()
    qapp.processEvents()
    drop_urls(page, [first, second])
    qapp.processEvents()
    assert page._dir is None
    assert page.property("input_error")


def test_successful_drop_clears_previous_input_error(qapp, qtbot, tmp_path):
    from ui.widgets import DragDropListWidget

    widget = DragDropListWidget()
    received = []
    widget.files_dropped.connect(received.extend)
    widget.show()
    qapp.processEvents()
    drop_urls(widget, [tmp_path / "vanished.mp4"])
    qapp.processEvents()
    assert widget.property("input_error")
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    assert all(drop_urls(widget, [video]))
    qtbot.waitUntil(lambda: received == [str(video)])
    assert not widget.property("input_error")
    assert not widget.input_feedback.isVisible()


def test_move_only_drop_is_rejected_without_queue_or_file_changes(qapp, tmp_path):
    from ui.widgets import DragDropListWidget

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    widget = DragDropListWidget()
    received = []
    widget.files_dropped.connect(received.extend)
    widget.show()
    qapp.processEvents()
    assert drop_urls(widget, [video], Qt.MoveAction) == (False, False)
    qapp.processEvents()
    assert received == []
    assert video.read_bytes() == b"fixture"


@pytest.mark.parametrize("table", [False, True])
def test_outward_drag_requests_copy_action_only(qapp, tmp_path, monkeypatch, table):
    from ui import widgets

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    if table:
        widget = widgets.DragDropTableWidget(["file", "modified"])
        widget.add_file_row(video.name, "synthetic", str(video))
    else:
        widget = widgets.DragDropListWidget()
        widget.add_item(video.name, str(video))
    widget.selectAll()
    effects = []
    payload = []

    class NativeDragBoundary:
        def __init__(self, source):
            self.source = source

        def setMimeData(self, mime):
            payload.extend(url.toLocalFile() for url in mime.urls())

        def exec(self, actions):
            effects.append(actions)
            return Qt.CopyAction

    monkeypatch.setattr(widgets, "QDrag", NativeDragBoundary)
    widget.startDrag(Qt.CopyAction | Qt.MoveAction)
    assert effects == [Qt.CopyAction]
    assert [Path(path) for path in payload] == [video]
    assert video.read_bytes() == b"fixture"


def test_picker_cancel_leaves_queue_and_feedback_unchanged(qapp, cfg, monkeypatch):
    from ui.pages.replace import ReplacePage
    from PySide6.QtWidgets import QFileDialog, QPushButton

    page = ReplacePage(cfg)
    page.show()
    qapp.processEvents()
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([], ""))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
    for role in ("restored", "library"):
        page.findChild(QPushButton, "input-files-" + role).click()
        page.findChild(QPushButton, "input-folders-" + role).click()
    assert page.list_restored.count() == page.list_library.count() == 0
    for actions in page.findChildren(
        __import__("ui.widgets", fromlist=["FileInputActions"]).FileInputActions
    ):
        assert not actions.property("input_error")


def test_delete_manual_picker_and_folder_drop_keep_scope(qapp, qtbot, cfg, tmp_path, monkeypatch):
    from ui.pages.delete import DeletePage
    from PySide6.QtWidgets import QPushButton, QFileDialog, QTableWidgetItem
    from ui.services import busy

    chosen = tmp_path / "chosen.mp4"
    chosen.write_bytes(b"fixture")
    folder = tmp_path / "folder"
    folder.mkdir()
    dropped = folder / "drop.mp4"
    dropped.write_bytes(b"fixture")
    page = DeletePage(cfg)
    page.tabs.setCurrentIndex(1)
    page.show()
    qapp.processEvents()
    page._delete_rows = [{"path": "hidden-other-tab.mp4", "size": 1}]
    page.table.setRowCount(1)
    item = QTableWidgetItem("hidden")
    item.setData(Qt.UserRole, "hidden-other-tab.mp4")
    page.table.setItem(0, 0, item)
    page.table.selectRow(0)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(chosen)], ""))
    button = next(b for b in page.findChildren(QPushButton) if b.text() == "添加文件")
    button.click()
    assert page.manual_list.get_all_paths() == [str(chosen)]
    assert all(drop_urls(page.manual_list, [folder]))
    qtbot.waitUntil(lambda: str(dropped) in page.manual_list.get_all_paths(), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert {path for path, _ in page._planned_targets(False)} == {str(chosen), str(dropped)}


def test_merge_directory_drop_reaches_background_plan(qapp, qtbot, cfg, tmp_path, monkeypatch):
    from ui.pages.merge import MergePage
    from utils.merge import PartProbe
    from ui.services import busy

    first = tmp_path / "ABC-123-CD1.mp4"
    first.write_bytes(b"first synthetic part")
    second = tmp_path / "ABC-123-CD2.mp4"
    second.write_bytes(b"second synthetic part, different")
    monkeypatch.setattr("ui.pages.merge._resolve_ffprobe", lambda *a, **k: "fixture-ffprobe")
    monkeypatch.setattr(
        "utils.merge.probe_part",
        lambda executable, path, **k: PartProbe(
            ok=True,
            duration=120,
            width=1920,
            height=1080,
            fps=24,
            video_codec="h264",
            audio_codec="aac",
            sample_rate=48000,
            channels=2,
            video_stream_index=0,
            audio_stream_index=1,
            container="mp4",
            size=Path(path).stat().st_size,
        ),
    )
    page = MergePage(cfg)
    page.show()
    qapp.processEvents()
    assert all(drop_urls(page.file_list, [tmp_path]))
    qtbot.waitUntil(lambda: page.file_list.count() == 1)
    page.btn_scan.click()
    qtbot.waitUntil(lambda: page._plan is not None, timeout=6000)
    qtbot.waitUntil(lambda: not busy(page), timeout=6000)
    assert len(page._plan.groups) == 1
    assert {part.path for part in page._plan.groups[0].parts} == {str(first), str(second)}
    assert page.tree.topLevelItemCount() == 1
    assert first.exists() and second.exists()


def test_repair_folder_selection_reaches_background_analysis(
    qapp, qtbot, cfg, tmp_path, monkeypatch
):
    from ui.pages.repair import RepairPage
    from PySide6.QtWidgets import QPushButton, QFileDialog
    from ui.services import busy

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(
        "workers.ffmpeg.analyze_video",
        lambda path: {
            "is_corrupted": False,
            "is_compatible": True,
            "video_codec": "h264",
            "error": "",
            "duration": 10,
        },
    )
    page = RepairPage(cfg)
    page.show()
    qapp.processEvents()
    page.findChild(QPushButton, "input-folders-main").click()
    qtbot.waitUntil(lambda: page.file_list.get_all_paths() == [str(video)], timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    page.btn_scan.click()
    qtbot.waitUntil(lambda: str(video) in page._scan_results, timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)
    assert page.result_table.rowCount() == 1
    assert not page._scan_results[str(video)]["is_corrupted"]
    assert video.read_bytes() == b"fixture"


def test_disable_after_drop_before_delivery_discards_queued_input(qapp, tmp_path):
    from ui.widgets import DragDropListWidget

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    widget = DragDropListWidget()
    received = []
    widget.files_dropped.connect(received.extend)
    widget.show()
    qapp.processEvents()
    assert all(drop_urls(widget, [video]))
    widget.setEnabled(False)
    qapp.processEvents()
    assert received == []


@pytest.mark.parametrize("module,name,view,role", [PAGES[2], PAGES[3], PAGES[4]])
def test_long_file_path_reaches_page_queue(qapp, qtbot, cfg, tmp_path, module, name, view, role):
    from ui.services import busy

    folder = tmp_path
    while len(str(folder / "movie.mp4")) < 245:
        folder = folder / ("directory-" + "q" * 18)
    folder.mkdir(parents=True)
    video = folder / "movie.mp4"
    video.write_bytes(b"fixture")
    page = make_page(module, name, cfg)
    page.show()
    qapp.processEvents()
    assert all(drop_urls(getattr(page, view), [video]))
    qtbot.waitUntil(lambda: str(video) in queue_paths(page, view), timeout=4000)
    qtbot.waitUntil(lambda: not busy(page), timeout=4000)


def test_picker_success_clears_previous_drop_error(qapp, qtbot, cfg, tmp_path, monkeypatch):
    from ui.pages.replace import ReplacePage
    from PySide6.QtWidgets import QFileDialog, QPushButton

    page = ReplacePage(cfg)
    page.show()
    qapp.processEvents()
    drop_urls(page.list_restored, [tmp_path / "vanished.mp4"])
    qapp.processEvents()
    assert page.list_restored.property("input_error")
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fixture")
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(video)], ""))
    page.findChild(QPushButton, "input-files-restored").click()
    assert page.list_restored.get_all_paths() == [str(video)]
    assert not page.list_restored.property("input_error")
    assert not page.list_restored.input_feedback.isVisible()
