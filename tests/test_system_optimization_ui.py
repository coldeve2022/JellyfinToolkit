from itertools import product
import time

from PySide6.QtCore import Qt

from config import ToolkitConfig
from censor.releases.models import Availability, ReleaseCandidate, ReleaseReport
from ui.censor_release import ReleasePanel
from ui.pages.censor_replace import CensorReplacePage, NumberRow


def test_sorted_updates_keep_all_fields_on_the_correct_work(qapp, qtbot):
    page = CensorReplacePage(ToolkitConfig())
    qtbot.addWidget(page)
    for first, second, new, order in product(Availability, Availability, Availability,
                                            (Qt.AscendingOrder, Qt.DescendingOrder)):
        page._rows = {key: NumberRow(key, [key + ".mp4"], ReleaseReport(
            number=key, availability=state,
            candidates=tuple(ReleaseCandidate("fake") for _ in range(count))))
            for key, state, count in (("A123", first, 1), ("B123", second, 2))}
        page._fill_table()
        page.table.sortItems(2, order)
        page._on_report(ReleaseReport(number="B123", availability=new,
            candidates=tuple(ReleaseCandidate("fake") for _ in range(7))))
        actual = {page.table.item(r, 0).text(): page.table.item(r, 3).text()
                  for r in range(page.table.rowCount())}
        assert actual == {"A123": "1", "B123": "7"}, (first, second, new, order)


def test_expired_report_is_a_lookup_target(qapp, qtbot):
    page = CensorReplacePage(ToolkitConfig())
    qtbot.addWidget(page)
    page._rows = {"ABC123": NumberRow("ABC-123", ["synthetic.mp4"], ReleaseReport(
        number="ABC123", availability=Availability.NONE, queried_at=time.time() - 8 * 86400))}
    assert page.lookup_targets() == ["ABC-123"]


def test_candidate_index_releases_previous_report(qapp, qtbot):
    panel = ReleasePanel()
    qtbot.addWidget(panel)
    for index in range(20):
        panel.show_report(ReleaseReport(candidates=(ReleaseCandidate("fake", number=str(index)),)))
    assert len(panel._candidates_index) == 1
    panel.clear()
    assert not panel._candidates_index


def test_label_event_refreshes_replacement_list(qapp, qtbot, tmp_path):
    from tests.test_system_optimization_state import scanned
    from censor import CensorState
    from ui.services import events
    path, svc, result = scanned(tmp_path)
    page = CensorReplacePage(ToolkitConfig())
    page._censor = svc
    qtbot.addWidget(page)
    svc.label(path, CensorState.CENSORED, base=result)
    events.censor_changed.emit([str(path)])
    qtbot.waitUntil(lambda: page.table.rowCount() == 1)
    svc.remove_label(path)
    events.censor_changed.emit([str(path)])
    qtbot.waitUntil(lambda: page.table.rowCount() == 0)
    assert page._current_key == "" and page.files.rowCount() == 0
