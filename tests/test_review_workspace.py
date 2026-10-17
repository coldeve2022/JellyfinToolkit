"""Behavior and available space in the real registered review workspace."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea, QWidget
from config import ToolkitConfig
from utils.library import Item
from utils.scoring import Scored


def test_workspace_keeps_results_visible_and_pills_independent(qapp):
    from ui.main_window import MainWindow
    from ui.pages.review import ReviewPage
    cfg = ToolkitConfig()
    cfg.save = lambda: None
    window = MainWindow(cfg)
    page = ReviewPage(cfg)
    window.register_page(page)
    window.stack.setCurrentIndex(0)
    page.setEnabled(True)
    page.items = [Item(str(i), f'Example {i}', f'{i}.mp4', 'Movie', 2024,
                       tags=['剧情|旅行', '悬疑'] if i % 2 else ['剧情|日常'], bucket='jav')
                  for i in range(80)]
    page.results = [Scored(it, s_content=i / 100, decision='like') for i, it in enumerate(page.items)]
    page._fill_tables()
    page.main_tabs.setCurrentIndex(1)
    window.resize(1100, 720)
    window.show()
    qapp.processEvents()
    try:
        assert not isinstance(page.parentWidget(), QScrollArea)
        assert page.tab_like.viewport().visibleRegion().boundingRect().height() >= 230
        assert page.height() <= window.height()
        page.filter_toggle.setChecked(True)
        qapp.processEvents()
        cloud = page.filter_facets
        assert cloud.horizontalScrollBar().maximum() == 0
        facets = {tuple(cloud.item(i).data(Qt.UserRole)): cloud.item(i) for i in range(cloud.count())}
        assert ('tag', '剧情') in facets
        assert not any('|' in value for _, value in facets)
        a, b = facets['tag', '剧情'], facets['tag', '旅行']
        QTest.mouseClick(cloud.viewport(), Qt.LeftButton, pos=cloud.visualItemRect(a).center())
        QTest.mouseClick(cloud.viewport(), Qt.LeftButton, pos=cloud.visualItemRect(b).center())
        assert page._metadata_facets == {('tag', '剧情'), ('tag', '旅行')}
        assert sum(not page.tab_like.isRowHidden(r) for r in range(80)) == 40
        page.facet_selection.setCurrentIndex(0)
        assert len(page._metadata_facets) == 1
        other = b if ('tag', '剧情') in page._metadata_facets else a
        QTest.mouseClick(cloud.viewport(), Qt.LeftButton, pos=cloud.visualItemRect(other).center())
        assert page._metadata_facets == {tuple(other.data(Qt.UserRole))}
        page.filter_text.setText('no-such-title')
        assert page.tab_like.empty_hint.isVisible()
        assert '清除筛选' in page.tab_like.empty_hint.text()
        page.filter_text.clear()
        assert not page.tab_like.empty_hint.isVisible()
        window.resize(900, 600)
        page.filter_toggle.setChecked(False)
        qapp.processEvents()
        assert page.tab_like.viewport().visibleRegion().boundingRect().height() >= 200
    finally:
        window.close()
        page.deleteLater()


def test_pills_wrap_and_keyboard_toggle(qapp):
    from ui.review_workspace import FacetCloud
    from PySide6.QtWidgets import QListWidgetItem
    cloud = FacetCloud()
    for i in range(30):
        item = QListWidgetItem(f'标签: Label {i} ({i+1})')
        item.setData(Qt.UserRole, ('tag', f'Label {i}'))
        item.setData(Qt.UserRole + 10, i+1)
        item.setCheckState(Qt.Unchecked)
        cloud.addItem(item)
    cloud.resize(300, 350)
    cloud.show()
    qapp.processEvents()
    try:
        assert cloud.visualItemRect(cloud.item(3)).top() > cloud.visualItemRect(cloud.item(0)).top()
        assert cloud.horizontalScrollBar().maximum() == 0
        cloud.setCurrentRow(0)
        QTest.keyClick(cloud, Qt.Key_Space)
        assert cloud.item(0).checkState() == Qt.Checked
    finally:
        cloud.close()
