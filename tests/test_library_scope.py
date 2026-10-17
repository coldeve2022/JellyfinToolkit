import json
import sqlite3
from types import SimpleNamespace

import pytest

from utils import library, library_api, library_source


def item(i, path):
    return library.Item(i, i, path, 'Movie', 2024)


def setup_collection(monkeypatch, rows):
    monkeypatch.setattr(library_source, 'resolve', lambda *a, **k: ('api', {'server': 'http://one', 'key': 'key'}))
    monkeypatch.setattr(library_source, 'collect_library_api', lambda *a, **k: (rows, 'jellyfin'))


def test_explicit_path_scope_precedes_local_mapping(monkeypatch):
    setup_collection(monkeypatch, [item('anime', '/media/anime/a.mkv'), item('movie', '/media/movies/b.mkv'),
                                   item('neighbor', '/media/anime-old/c.mkv')])
    cfg = {'library_scope_paths': ['/media/anime'],
           'library_path_mappings': [{'remote': '/media', 'local': 'Z:/mapped'}]}
    result = library_source.collect(cfg)
    assert [it.id for it in result] == ['anime']
    assert result[0].source_path == '/media/anime/a.mkv'


def test_missing_selected_library_fails_closed(monkeypatch):
    setup_collection(monkeypatch, [item('movie', '/media/movie.mkv')])
    monkeypatch.setattr(library_source, 'discover_scopes', lambda *a, **k: [], raising=False)
    with pytest.raises(library_source.SourceUnavailable, match='重新选择'):
        library_source.collect({'library_scope_ids': ['missing'], 'library_scope_paths': ['/media']})


def test_selected_libraries_and_extra_paths_are_union(monkeypatch):
    setup_collection(monkeypatch, [item('anime', '/anime/a.mkv'), item('movie', '/movies/b.mkv'),
                                   item('other', '/other/c.mkv')])
    monkeypatch.setattr(library_source, 'discover_scopes', lambda *a, **k: [
        {'id': 'anime-library', 'name': 'Animation', 'paths': ['/anime']}], raising=False)
    result = library_source.collect({'library_scope_ids': ['anime-library'], 'library_scope_paths': ['/movies']})
    assert [it.id for it in result] == ['anime', 'movie']
    assert result[0].library_ids == ['anime-library']
    assert result[0].library_names == ['Animation']


def test_scope_path_segment_windows_case_and_posix_case(monkeypatch):
    setup_collection(monkeypatch, [item('win', 'd:\\ANIME\\a.mkv'), item('adjacent', 'D:/anime2/a.mkv'),
                                   item('case', '/Anime/a.mkv'), item('unsafe', '/anime/../movies/a.mkv')])
    result = library_source.collect({'library_scope_paths': ['D:/anime/', '/anime']})
    assert [it.id for it in result] == ['win']


@pytest.mark.parametrize('legacy', [False, True])
def test_discover_db_physical_roots_readonly(tmp_path, legacy):
    db = tmp_path / 'library.db'
    with sqlite3.connect(db) as conn:
        if legacy:
            conn.execute('CREATE TABLE TypedBaseItems(guid TEXT,type TEXT,data BLOB,Path TEXT,Name TEXT,IsFolder INT)')
            conn.execute('INSERT INTO TypedBaseItems VALUES(?,?,?,?,?,?)', (
                'root-1', 'MediaBrowser.Controller.Entities.CollectionFolder',
                json.dumps({'PhysicalLocations': ['/anime', '/anime2']}).encode(), '/config/root/Anime', 'Anime', 1))
        else:
            conn.execute('CREATE TABLE BaseItems(Id TEXT,Type TEXT,Path TEXT,Name TEXT,IsFolder INT,PhysicalLocations TEXT)')
            conn.execute('INSERT INTO BaseItems VALUES(?,?,?,?,?,?)', (
                'root-1', 'MediaBrowser.Controller.Entities.CollectionFolder', '/config/root/Anime', 'Anime', 1,
                json.dumps(['/anime', '/anime2'])))
    before = db.read_bytes()
    scopes = library_source.discover_scopes({'library_source': 'db', 'jellyfin_db_path': str(db)})
    assert len(scopes) == 1
    assert scopes[0]['name'] == 'Anime'
    assert scopes[0]['paths'] == ['/anime', '/anime2']
    assert db.read_bytes() == before
    assert scopes == library_source.discover_scopes({'library_source': 'db', 'jellyfin_db_path': str(db)})


def test_api_discovery_virtualfolders_source_qualified(monkeypatch):
    calls = []
    def request(method, url, key, **kwargs):
        calls.append((method, url))
        return 200, [{'ItemId': 'root-1', 'Name': 'Movies', 'Locations': ['/movies']}]
    monkeypatch.setattr(library_api, '_req', request)
    cfg = {'library_source': 'api', 'jellyfin_server': 'http://one', 'jellyfin_api_key': 'key'}
    first = library_source.discover_scopes(cfg)
    second = library_source.discover_scopes(dict(cfg, jellyfin_server='http://two'))
    assert first[0]['paths'] == ['/movies']
    assert first[0]['id'] != second[0]['id']
    assert all(method == 'GET' and url.endswith('/Library/VirtualFolders') for method, url in calls)


def test_discovery_cancelled_before_io(monkeypatch):
    with pytest.raises(library_source.CollectionCancelled):
        library_source.discover_scopes({}, should_stop=lambda: True)

def test_pure_apply_scope_preserves_authoritative_membership():
    from utils import library_scope
    anime, movie = item('anime', '/anime/a.mkv'), item('movie', '/movies/b.mkv')
    anime.library_ids, anime.library_names = ['anime'], ['Animation']
    result = library_scope.apply_scope([anime, movie], {'library_scope_ids': ['anime']})
    assert result == [anime]
    assert anime.library_names == ['Animation']


def test_options_xml_roots_from_actual_jellyfin_layout(tmp_path):
    db = tmp_path / 'data' / 'jellyfin.db'
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems(Id TEXT,Type TEXT,Path TEXT,Name TEXT)')
    folder = tmp_path / 'root' / 'default' / 'Anime'
    folder.mkdir(parents=True)
    (folder / 'options.xml').write_text('<LibraryOptions><PathInfos><MediaPathInfo><Path>F:\\Anime</Path></MediaPathInfo></PathInfos></LibraryOptions>')
    scopes = library_source.discover_scopes({'library_source': 'db', 'jellyfin_db_path': str(db)})
    assert scopes[0]['paths'] == ['F:\\Anime']
    assert scopes[0]['name'] == 'Anime'


def test_invalid_relative_root_fails_closed(monkeypatch):
    setup_collection(monkeypatch, [item('movie', '/movies/b.mkv')])
    with pytest.raises(library_source.SourceUnavailable, match='绝对'):
        library_source.collect({'library_scope_paths': ['movies']})

def test_actual_ef_data_physical_locations_list_excludes_virtual_folder(tmp_path):
    db = tmp_path / 'jellyfin.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE BaseItems(Id TEXT,Type TEXT,Path TEXT,Name TEXT,Data TEXT)')
        conn.execute('INSERT INTO BaseItems VALUES(?,?,?,?,?)', ('root', 'MediaBrowser.Controller.Entities.CollectionFolder',
            'C:/Server/root/default/Anime', 'Anime', json.dumps({'PhysicalLocationsList': [
                'C:/Server/root/default/Anime', 'F:/Anime'], 'PhysicalFolderIds': ['folder']})))
    scopes = library_source.discover_scopes({'library_source': 'db', 'jellyfin_db_path': str(db)})
    assert len(scopes) == 1
    assert scopes[0]['paths'] == ['F:/Anime']

def test_explicit_all_attaches_library_memberships(monkeypatch):
    setup_collection(monkeypatch, [item('anime', '/anime/a.mkv')])
    monkeypatch.setattr(library_source, 'discover_scopes', lambda *a, **k: [
        {'id': 'anime-library', 'name': 'Animation', 'paths': ['/anime']}])
    result = library_source.collect({'library_scope_confirmed': True})
    assert result[0].library_ids == ['anime-library']

def test_normalize_drive_root_is_idempotent():
    from utils.library_scope import normalize_scope_path
    assert normalize_scope_path('D:/') == 'd:/'
    assert normalize_scope_path(normalize_scope_path('D:/')) == 'd:/'

def test_scope_path_canonicalizes_current_directory_segments():
    from utils.library_scope import normalize_scope_path
    assert normalize_scope_path('/media/./anime//') == '/media/anime'
    assert normalize_scope_path('D:/media/./Anime/') == 'd:/media/anime'

def test_manual_paths_work_when_library_discovery_unavailable(monkeypatch):
    setup_collection(monkeypatch, [item('anime', '/anime/a.mkv'), item('movie', '/movies/b.mkv')])
    def unavailable(*args, **kwargs):
        raise library_source.SourceUnavailable('VirtualFolders denied')
    monkeypatch.setattr(library_source, 'discover_scopes', unavailable)
    result = library_source.collect({'library_scope_confirmed': True, 'library_scope_paths': ['/anime']})
    assert [it.id for it in result] == ['anime']


def test_explicit_directory_memberships_partition_selected_roots(monkeypatch):
    setup_collection(monkeypatch, [item('anime', '/anime/a.mkv'), item('movie', '/movies/b.mkv')])
    result = library_source.collect({'library_scope_paths': ['/anime', '/movies']})
    assert result[0].library_ids and result[1].library_ids
    assert set(result[0].library_ids).isdisjoint(result[1].library_ids)
