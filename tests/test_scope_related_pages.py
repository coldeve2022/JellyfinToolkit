from config import ToolkitConfig
from PySide6.QtCore import Qt


def test_delete_search_limits_whole_visible_list_action(qapp):
    from ui.pages.delete import DeletePage

    page = DeletePage(ToolkitConfig())
    page._delete_rows = [
        dict(path="/movies/keep.mp4", size=20, num="A-1", name="wanted", hit="9%", reason="low"),
        dict(path="/movies/hidden.mp4", size=100, num="B-2", name="hidden", hit="80%", reason="low"),
    ]
    page._render_table()
    page.table_search.input.setText("wanted")
    assert page._planned_targets(False) == [("/movies/keep.mp4", 20)]
    page.table_search.input.clear()
    page.table.sortItems(5, Qt.DescendingOrder)
    assert page.table.item(0, 0).data(Qt.UserRole) == "/movies/hidden.mp4"


def test_context_switch_clears_actions_and_ai_report(qapp):
    from ui.pages.profile import ProfilePage
    from ui.pages.dedup import DedupPage
    from ui.pages.delete import DeletePage
    from ui.services import refresh_configuration

    cfg = ToolkitConfig()
    pages = [ProfilePage(cfg), DedupPage(cfg), DeletePage(cfg)]
    pages[0].ai_report.setPlainText("old library AI report")
    pages[1]._ai_predictions = {"same-number": {"keep_ids": ["old"]}}
    pages[2]._ai_keep_nums = {"old-number"}
    pages[2]._manual_paths = {"/old-library/movie.mp4"}
    cfg.library_scope_paths = ["/other-library"]
    for page in pages:
        refresh_configuration(page)
    assert not pages[0].ai_report.toPlainText()
    assert not pages[1]._ai_predictions
    assert not pages[2]._ai_keep_nums
    assert not pages[2]._manual_paths


def test_direct_scoring_rechecks_selection_without_network(qapp, monkeypatch):
    from utils.library import Item
    from utils.scoring_service import score_snapshot

    cfg = ToolkitConfig()
    cfg.library_scope_paths = ["/movies"]
    cfg.ai_enabled = False
    movie = Item("movie", "movie", "/movies/movie.mp4", "Movie", 2024)
    anime = Item("anime", "anime", "/anime/movie.mp4", "Movie", 2024)
    result = score_snapshot([movie, anime], cfg, {})
    assert [item.id for item in result["items"]] == ["movie"]


def test_profile_table_numbers_sort_numerically(qapp):
    from ui.pages.profile import ProfilePage
    from utils.profile import build_profile
    from utils.library import Item

    page = ProfilePage(ToolkitConfig())
    rows = [Item(str(i), f"work-{i}", f"/movie-{i}.mp4", "Movie", 2024,
                 tags=["小众" if i < 2 else "常见"], is_favorite=True) for i in range(12)]
    page.profile = build_profile(rows, {}, [])
    page._render_profile()
    page.tab_tags.sortItems(1, Qt.DescendingOrder)
    assert float(page.tab_tags.item(0, 1).text()) > float(page.tab_tags.item(1, 1).text())


def test_insight_search_preserves_selected_sort(qapp):
    from ui.pages.insight import InsightPage

    page = InsightPage(ToolkitConfig())
    page._all_rows = [("年份", "2024", 10), ("年份", "2023", 2)]
    page.table.sortItems(1, Qt.DescendingOrder)
    page._apply_filter()
    assert page.table.item(0, 1).text() == "2024"
    page.table_search.input.setText("2023")
    assert page.table.isRowHidden(0)
    assert not page.table.isRowHidden(1)


def test_direct_scoring_manual_roots_keep_group_memberships_separate(qapp):
    from utils.library import Item
    from utils.library_scope import apply_scope
    from utils.scoring import dedup_candidate_groups

    cfg = ToolkitConfig()
    cfg.library_scope_paths = ["/movies", "/anime"]
    rows = [Item("a", "a", "/movies/ABC-123.mp4", "Movie", 2024, num="ABC-123", bucket="jav"),
            Item("b", "b", "/anime/ABC-123.mp4", "Movie", 2024, num="ABC-123", bucket="jav")]
    result = apply_scope(rows, cfg)
    assert len(result) == 2
    assert result[0].library_ids != result[1].library_ids
    assert dedup_candidate_groups(result) == []
