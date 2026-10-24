"""Startup binds a persistent library before any services are constructed."""
from pathlib import Path

from tests.test_data_profile import library


def setup_roots(tmp_path, monkeypatch):
    import config
    shared = tmp_path / 'user'
    program = tmp_path / 'project/dist/v3.22.1/JellyfinToolkit'
    program.mkdir(parents=True)
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(config.PORTABLE_ENV, raising=False)
    monkeypatch.setattr(config, 'user_data_root', lambda: shared)
    monkeypatch.setattr(config, 'program_dir', lambda: program)
    monkeypatch.setattr(config, 'portable_mode', lambda: False)
    config.set_data_root(shared)
    return shared, program


def test_startup_chooses_and_rebinds_existing_library(qapp, tmp_path, monkeypatch):
    import config
    from ui import data_profiles
    shared, _ = setup_roots(tmp_path, monkeypatch)
    old = library(tmp_path / 'project', 5, 2)
    library(shared, 1, 0)
    monkeypatch.setattr(data_profiles, 'choose_profile', lambda rows: old)
    assert data_profiles.prepare_startup_profile()
    assert config.BASE_DIR == old.resolve()
    assert config.DATA_DIR == old / 'data'


def test_cancel_startup_never_creates_empty_database(qapp, tmp_path, monkeypatch):
    from ui import data_profiles
    shared, _ = setup_roots(tmp_path, monkeypatch)
    old = library(tmp_path / 'project', 5, 2)
    library(shared, 1, 0)
    monkeypatch.setattr(data_profiles, 'choose_profile', lambda rows: None)
    monkeypatch.setattr(data_profiles, 'recover_profile', lambda message: False)
    assert data_profiles.prepare_startup_profile() is False
    assert not (shared / 'data-profile.json').exists()
    assert (old / 'data/censor.db').exists()


def test_switch_profile_only_affects_next_normal_startup(qapp, tmp_path, monkeypatch):
    import config
    from ui import data_profiles
    from utils.data_profile import registered_root
    shared, _ = setup_roots(tmp_path, monkeypatch)
    current = library(tmp_path / 'current')
    selected = library(tmp_path / 'next')
    config.set_data_root(current)
    monkeypatch.setattr(data_profiles.QFileDialog, 'getExistingDirectory', lambda *a, **k: str(selected))
    monkeypatch.setattr(data_profiles.QMessageBox, 'question', lambda *a, **k: data_profiles.QMessageBox.Yes)
    monkeypatch.setattr(data_profiles.QMessageBox, 'information', lambda *a, **k: None)
    assert data_profiles.select_existing_profile()
    assert registered_root(shared / 'data-profile.json') == selected.resolve()
    assert config.BASE_DIR == current.resolve()


def test_selecting_data_subdirectory_is_rejected(qapp, tmp_path, monkeypatch):
    from ui import data_profiles
    shared, _ = setup_roots(tmp_path, monkeypatch)
    selected = library(tmp_path / 'next')
    messages = []
    monkeypatch.setattr(data_profiles.QFileDialog, 'getExistingDirectory', lambda *a, **k: str(selected / 'data'))
    monkeypatch.setattr(data_profiles.QMessageBox, 'warning', lambda *a, **k: messages.append(a))
    assert data_profiles.select_existing_profile() is False
    assert messages and not (shared / 'data-profile.json').exists()


def test_main_window_has_visible_library_entry(qapp, qtbot, monkeypatch):
    from config import ToolkitConfig
    from ui.main_window import MainWindow
    from ui import data_profiles
    from PySide6.QtWidgets import QPushButton
    seen = []
    monkeypatch.setattr(data_profiles, 'show_profile_manager', lambda parent=None: seen.append(True))
    page = MainWindow(ToolkitConfig()); qtbot.addWidget(page)
    button = page.findChild(QPushButton, 'data-profile-button')
    assert button is not None and '资料库' in button.text()
    button.click()
    assert seen == [True]
