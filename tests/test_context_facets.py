"""Facets must describe actual page results, not the entire analyzed library."""
from PySide6.QtCore import Qt
from config import ToolkitConfig
from utils.library import Item
from utils.scoring import Scored


def test_index_counts_prospective_and_or_without_destroying_input():
    from utils.review_facets import FacetIndex
    items = [Item('a', 'A', 'a.mp4', 'Movie', 2024, tags=['旅行', '剧情', 'ＨＤＲ']),
             Item('b', 'B', 'b.mp4', 'Movie', 2024, tags=['旅行', '喜剧', 'hdr']),
             Item('c', 'C', 'c.mp4', 'Movie', 2024, tags=['悬疑'])]
    index = FacetIndex(items)
    selected = {('tag', '剧情')}
    opts = {o.key: o for o in index.options({'a', 'b'}, selected, 'AND')}
    assert opts['tag', '旅行'].count == 1
    assert opts['tag', '喜剧'].count == 0
    assert ('tag', '悬疑') not in opts
    opts_or = {o.key: o for o in index.options({'a', 'b'}, selected, 'OR')}
    assert opts_or['tag', '喜剧'].count == 2
    hdr = opts['tag', 'hdr']
    assert hdr.total == 2 and hdr.group == 'technical'
    assert set(hdr.aliases) == {'ＨＤＲ', 'hdr'}
    assert items[0].tags == ['旅行', '剧情', 'ＨＤＲ']


def make_page():
    from ui.pages.review import ReviewPage
    p = ReviewPage(ToolkitConfig())
    p.items = [Item('a', 'Alpha', 'a.mp4', 'Movie', 2024, tags=['旅行', '剧情', 'HDR']),
               Item('b', 'Beta', 'b.mp4', 'Movie', 2024, tags=['旅行', '喜剧']),
               Item('c', 'Gamma', 'c.mp4', 'Movie', 2024, tags=['删除专属']),
               Item('d', 'Delta', 'd.mp4', 'Movie', 2024, tags=['待复核专属'])]
    p.results = [Scored(it, s_content=.5, decision=decision) for it, decision in
                 zip(p.items, ['like', 'like', 'delete', 'review'])]
    p._fill_tables()
    return p


def facet_items(p, visible=False):
    return {tuple(p.filter_facets.item(i).data(Qt.UserRole)): p.filter_facets.item(i)
            for i in range(p.filter_facets.count())
            if not visible or not p.filter_facets.item(i).isHidden()}


def test_each_tab_only_has_own_items_and_keeps_own_selection(qapp):
    p = make_page()
    try:
        assert set(facet_items(p)) == {('tag', '待复核专属')}
        p.main_tabs.setCurrentIndex(1)
        assert ('tag', '删除专属') not in facet_items(p)
        assert facet_items(p)['tag', '旅行'].data(Qt.UserRole + 10) == 2
        p._set_metadata_facets({('tag', '剧情')})
        assert p.deck_ids == ['d']  # A like-page condition must not alter the review queue.
        assert facet_items(p)['tag', '旅行'].data(Qt.UserRole + 10) == 1
        assert ('tag', '喜剧') not in facet_items(p, visible=True)
        p.main_tabs.setCurrentIndex(2)
        assert not p._metadata_facets
        assert set(facet_items(p)) == {('tag', '删除专属')}
        assert not p.tab_del.isRowHidden(0)
        p.main_tabs.setCurrentIndex(1)
        assert p._metadata_facets == {('tag', '剧情')}
        assert p.tab_like.isRowHidden(next(r for r in range(2) if p.tab_like.item(r, 1).text() == 'Beta'))
        p.filter_mode.setCurrentIndex(1)
        assert ('tag', '喜剧') in facet_items(p, visible=True)
        assert facet_items(p)['tag', '喜剧'].data(Qt.UserRole + 10) == 2
    finally:
        p.close()


def test_single_choice_counts_replacement_and_selected_can_clear_across_groups(qapp):
    p = make_page()
    try:
        p.main_tabs.setCurrentIndex(1)
        p._set_metadata_facets({('tag', '剧情')})
        assert facet_items(p)['tag', '旅行'].data(Qt.UserRole + 10) == 1
        p.facet_selection.setCurrentIndex(0)
        assert facet_items(p)['tag', '旅行'].data(Qt.UserRole + 10) == 2
        p._set_metadata_facets({('tag', 'HDR')})
        assert p.selected_facets.item(0).data(Qt.UserRole) == ('tag', 'hdr')
        p.facet_search.setText('not-found')
        p.selected_facets.item(0).setCheckState(Qt.Unchecked)
        assert not p._metadata_facets
    finally:
        p.close()


def test_index_prospective_counts_match_brute_force():
    from random import Random
    from utils.review_facets import FacetIndex
    rng = Random(1)
    items = [Item(str(i), str(i), f'{i}.mp4', 'Movie', 2024,
                  tags=rng.sample([f'tag{k}' for k in range(20)], 5)) for i in range(120)]
    index = FacetIndex(items)
    base = {it.id for it in items[:75]}
    for mode in ['AND', 'OR']:
        for selected in [set(), {('tag', 'tag1')}, {('tag', 'tag1'), ('tag', 'tag2')}]:
            for single in [False, True]:
                for opt in index.options(base, selected, mode, single):
                    wanted = selected if opt.key in selected else {opt.key} if single else selected | {opt.key}
                    expected = [it for it in items if it.id in base and (
                        all(tag in it.tags for _, tag in wanted) if mode == 'AND'
                        else any(tag in it.tags for _, tag in wanted))]
                    assert opt.count == len(expected)


def test_done_and_live_ai_facets_only_include_actual_rows(qapp):
    p = make_page()
    try:
        p.labels = {'a': 'KEEP'}
        p._fill_tables()
        p.main_tabs.setCurrentIndex(3)
        assert ('tag', '剧情') in facet_items(p)
        assert ('tag', '喜剧') not in facet_items(p)
        p.main_tabs.setCurrentIndex(4)
        assert not facet_items(p)
        p._collect_ai_suggestion({'item_id': 'b', 'verdict': 'KEEP', 'reason': 'example'})
        assert ('tag', '喜剧') in facet_items(p)
        assert ('tag', '剧情') not in facet_items(p)
        p._collect_ai_suggestion({'item_id': 'c', 'verdict': 'DELETE', 'reason': 'example'})
        assert ('tag', '删除专属') in facet_items(p)
        p._set_metadata_facets({('tag', '喜剧')})
        assert sum(not p.ai_suggestions.isRowHidden(r) for r in range(2)) == 1
        p.main_tabs.setCurrentIndex(3)
        assert not p._metadata_facets
        assert not p.tab_done.isRowHidden(0)
    finally:
        p.close()


def test_rerank_removes_candidate_but_selection_remains_removable(qapp):
    p = make_page()
    try:
        p._set_metadata_facets({('tag', '待复核专属')})
        p.labels = {'d': 'KEEP'}
        p._fill_tables()
        assert not p.deck_ids
        assert p.selected_facets.count() == 1
        assert p.selected_facets.item(0).data(Qt.UserRole + 10) == 0
        p.selected_facets.item(0).setCheckState(Qt.Unchecked)
        assert not p._metadata_facets
        assert not facet_items(p)
        p.main_tabs.setCurrentIndex(3)
        assert ('tag', '待复核专属') in facet_items(p)
    finally:
        p.close()


def test_card_list_and_table_share_only_their_page_filters(qapp):
    from PySide6.QtWidgets import QLabel
    p = make_page()
    try:
        p.main_tabs.setCurrentIndex(1)
        p._set_metadata_facets({('tag', '剧情')})
        p._toggle_view()
        layout = p._list_cardlay['like']
        cards = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
        assert len(cards) == 1
        assert any('Alpha' in label.text() for label in cards[0].findChildren(QLabel))
        p.main_tabs.setCurrentIndex(2)
        p._toggle_view()
        assert any('Gamma' in label.text() for label in p._list_cardlay['del'].itemAt(0).widget().findChildren(QLabel))
        assert p.deck_ids == ['d']
    finally:
        p.close()


def test_spelling_aliases_count_items_once_and_preserve_tag_genre_distinction():
    from utils.review_facets import FacetIndex
    item = Item('a', 'A', 'a.mp4', 'Movie', 2024, tags=['HDR', 'hdr', 'ＨＤＲ'], genres=['HDR'])
    index = FacetIndex([item])
    opts = {o.key: o for o in index.options({'a'}, set())}
    assert set(opts) == {('tag', 'hdr'), ('genre', 'hdr')}
    assert opts['tag', 'hdr'].count == 1
    assert opts['genre', 'hdr'].group == 'genre'
    assert item.tags == ['HDR', 'hdr', 'ＨＤＲ']


def test_other_result_page_shortcuts_never_label_hidden_review_card(qapp, monkeypatch):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    p = make_page()
    calls = []
    monkeypatch.setattr(p, '_label', calls.append)
    try:
        p.main_tabs.setCurrentIndex(1)
        p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
        assert not calls
        p.main_tabs.setCurrentIndex(0)
        p.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
        assert calls == ['keep']
    finally:
        p.close()


def test_text_search_limits_facets_but_selected_chips_stay_cancellable(qapp):
    p = make_page()
    try:
        p.main_tabs.setCurrentIndex(1)
        p._set_metadata_facets({('tag', '剧情')})
        p.filter_text.setText('Beta')
        assert ('tag', '删除专属') not in facet_items(p)
        assert p.selected_facets.count() == 1
        p.selected_facets.item(0).setCheckState(Qt.Unchecked)
        assert not p._metadata_facets
        assert facet_items(p)['tag', '旅行'].data(Qt.UserRole + 10) == 1
        assert ('tag', '喜剧') in facet_items(p, visible=True)
        assert ('tag', '剧情') not in facet_items(p)
        p.filter_text.clear()
        assert ('tag', 'hdr') not in facet_items(p, visible=True)
        p.facet_group.setCurrentIndex(p.facet_group.findData('technical'))
        assert ('tag', 'hdr') in facet_items(p, visible=True)
    finally:
        p.close()
