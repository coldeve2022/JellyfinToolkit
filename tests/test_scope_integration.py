"""Library selection is shared evidence scope, never only a visual filter."""
from copy import deepcopy

from config import ToolkitConfig
from utils.state_context import state_context


def test_library_selection_namespaces_manual_and_ai_state(tmp_path):
    from utils.labels import LabelStore

    cfg = ToolkitConfig()
    cfg.jellyfin_server = "http://library.test"
    cfg.jellyfin_user_id = "viewer"
    cfg.library_scope_ids = ["movie"]
    movie = state_context(cfg)
    LabelStore(tmp_path / "feedback.db", context=movie).set("same-id", "KEEP")
    cfg.library_scope_ids = ["anime"]
    anime = state_context(cfg)
    assert movie != anime
    assert LabelStore(tmp_path / "feedback.db", context=anime).all() == {}
    cfg.library_scope_ids = ["movie"]
    assert LabelStore(tmp_path / "feedback.db", context=state_context(cfg)).all() == {"same-id": "KEEP"}


def test_equivalent_scope_order_and_windows_roots_have_same_context():
    cfg = ToolkitConfig()
    cfg.library_scope_ids = ["b", "a", "a"]
    cfg.library_scope_paths = ["D:/Movies/", r"D:\Anime"]
    other = deepcopy(cfg)
    other.library_scope_ids = ["a", "b"]
    other.library_scope_paths = ["d:/anime/", "d:/movies"]
    assert state_context(cfg) == state_context(other)
    other.library_scope_paths = ["d:/anime-special"]
    assert state_context(cfg) != state_context(other)


def test_scope_changes_snapshot_key_and_rejects_old_results():
    from ui.services import _snapshot_key, _library_snapshots, publish_snapshot

    cfg = ToolkitConfig()
    cfg.library_scope_ids = ["movie"]
    movie = _snapshot_key(cfg)
    publish_snapshot(cfg, {"items": ["movie fixture"]})
    cfg.library_scope_ids = ["anime"]
    assert _snapshot_key(cfg) != movie
    assert _snapshot_key(cfg) not in _library_snapshots


def test_scope_bar_does_not_implicitly_choose_all(qapp, monkeypatch):
    from ui.scope_selector import ScopeBar

    cfg = ToolkitConfig()
    cfg.save = lambda: None
    bar = ScopeBar(cfg)
    assert not bar.selection_ready()
    assert "未选择" in bar.summary.text()
    bar.apply_selection([{"id": "movie", "name": "电影", "paths": ["/movies"]}], ["movie"], [])
    assert bar.selection_ready()
    assert "电影" in bar.summary.text()
    assert cfg.library_scope_ids == ["movie"]
    assert not cfg.library_scope_paths


def test_scope_change_is_blocked_during_tasks(qapp, monkeypatch):
    from ui.scope_selector import ScopeBar

    cfg = ToolkitConfig()
    cfg.save = lambda: None
    bar = ScopeBar(cfg)
    monkeypatch.setattr(bar, "_tasks_running", lambda: True)
    assert not bar.apply_selection([], [], ["/movies"])
    assert not getattr(cfg, "library_scope_paths", [])


def test_scope_selector_save_failure_rolls_back(qapp):
    from ui.scope_selector import ScopeBar

    cfg = ToolkitConfig()
    cfg.library_scope_ids = ["old"]
    def fail():
        raise OSError("fixture full disk")
    cfg.save = fail
    bar = ScopeBar(cfg)
    assert not bar.apply_selection([], ["new"], [])
    assert cfg.library_scope_ids == ["old"]


def test_main_window_blocks_fixed_action_footer_until_scope_selected(qapp):
    from ui.main_window import MainWindow
    from ui.pages.review import ReviewPage

    cfg = ToolkitConfig()
    cfg.save = lambda: None
    window = MainWindow(cfg)
    page = ReviewPage(cfg)
    window.register_page(page)
    assert not page.isEnabled()
    assert not page.btn_analyze.isEnabled()
    assert window.scope_bar.apply_selection([], [], ["/movies"])
    assert page.isEnabled()
    assert page.btn_analyze.isEnabled()
    window.close()


def test_changing_connection_requires_reconfirming_scope(qapp):
    from ui.scope_selector import ScopeBar

    cfg = ToolkitConfig()
    cfg.save = lambda: None
    bar = ScopeBar(cfg)
    assert bar.apply_selection([], [], [], all_libraries=True)
    assert bar.selection_ready()
    cfg.jellyfin_server = "http://another-server.test"
    assert not bar.selection_ready()


def test_selecting_scope_does_not_reprobe_unrelated_media_tools(qapp):
    from PySide6.QtWidgets import QWidget, QPushButton
    from ui.main_window import MainWindow

    cfg = ToolkitConfig()
    cfg.save = lambda: None
    window = MainWindow(cfg)
    class ToolPage(QWidget):
        __module__ = "ui.pages.lada"
        def __init__(self):
            super().__init__()
            self.cfg = cfg
            self.btn_scan = QPushButton(self)
        def on_config_changed(self):
            raise AssertionError("Selecting a library must not rescan third-party tool installations")
    page = ToolPage()
    window.register_page(page)
    assert window.scope_bar.apply_selection([], [], ["/movies"])
    assert page.btn_scan.isEnabled()
    window.close()
