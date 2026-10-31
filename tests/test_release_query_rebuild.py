"""Regression coverage for replacement filtering and shared user-state reads."""
import pytest

from censor.release_filter import FilterRule, evaluate
from censor.releases.models import Availability, ReleaseCandidate, ReleaseReport
from ui.pages.censor_replace import NumberRow


@pytest.fixture
def page(qapp, qtbot):
    from config import ToolkitConfig
    from ui.pages.censor_replace import CensorReplacePage
    widget = CensorReplacePage(ToolkitConfig(library_source='api', jellyfin_user_id='viewer'))
    qtbot.addWidget(widget)
    widget._clear_filters()
    return widget


def test_partial_unknown_copies_use_one_aggregation_in_quick_and_rule_filters(page):
    row = NumberRow('ABC-123', ['C:/one.mp4', 'C:/two.mp4'])
    page._review_items = {page._review_service().normalize_path('C:/one.mp4'): {
        'is_favorite': False, 'played': False}}
    assert page._favorite_matches(row, 'unknown')
    assert page._playback_matches(row, 'unknown')
    assert evaluate([FilterRule('favorite', 'eq', 'unknown')], 'and', row, page._filter_context())
    assert not page._favorite_matches(row, 'not_favorite')


def test_preview_counts_the_same_intersection_as_visible_table(page):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/a.mp4']),
                  'DEF456': NumberRow('DEF-456', ['C:/b.mp4'])}
    page._fill_table()
    page.search.setText('ABC')
    assert page._preview_count([FilterRule('files', 'gte', '1')], 'and') == 1


def test_invalid_numeric_condition_does_not_silently_become_zero():
    row = NumberRow('ABC-123', ['C:/one.mp4'])
    with pytest.raises(ValueError):
        evaluate([FilterRule('files', 'gte', 'oops')], 'and', row)


def test_hit_source_does_not_include_failed_or_empty_sources():
    row = NumberRow('ABC-123', report=ReleaseReport(number='ABC-123',
        availability=Availability.LIKELY, candidates=(ReleaseCandidate('found'),),
        sources={'found': 'hit', 'failed': 'timeout'}))
    assert not evaluate([FilterRule('sources', 'contains', 'failed')], 'and', row)


def test_filtering_hidden_current_row_never_drags_it(page):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/one.mp4'])}
    page._fill_table()
    page.table.setCurrentCell(0, 0)
    page.search.setText('nothing')
    assert page._drag_paths() == []


def test_filtering_clears_hidden_selection_and_details_after_refresh(page):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/one.mp4'])}
    page._fill_table()
    page.table.selectRow(0)
    page.search.setText('nothing')
    page._current_key = 'ABC123'  # refresh used to reselect this hidden row
    page._fill_table()
    assert page._current_key == ''
    assert page.files.rowCount() == 0


def test_state_and_favorite_header_facets_intersect_and_clear(page):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/a.mp4']),
                  'DEF456': NumberRow('DEF-456', ['C:/b.mp4'],
                      ReleaseReport(number='DEF456', availability=Availability.NONE))}
    page._fill_table()
    page._set_column_values('state', {'todo'})
    assert page._visible_rows() == [page._rows['ABC123']]
    page._set_column_values('state', set())
    assert page._visible_rows() == []  # empty selection means no values, not all
    page._clear_filters()
    assert len(page._visible_rows()) == 2


def test_rules_are_edited_in_page_without_a_dialog(page):
    from PySide6.QtWidgets import QDialog
    panel = page.filter_bar
    assert not isinstance(panel, QDialog)
    panel.field.setCurrentIndex(panel.field.findData('files'))
    panel.value.setText('bad')
    panel.add.click()
    assert panel.rules() == []
    assert panel.error.text()
    panel.value.setText('2')
    panel.add.click()
    assert panel.rules()[0].field == 'files'
    assert panel.rules()[0].value == '2'


def test_same_context_shares_resolved_snapshot_service_and_events(page, monkeypatch):
    from utils.jellyfin_review import shared_review_service
    service = shared_review_service(page.cfg, page._censor.store.db_path.parent)
    assert page._review_service() is service
    calls = []
    monkeypatch.setattr(page, '_reload_review', lambda: calls.append(True))
    from ui.services import events
    events.user_state_changed.emit(service.context_key, str(service._db_path.parent.resolve()))
    assert calls


def test_execution_status_does_not_erase_availability():
    from censor.models import ProcessingStatus
    row = NumberRow('ABC-123', report=ReleaseReport(availability=Availability.CONFIRMED,
                                                   status=ProcessingStatus.PARTIAL))
    assert evaluate([FilterRule('state', 'eq', 'confirmed'),
                     FilterRule('lookup_status', 'eq', 'partial')], 'and', row)


def test_unknown_playback_with_ticks_remains_unknown_in_both_modules(page):
    from utils.media_review_query import playback_state
    row = NumberRow('ABC-123', ['C:/one.mp4'])
    item = {'played': None, 'playback_ticks': 100}
    page._review_items = {page._review_service().normalize_path(row.paths[0]): item}
    assert playback_state(item) == 'unknown'
    assert page._playback_matches(row, 'unknown')
    assert not page._playback_matches(row, 'progress')


def test_column_counts_exclude_self_but_keep_other_conditions(page):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/a.mp4']),
                  'ABC456': NumberRow('ABC-456', ['C:/b.mp4'],
                      ReleaseReport(availability=Availability.NONE)),
                  'DEF456': NumberRow('DEF-456', ['C:/c.mp4'])}
    page._fill_table()
    page.search.setText('ABC')
    page._set_column_values('state', {'none'})
    assert page._column_counts('state') == {'todo': 1, 'none': 1}
    assert page._preview_count([], 'and') == 1


def test_column_menu_cancel_apply_empty_and_clear_have_different_meanings(qapp, qtbot):
    from ui.release_filters import ColumnFilterMenu
    menu = ColumnFilterMenu('state', {'todo': 3, 'none': 2}, {'todo'})
    qtbot.addWidget(menu)
    seen = []
    menu.applied.connect(seen.append)
    menu.close()
    assert seen == []
    menu._check_all(False)
    menu._apply()
    assert seen == [set()]
    menu._clear()
    assert seen == [set(), None]


def test_header_arrow_requests_filter_after_column_move(page, qtbot):
    from PySide6.QtCore import QPoint, Qt
    header = page.table.horizontalHeader()
    header.filter_requested.disconnect(page._header_menu)
    seen = []
    header.filter_requested.connect(lambda pos: seen.append(header.logicalIndexAt(pos)))
    header.moveSection(header.visualIndex(0), 2)
    pos = QPoint(header.sectionViewportPosition(0) + header.sectionSize(0) - 10, 8)
    qtbot.mouseClick(header.viewport(), Qt.LeftButton, pos=pos)
    assert seen == [0]


def test_inline_remove_then_edit_keeps_other_conditions(page):
    bar = page.filter_bar
    bar.set_rules([FilterRule('number', 'contains', 'ABC'),
                   FilterRule('files', 'gte', '1'), FilterRule('sources', 'contains', 'site')])
    bar.active.setCurrentRow(1)
    bar._remove()
    bar._edit(bar.active.item(1))
    bar.value.setText('updated')
    bar.add.click()
    assert bar.rules() == [FilterRule('number', 'contains', 'ABC'),
                           FilterRule('sources', 'contains', 'updated')]


def test_legacy_rule_restore_shows_invalid_input_and_preserves_or(page):
    bar = page.filter_bar
    bar.set_rules([FilterRule('number', 'contains', 'ABC'),
                   FilterRule('files', 'gte', 'invalid')], 'or')
    assert bar.logic_key() == 'or'
    assert len(bar.rules()) == 1
    assert '旧条件未启用' in bar.error.text()


def test_saved_filters_restore_without_opening_a_dialog(page, qtbot):
    from ui.pages.censor_replace import CensorReplacePage
    page.filter_bar.set_rules([FilterRule('files', 'gte', '2')], 'or')
    page._conditions_changed()
    page._set_column_values('state', {'todo', 'none'})
    page.workspace.save()
    other = CensorReplacePage(page.cfg)
    qtbot.addWidget(other)
    assert other.filter_bar.rules() == [FilterRule('files', 'gte', '2')]
    assert other._logic == 'or'
    assert other._column_values == {'state': {'todo', 'none'}}


def test_prepare_lookup_respects_both_visible_rows_and_received_scope(page, monkeypatch):
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/a.mp4']),
                  'DEF456': NumberRow('DEF-456', ['C:/b.mp4'])}
    page._fill_table()
    page._focus_numbers = {'ABC123'}
    calls = []
    monkeypatch.setattr(page, '_lookup', lambda **kw: calls.append(kw))
    page._prepare_lookup(True)
    assert calls[-1]['numbers'] == ['ABC-123']
    page.search.setText('DEF')
    page._prepare_lookup(True)
    assert calls[-1]['numbers'] == []


def test_export_contains_only_the_current_filter(page, monkeypatch, tmp_path):
    import csv
    from PySide6.QtWidgets import QFileDialog
    page._rows = {'ABC123': NumberRow('ABC-123', ['C:/a.mp4']),
                  'DEF456': NumberRow('DEF-456', ['C:/b.mp4'])}
    page._fill_table()
    page.search.setText('ABC')
    target = tmp_path / 'export.csv'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), 'CSV'))
    page._export()
    with target.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert len(rows) == 2
    assert rows[1][0] == 'ABC-123'


def test_shared_auto_source_identity_is_isolated_by_account_and_directory(page, tmp_path):
    from copy import deepcopy
    from utils.jellyfin_review import shared_review_service
    cfg = deepcopy(page.cfg)
    cfg.library_source = 'auto'
    cfg.jellyfin_db_path = ''
    first = shared_review_service(cfg, tmp_path / 'one')
    first._resolved_identity = 'db:synthetic'
    second = shared_review_service(deepcopy(cfg), tmp_path / 'one')
    assert second is first and second._snapshot_key()
    cfg.jellyfin_user_id = 'other'
    assert shared_review_service(cfg, tmp_path / 'one')._snapshot_key() is None
    assert shared_review_service(page.cfg, tmp_path / 'two') is not first


def test_publish_snapshot_updates_replacement_cells_without_reopening(page):
    from utils.user_state_store import UserStateStore
    from ui.services import events
    row = NumberRow('ABC-123', ['C:/a.mp4'])
    page._rows = {row.key: row}
    page._fill_table()
    service = page._review_service()
    item = dict(path=row.paths[0], item_id='one', user_id='viewer', remote_path='/media/a.mp4',
                mapped=True, is_favorite=True, played=False, has_progress=False,
                playback_ticks=0, play_count=0, tags=['Shared'], genres=[], name='Example')
    UserStateStore(service._db_path).publish(service._snapshot_key(), {service.normalize_path(item['path']): item})
    events.user_state_changed.emit(service.context_key, str(service._db_path.parent.resolve()))
    assert page.table.item(0, 6).text() == '已喜欢'
    assert page.table.item(0, 7).text() == '未播放'
    page.search.setText('Shared')
    assert not page.table.isRowHidden(0)


def test_real_database_projection_joins_file_identity_number_and_latest_attempt(page, tmp_path):
    from censor.identity import identify_file
    from censor.models import VideoVerdict, CensorState, ProcessingStatus
    for name in ('one', 'two'):
        path = tmp_path / (name + '.mp4')
        path.write_bytes(name.encode() * 100)
        identity = identify_file(str(path)).serialize()
        record = VideoVerdict(path=str(path), number='ABC-123', file_identity=identity, visual_score=.75)
        page._censor.store.put(record)
        page._censor.store.set_label(str(path), CensorState.CENSORED, file_identity=identity)
    page._release.store.put(ReleaseReport(number='ABC123', availability=Availability.CONFIRMED, queried_at=1))
    page._release.store.record_attempt(ReleaseReport(number='ABC-123', queried_at=2, status=ProcessingStatus.FAILED))
    rows = page._collect_rows(page._censor, page._release, lambda: False)
    assert set(rows) == {'ABC123'}
    assert len(rows['ABC123'].paths) == len(rows['ABC123'].records) == 2
    assert rows['ABC123'].report.status is ProcessingStatus.FAILED
    assert evaluate([FilterRule('visual_score', 'gte', '.7')], 'and', rows['ABC123'])
    assert page._release.store.get('ABC123').availability is Availability.CONFIRMED


def test_no_identifier_files_remain_separate_works(page, monkeypatch):
    from censor.models import VideoVerdict
    monkeypatch.setattr(page._censor.store, 'confirmed_censored', lambda: [
        VideoVerdict(path='C:/a.mp4'), VideoVerdict(path='C:/b.mp4')])
    rows = page._collect_rows(page._censor, page._release, lambda: False)
    assert len(rows) == 2
    assert all(not row.searchable and len(row.paths) == 1 for row in rows.values())
