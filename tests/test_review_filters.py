"""Real offscreen review tables: filters, sort identities and explicit adoption."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QMessageBox, QLabel
from config import ToolkitConfig
from utils.library import Item
from utils.scoring import Scored


@pytest.fixture
def page(qapp):
    from ui.pages.review import ReviewPage
    p = ReviewPage(ToolkitConfig())
    p.items = [
        Item('a', 'Alpha', 'shared.mp4', 'Movie', 2020, tags=['共同', '独有'], genres=['剧情'], bucket='jav', size=2*1024**2),
        Item('b', 'Beta', 'shared.mp4', 'Movie', 2021, tags=['共同'], genres=['独有'], bucket='hentai', size=100*1024**2),
        Item('c', 'Gamma', 'c.mp4', 'Movie', 2022, tags=['别的'], genres=['剧情'], bucket='jav'),
    ]
    p.results = [Scored(p.items[0], s_content=.09, decision='like', reason='实际依据 A'), Scored(p.items[1], s_content=1., decision='like', reason='实际依据 B'), Scored(p.items[2], s_content=.5, decision='review')]
    p._fill_tables()
    yield p
    p.close()


def visible_ids(table):
    from ui.pages.review import ROLE_ITEM_ID
    return [table.item(r, 0).data(ROLE_ITEM_ID) for r in range(table.rowCount()) if not table.isRowHidden(r)]


def test_numeric_sort_and_filter_preserve_selected_identity(page):
    page.tab_like.sortItems(2, Qt.AscendingOrder)
    assert visible_ids(page.tab_like) == ['a', 'b']
    page.tab_like.selectRow(1)
    assert page._selected_item_ids(page.tab_like) == ['b']
    page.filter_text.setText('  beta  ')
    assert visible_ids(page.tab_like) == ['b']
    assert page._selected_item_ids(page.tab_like) == ['b']
    assert '1 / 2' in page.filter_counts.text()
    page.filter_text.clear()
    for table in (page.tab_like, page.tab_del, page.tab_done, page.ai_suggestions):
        h = table.horizontalHeader()
        assert h.sectionsMovable()
        assert all(h.sectionResizeMode(c) == QHeaderView.Interactive for c in range(table.columnCount()))
        h.resizeSection(0, 190)
        assert h.sectionSize(0) == 190


def test_facets_distinguish_tag_and_genre_and_support_and_or(page):
    # Facet selections now belong to the active result page.
    page.main_tabs.setCurrentIndex(1)
    page._set_metadata_facets({('tag', '独有')})
    assert visible_ids(page.tab_like) == ['a']
    page._set_metadata_facets({('genre', '独有')})
    assert visible_ids(page.tab_like) == ['b']
    page._set_metadata_facets({('tag', '共同'), ('genre', '剧情')})
    assert visible_ids(page.tab_like) == ['a']
    page.filter_mode.setCurrentIndex(1)
    assert set(visible_ids(page.tab_like)) == {'a', 'b'}
    page.filter_bucket.setCurrentIndex(page.filter_bucket.findData('hentai'))
    assert visible_ids(page.tab_like) == ['b']
    assert page.deck_ids == []
    keys = {tuple(page.filter_facets.item(i).data(Qt.UserRole))
            for i in range(page.filter_facets.count())}
    assert ('genre', '独有') in keys
    assert ('tag', '独有') not in keys  # Only Alpha has it; category now contains Beta.


def test_ai_sort_filter_and_adoption_use_checked_visible_item_ids(page, monkeypatch):
    monkeypatch.setattr(page, '_refresh_after_change', lambda: None)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.Yes)
    page._collect_ai_suggestion({'item_id': 'a', 'verdict': 'KEEP', 'reason': 'A'})
    page._collect_ai_suggestion({'item_id': 'b', 'verdict': 'DELETE', 'reason': 'B'})
    page.ai_suggestions.sortItems(1, Qt.DescendingOrder)
    assert visible_ids(page.ai_suggestions) == ['b', 'a']
    for row in range(2):
        page.ai_suggestions.item(row, 0).setCheckState(Qt.Checked)
    page.filter_text.setText('Beta')
    page._adopt_ai_suggestions()
    assert page.store.get('b') == 'DELETE'
    assert page.store.get('a') is None


def test_card_and_table_expose_full_metadata_and_original_evidence(page):
    reason = page.tab_like.item(next(r for r in range(2) if page.tab_like.item(r, 1).text() == 'Alpha'), 3)
    assert '实际依据 A' in reason.text()
    assert '独有' in reason.toolTip()
    assert page.main_tabs.currentIndex() == 0
    assert any('别的' in label.text() for label in page.card.findChildren(QLabel))
    page.main_tabs.setCurrentIndex(1)
    page._toggle_view()
    card = page._list_cardlay['like'].itemAt(0).widget()
    texts = [label.text() for label in card.findChildren(QLabel)]
    assert any('实际依据 A' in text for text in texts)
    assert any('独有' in text for text in texts)


def test_sort_after_filter_keeps_only_matching_identity_visible(page):
    page.filter_text.setText('Alpha')
    page.tab_like.sortItems(1, Qt.DescendingOrder)
    assert visible_ids(page.tab_like) == ['a']
    page.tab_like.sortItems(2, Qt.AscendingOrder)
    assert visible_ids(page.tab_like) == ['a']


def test_card_label_uses_filtered_deck_identity_and_preserves_undo(page, monkeypatch):
    monkeypatch.setattr(page, '_refresh_after_change', lambda: None)
    page.filter_text.setText('Gamma')
    assert page.deck_ids == ['c']
    page._label('keep')
    assert page.store.get('c') == 'PLAYED_LIKE'
    assert page.store.get('a') is None
    page._undo()
    assert page.store.get('c') is None


def test_profile_explanation_does_not_cross_categories(page):
    page.items[0].is_favorite = True
    page._fill_tables()
    text = page._reason_details(page.results[1])
    assert '实际依据 B' in text
    assert '喜欢特征重合：无' in text
    own = page._reason_details(page.results[0])
    assert '标签:共同' in own
    assert '类型:剧情' in own
    assert page.results[1].s_content == 1.0

