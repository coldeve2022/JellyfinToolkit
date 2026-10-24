"""Review navigation cannot act on records excluded by the current scope."""
import pytest

from config import ToolkitConfig
from censor import CensorService, CensorOptions
from censor.models import VideoVerdict, Verdict, ProcessingStatus


@pytest.fixture
def page(qapp, qtbot, tmp_path):
    from ui.pages.censorship import CensorshipPage
    widget = CensorshipPage(ToolkitConfig(censor_visual_enabled=False, release_auto_lookup=False))
    qtbot.addWidget(widget)
    for name in ('visible', 'hidden'):
        path = tmp_path / (name + '.mp4')
        path.write_bytes(b'synthetic')
        widget._on_item(VideoVerdict(path=str(path), verdict=Verdict(needs_review=True)))
    return widget


def test_next_review_never_selects_an_invisible_search_result(page):
    page.search.setText('visible')
    page._advance_review('')
    current = page._current.path
    page._advance_review(current)
    assert page._current is None
    assert page.search.text() == 'visible'


def test_filtering_away_the_current_record_clears_its_action_target(page):
    page.search.setText('hidden')
    page._advance_review('')
    assert page._current.path.endswith('hidden.mp4')
    page.search.setText('no-such-video')
    assert page._current is None
    assert page._resolve_current() is None
    assert not page.table.selectionModel().selectedRows()


def test_withdrawing_stale_confirmation_clears_stale_ui_state(page, monkeypatch):
    page.search.setText('visible')
    page._advance_review('')
    path = page._current.path
    monkeypatch.setattr(page._service, 'remove_label', lambda _path: None)
    page._withdraw_current()
    assert page._results[path].status is ProcessingStatus.PARTIAL


def test_empty_input_is_rejected_before_parsing_tools_or_detectors(tmp_path, monkeypatch):
    path = tmp_path / 'empty.mp4'
    path.touch()
    service = CensorService(data_dir=tmp_path / 'data')
    def forbidden(*args, **kwargs):
        raise AssertionError('empty input must fail preflight')
    monkeypatch.setattr(service, '_active_detectors', forbidden)
    result = service.probe(path)
    assert result.status is ProcessingStatus.FAILED
    assert '空文件' in result.error
    assert service.store.get(str(path)) is None


def test_history_ui_uses_database_cursor_even_if_all_rows_are_filtered(page):
    page._on_history_loaded({'records': [], 'next_offset': 500, 'has_more': True})
    assert page._history_offset == 500
    assert '500' in page.progress_title.text()
