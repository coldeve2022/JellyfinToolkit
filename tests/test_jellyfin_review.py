"""Isolated read-only user snapshots: incorrect identity or partial fetch must never publish."""
import importlib.util
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


def test_review_backend_exists():
    assert importlib.util.find_spec('utils.jellyfin_review') is not None


@pytest.fixture
def setup_service(tmp_path, monkeypatch):
    from utils import jellyfin_review as review
    cfg = SimpleNamespace(library_source='api', jellyfin_server='http://fixture.invalid', jellyfin_api_key='fixture-secret',
                          jellyfin_user_id='viewer', library_scope_ids=[], library_scope_paths=[],
                          library_path_mappings=[{'remote': '/media', 'local': str(tmp_path / 'media')}])
    pages = [[{'Id': 'one', 'Path': '/media/one.mkv', 'Tags': ['Keep'], 'Genres': ['Drama'],
               'UserData': {'IsFavorite': True, 'Played': False, 'PlaybackPositionTicks': 42, 'PlayCount': 2}}]]
    calls = []
    def request(method, url, key, **kwargs):
        assert method == 'GET'
        assert key == 'fixture-secret'
        parsed = urlsplit(url)
        calls.append(parsed)
        if parsed.path == '/Users':
            return 200, [{'Id': 'admin', 'Name': 'Admin'}, {'Id': 'viewer', 'Name': 'Viewer'}]
        if parsed.path == '/Library/VirtualFolders':
            return 200, [{'ItemId': 'lib', 'Name': 'Library', 'Locations': ['/media']}]
        assert parsed.path == '/Users/viewer/Items'
        query = parse_qs(parsed.query)
        assert set(query['fields'][0].split(',')) >= {'UserData', 'Path', 'Tags', 'Genres'}
        index = int(query['startIndex'][0])
        if isinstance(pages[0], Exception):
            raise pages[0]
        return 200, {'Items': pages[index], 'TotalRecordCount': sum(len(p) for p in pages)}
    monkeypatch.setattr(review.api, '_req', request)
    return review.JellyfinReviewService(cfg, tmp_path / 'cache'), cfg, pages, calls, tmp_path


def test_explicit_user_pagination_and_persistent_snapshot(setup_service):
    service, cfg, pages, calls, root = setup_service
    pages.append([{'Id': 'two', 'Path': '/media/two.mkv', 'UserData': {'IsFavorite': False, 'Played': True}}])
    assert service.refresh()['ok']
    rows = service.cached_items()
    first = rows[service.normalize_path(str(root / 'media/one.mkv'))]
    assert (first['is_favorite'], first['played'], first['playback_ticks'], first['play_count']) == (True, False, 42, 2)
    assert first['user_id'] == 'viewer' and first['tags'] == ['Keep']
    from utils.jellyfin_review import JellyfinReviewService
    assert JellyfinReviewService(cfg, root / 'cache').cached_items() == rows
    assert service.favorite_paths() == [first['path']]
    assert len([c for c in calls if c.path.endswith('/Items')]) == 2


def test_no_user_does_not_pick_admin(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    cfg.jellyfin_user_id = ''
    result = JellyfinReviewService(cfg, root / 'cache').refresh()
    assert not result['ok'] and not calls


def test_failed_or_cancelled_refresh_retains_prior_snapshot_and_redacts(setup_service):
    service, cfg, pages, calls, root = setup_service
    assert service.refresh()['ok']
    old = service.cached_items()
    pages[:] = [RuntimeError('fixture-secret /media/private.mkv http://private')]
    result = service.refresh()
    assert not result['ok'] and 'fixture-secret' not in result['error'] and '/media' not in result['error']
    assert service.cached_items() == old
    assert service.refresh(should_stop=lambda: True)['cancelled']
    assert service.cached_items() == old


def test_ambiguous_or_missing_userdata_never_becomes_false(setup_service):
    service, cfg, pages, calls, root = setup_service
    pages[0] += [{'Id': 'duplicate', 'Path': '/media/one.mkv', 'UserData': {'IsFavorite': False}},
                 {'Id': 'unknown', 'Path': '/media/unknown.mkv'},
                 {'Id': 'no-path', 'UserData': {'IsFavorite': True}}]
    result = service.refresh()
    assert result['ok'] and result['unknown'] >= 2
    rows = service.cached_items()
    assert rows[service.normalize_path(str(root / 'media/one.mkv'))]['is_favorite'] is None
    assert rows[service.normalize_path(str(root / 'media/unknown.mkv'))]['is_favorite'] is None
    assert not service.favorite_paths()


def test_context_separates_user_scope_and_mapping(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    assert service.refresh()['ok']
    for field, value in [('jellyfin_user_id', 'other'), ('library_scope_paths', ['/elsewhere']),
                         ('library_path_mappings', [])]:
        changed = SimpleNamespace(**vars(cfg),)  # independent config
        setattr(changed, field, value)
        other = JellyfinReviewService(changed, root / 'cache')
        assert other.context_key != service.context_key
        assert other.cached_items() == {}


def test_scope_paths_filter_remote_namespace(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    cfg.library_scope_paths = ['/media/sub']
    pages[0] += [{'Id': 'inside', 'Path': '/media/sub/file.mkv', 'UserData': {'IsFavorite': True}}]
    scoped = JellyfinReviewService(cfg, root / 'cache')
    assert scoped.refresh()['ok']
    assert len(scoped.cached_items()) == 1


def test_scope_id_uses_authoritative_server_library(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    from utils.library_scope import scope_id
    cfg.library_scope_ids = [scope_id('server:http://fixture.invalid', 'lib')]
    scoped = JellyfinReviewService(cfg, root / 'cache')
    assert scoped.refresh()['ok'] and len(scoped.cached_items()) == 1
    cfg.library_scope_ids = ['missing-library']
    assert not JellyfinReviewService(cfg, root / 'cache').refresh()['ok']


def test_local_database_users_and_selected_identity_without_api(tmp_path, monkeypatch):
    import sqlite3
    from utils import jellyfin_review as review
    from utils.library import Item
    db = tmp_path / 'library.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems (Id TEXT)')
        conn.execute('CREATE TABLE UserData (UserId TEXT, ItemId TEXT, PlaybackPositionTicks INTEGER)')
        conn.executemany('INSERT INTO UserData VALUES (?, ?, ?)', [('viewer', 'one', 77), ('admin', 'one', 0)])
    cfg = SimpleNamespace(library_source='db', jellyfin_db_path=str(db), jellyfin_user_id='viewer',
                          library_scope_ids=[], library_scope_paths=[], library_path_mappings=[])
    path = tmp_path / 'movie.mkv'
    path.touch()
    item = Item(id='one', name='Movie', path=str(path), type_cn='Movie', year=None,
                is_favorite=True, is_played=False, play_count=3, behavior_user_id='viewer', behavior_known=True)
    monkeypatch.setattr(review.source, 'collect', lambda *args, **kwargs: [item])
    monkeypatch.setattr(review.api, '_req', lambda *a, **kw: pytest.fail('DB mode must not use network'))
    service = review.JellyfinReviewService(cfg, tmp_path / 'cache')
    assert service.list_users() == [{'id': 'admin', 'name': 'admin'}, {'id': 'viewer', 'name': 'viewer'}]
    assert service.refresh()['ok']
    row = service.cached_items()[service.normalize_path(str(path))]
    assert row['is_favorite'] is True and row['playback_ticks'] == 77 and row['play_count'] == 3
    item.behavior_user_id = 'admin'
    assert service.refresh()['ok']
    assert service.cached_items()[service.normalize_path(str(path))]['is_favorite'] is None
    cfg.jellyfin_db_path = str(tmp_path / 'other.db')
    assert review.JellyfinReviewService(cfg, tmp_path / 'cache').context_key != service.context_key


def test_local_missing_behavior_row_is_unknown(tmp_path, monkeypatch):
    import sqlite3
    from utils import jellyfin_review as review
    from utils.library import Item
    db = tmp_path / 'library.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems (Id TEXT)')
        conn.execute('CREATE TABLE UserData (UserId TEXT, ItemId TEXT)')
        conn.execute("INSERT INTO UserData VALUES ('viewer', 'other-item')")
    path = tmp_path / 'movie.mkv'
    path.touch()
    item = Item(id='missing', name='Movie', path=str(path), type_cn='Movie', year=None,
                behavior_user_id='viewer', behavior_known=True)
    cfg = SimpleNamespace(library_source='db', jellyfin_db_path=str(db), jellyfin_user_id='viewer')
    monkeypatch.setattr(review.source, 'collect', lambda *args, **kwargs: [item])
    service = review.JellyfinReviewService(cfg, tmp_path / 'cache')
    assert service.refresh()['ok']
    assert service.cached_items()[service.normalize_path(str(path))]['is_favorite'] is None


def test_partial_page_failure_and_late_cancellation_preserve_snapshot(setup_service, monkeypatch):
    service, cfg, pages, calls, root = setup_service
    assert service.refresh()['ok']
    old = service.cached_items()
    original = service._read
    def broken(endpoint, should_stop=None):
        data = original(endpoint, should_stop)
        if '/Items?' in endpoint:
            if 'startIndex=0&' in endpoint:
                data['TotalRecordCount'] = 2
            else:
                raise RuntimeError('private server error')
        return data
    monkeypatch.setattr(service, '_read', broken)
    assert not service.refresh()['ok']
    assert service.cached_items() == old
    monkeypatch.setattr(service, '_read', original)
    next_item_read = len([c for c in calls if c.path.endswith('/Items')]) + 1
    def stopping():
        return len([c for c in calls if c.path.endswith('/Items')]) >= next_item_read
    assert service.refresh(should_stop=stopping)['cancelled']
    assert service.cached_items() == old


def test_conflicting_mapping_is_unknown(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    cfg.library_path_mappings.append({'remote': '/media', 'local': str(root / 'conflicting')})
    service = JellyfinReviewService(cfg, root / 'cache')
    assert service.refresh()['ok']
    assert service.favorite_paths() == []
    assert next(iter(service.cached_items().values()))['is_favorite'] is None


def test_auto_discovery_runs_only_in_worker_and_returns_resolved_selection(tmp_path, monkeypatch):
    import sqlite3
    from utils import jellyfin_review as review
    from utils.library import Item
    db = tmp_path / 'discovered.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems (Id TEXT)')
        conn.execute('CREATE TABLE UserData (UserId TEXT, ItemId TEXT)')
        conn.execute("INSERT INTO UserData VALUES ('viewer', 'one')")
    cfg = SimpleNamespace(library_source='auto', jellyfin_user_id='viewer')
    resolutions = []
    def resolve(value):
        resolutions.append(value)
        return 'db', {'path': db}
    monkeypatch.setattr(review.source, 'resolve', resolve)
    path = tmp_path / 'movie.mkv'
    path.touch()
    item = Item(id='one', name='Movie', path=str(path), type_cn='Movie', year=None,
                behavior_known=True, behavior_user_id='viewer', is_favorite=True)
    monkeypatch.setattr(review.source, 'collect', lambda *a, **kw: [item])
    service = review.JellyfinReviewService(cfg, tmp_path / 'cache')
    initial_context = service.context_key
    assert resolutions == []
    assert service.list_users() == [{'id': 'viewer', 'name': 'viewer', 'source_db_path': str(db)}]
    result = service.refresh()
    assert result['ok'] and result['effective_source_path'] == str(db)
    assert service.context_key == initial_context
    assert service.favorite_paths() == [str(path)]
    unresolved = review.JellyfinReviewService(cfg, tmp_path / 'cache')
    assert unresolved.cached_items() == {}  # never load an auto-discovered source blindly
    other_db = tmp_path / 'changed.db'
    with sqlite3.connect(other_db) as conn:
        conn.execute('CREATE TABLE BaseItems (Id TEXT)')
        conn.execute('CREATE TABLE UserData (UserId TEXT, ItemId TEXT)')
        conn.execute("INSERT INTO UserData VALUES ('viewer', 'one')")
    monkeypatch.setattr(review.source, 'resolve', lambda value: ('db', {'path': other_db}))
    unresolved.list_users()
    assert unresolved.cached_items() == {}


def test_api_credential_change_isolates_authorization_context(setup_service):
    service, cfg, pages, calls, root = setup_service
    from utils.jellyfin_review import JellyfinReviewService
    assert service.refresh()['ok']
    cfg.jellyfin_api_key = 'different-fixture-secret'
    changed = JellyfinReviewService(cfg, root / 'cache')
    assert changed.context_key != service.context_key
    assert changed.cached_items() == {}
    assert 'fixture-secret' not in service.context_key


def test_duplicate_path_discards_progress_evidence(setup_service):
    service, cfg, pages, calls, root = setup_service
    pages[0].append({'Id': 'duplicate', 'Path': '/media/one.mkv',
                     'UserData': {'IsFavorite': False, 'Played': True, 'PlaybackPositionTicks': 123, 'PlayCount': 5}})
    assert service.refresh()['ok']
    row = next(iter(service.cached_items().values()))
    assert row['is_favorite'] is None and row['played'] is None
    assert row['has_progress'] is None and row['playback_ticks'] == 0 and row['play_count'] == 0


def test_auto_api_context_reads_same_instance_snapshot_after_refresh(setup_service, monkeypatch):
    service, cfg, pages, calls, root = setup_service
    from utils import jellyfin_review as review
    cfg.library_source = 'auto'
    monkeypatch.setattr(review.source, 'resolve', lambda value: ('api', {'server': cfg.jellyfin_server}))
    service = review.JellyfinReviewService(cfg, root / 'cache')
    assert service.refresh()['ok']
    assert len(service.favorite_paths()) == 1
    reloaded = review.JellyfinReviewService(cfg, root / 'cache')
    assert reloaded.cached_items() == {}
    reloaded.list_users()
    assert reloaded.cached_items() == service.cached_items()


@pytest.mark.parametrize('column', ['Username', 'Name'])
def test_local_users_prefer_names_in_same_database(tmp_path, column):
    import sqlite3
    from types import SimpleNamespace
    from utils.jellyfin_review import JellyfinReviewService
    db = tmp_path / 'library.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems (Id TEXT)')
        conn.execute('CREATE TABLE UserData (UserId TEXT, ItemId TEXT)')
        conn.execute("INSERT INTO UserData VALUES ('viewer', 'one')")
        conn.execute(f'CREATE TABLE Users (Id TEXT, {column} TEXT)')
        conn.execute(f'INSERT INTO Users (Id, {column}) VALUES (?, ?)', ('viewer', 'Watching Account'))
    service = JellyfinReviewService(SimpleNamespace(library_source='db', jellyfin_db_path=str(db)), tmp_path / 'cache')
    assert service.list_users() == [{'id': 'viewer', 'name': 'Watching Account'}]


@pytest.mark.parametrize('payload', ['not-json', 'null', '[]', '{"x":null}', '{"x":{"is_favorite":"false"}}',
                                     '{"x":{"is_favorite":true,"played":2}}'])
def test_invalid_cache_shape_is_a_miss(setup_service, payload):
    import sqlite3
    service, cfg, pages, calls, root = setup_service
    assert service.refresh()['ok']
    with sqlite3.connect(root / 'cache' / 'jellyfin_review.sqlite3') as conn:
        conn.execute('UPDATE snapshots SET payload=?', (payload,))
    assert service.cached_items() == {}
    assert service.favorite_paths() == []


def test_api_snapshot_keeps_item_name(setup_service):
    service, cfg, pages, calls, root = setup_service
    pages[0][0]['Name'] = 'Ordinary Movie'
    assert service.refresh()['ok']
    assert next(iter(service.cached_items().values()))['name'] == 'Ordinary Movie'


@pytest.mark.parametrize('field,value', [('is_favorite', 'false'), ('played', 2), ('has_progress', []),
                                         ('mapped', 1), ('play_count', True), ('playback_ticks', -1)])
def test_corrupt_cache_primitives_are_a_miss(setup_service, field, value):
    import json
    import sqlite3
    service, cfg, pages, calls, root = setup_service
    assert service.refresh()['ok']
    payload = service.cached_items()
    next(iter(payload.values()))[field] = value
    with sqlite3.connect(root / 'cache' / 'jellyfin_review.sqlite3') as conn:
        conn.execute('UPDATE snapshots SET payload=?', (json.dumps(payload),))
    assert service.cached_items() == {}
