"""Complete the remaining accepted layout and cross-host scheduling behavior."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier, Event
import time


def test_sources_can_overlap_without_changing_result_semantics(tmp_path):
    from censor.releases import ReleaseService
    from censor.releases.models import ReleaseCandidate
    from censor.models import ProcessingStatus
    barrier = Barrier(2)
    class Source:
        version = '1'
        def __init__(self, name):
            self.name = name
        def available(self):
            return True, ''
        def search(self, query, **kwargs):
            barrier.wait(.8)
            return [ReleaseCandidate(self.name, number=query.number)]
    service = ReleaseService({'release_sources': ['one', 'two'], 'release_source_workers': 2},
                             data_dir=tmp_path, factory=Source)
    report = service.lookup('ABC-123')
    assert report.status is ProcessingStatus.COMPLETED
    assert {c.source for c in report.candidates} == {'one', 'two'}


def test_concurrent_requests_share_one_atomic_work_budget(monkeypatch):
    from censor.releases.transport import HttpTransport, TransportConfig, TransportError, query_scope
    monkeypatch.delenv('JELLYFIN_TOOLKIT_OFFLINE', raising=False)
    transport = HttpTransport(TransportConfig(retries=0, min_interval=0, max_requests=3))
    barrier = Barrier(5)
    calls = []
    monkeypatch.setattr(transport, '_throttle', lambda _: barrier.wait(2))
    monkeypatch.setattr(transport, '_once', lambda *a: calls.append(1) or b'ok')
    def request():
        try:
            transport.get('https://synthetic.invalid/')
            return True
        except TransportError:
            return False
    with query_scope(3), ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(copy_context().run, request) for _ in range(5)]
        results = [f.result() for f in futures]
    assert len(calls) == 3 and sum(results) == 3


def test_parallel_source_service_preserves_budget_and_partial_negative(tmp_path, monkeypatch):
    from censor.releases import ReleaseService
    from censor.releases.transport import HttpTransport, TransportConfig
    from censor.releases.models import ReleaseCandidate, Availability
    from censor.models import ProcessingStatus
    monkeypatch.delenv('JELLYFIN_TOOLKIT_OFFLINE', raising=False)
    transport = HttpTransport(TransportConfig(retries=0, min_interval=0, max_requests=40))
    calls = []
    monkeypatch.setattr(transport, '_once', lambda *a: calls.append(1) or b'ok')
    class Source:
        version = '1'
        def __init__(self, name):
            self.name, self.transport = name, transport.fork()
        def available(self):
            return True, ''
        def search(self, query, **kw):
            self.transport.get('https://' + self.name + '.invalid/')
            return [ReleaseCandidate(self.name, number=query.number)]
    service = ReleaseService({'release_source_workers': 2, 'release_max_requests': 1}, data_dir=tmp_path)
    monkeypatch.setattr(service, 'build_sources', lambda *a: [Source('one'), Source('two')])
    report = service.lookup('ABC-123')
    assert len(calls) == 1
    assert report.status is ProcessingStatus.PARTIAL
    assert report.availability is Availability.UNKNOWN


def test_forked_routes_are_independent_but_host_throttles_are_shared(monkeypatch):
    from censor.releases.transport import HttpTransport, TransportConfig
    monkeypatch.delenv('JELLYFIN_TOOLKIT_OFFLINE', raising=False)
    transport = HttpTransport(TransportConfig(retries=0, min_interval=1))
    first, second = transport.fork(), transport.fork()
    first.config.proxy = 'http://only-first.invalid'
    assert second.config.proxy != first.config.proxy
    entered, release, other = Event(), Event(), Event()
    first._last_hit['one.invalid'] = time.monotonic()
    def pause(_):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr('censor.releases.transport._pause', pause)
    monkeypatch.setattr(first, '_once', lambda *a: b'ok')
    monkeypatch.setattr(second, '_once', lambda *a: other.set() or b'ok')
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.get, 'https://one.invalid/')
        assert entered.wait(1)
        two = pool.submit(second.get, 'https://two.invalid/')
        try:
            assert other.wait(1), 'waiting on one host must not lock another host'
        finally:
            release.set()
        assert one.result() == 'ok' and two.result() == 'ok'


def test_workspace_reset_restores_filters_splitter_and_menu(qapp, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget, QTableWidget, QSplitter, QLineEdit, QComboBox, QVBoxLayout
    from ui.workspace_preferences import WorkspacePreferences
    owner = QWidget()
    qtbot.addWidget(owner)
    layout = QVBoxLayout(owner)
    splitter = QSplitter()
    table = QTableWidget(0, 2)
    splitter.addWidget(table)
    splitter.addWidget(QWidget())
    layout.addWidget(splitter)
    owner.resize(700, 400)
    owner.show()
    splitter.setSizes([400, 250])
    default_sizes = splitter.sizes()
    search, state = QLineEdit(), QComboBox()
    layout.addWidget(search)
    layout.addWidget(state)
    state.addItem('all', '')
    state.addItem('filtered', 'x')
    pref = WorkspacePreferences(owner, 'reset-complete', table, splitter=splitter,
                                filters={'search': search, 'state': state})
    menu = pref.menu(owner)
    column_action = menu.actions()[0]
    columns = column_action.menu()
    columns.actions()[1].setChecked(False)
    search.setText('old search')
    state.setCurrentIndex(1)
    pref._orientation(True)
    splitter.setSizes([80, 250])
    pref.reset()
    assert search.text() == '' and state.currentData() == ''
    assert splitter.orientation() == Qt.Horizontal
    assert abs(splitter.sizes()[0] / sum(splitter.sizes()) - default_sizes[0] / sum(default_sizes)) < .05
    assert columns.actions()[1].isChecked() and not table.isColumnHidden(1)
