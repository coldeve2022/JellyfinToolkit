"""Tag/genre normalization at the common source boundary, without actor edits."""
import sqlite3
from types import SimpleNamespace

import pytest

from utils import library, library_api


def test_build_item_splits_real_ef_pipe_shape_and_deduplicates():
    item = library.build_item(item_id='1', name='Film', path='/remote/film.mkv',
                              type_full='Movie', year=2024, tags=['4K|H264|中文字幕', 'H264'],
                              genres='剧情｜喜剧|剧情', nfo_fallback=False)
    assert item.tags == ['4K', 'H264', '中文字幕']
    assert item.genres == ['剧情', '喜剧']


def test_normalizer_preserves_label_punctuation_and_input_order():
    from utils.metadata_labels import normalize_metadata_labels
    original = [' Sci-Fi/Fantasy｜4K ', '片商: Studio A', 'Drama, Comedy', '4K', '', None]
    assert normalize_metadata_labels(original) == ['Sci-Fi/Fantasy', '4K', '片商: Studio A', 'Drama, Comedy']
    assert original[0] == ' Sci-Fi/Fantasy｜4K '


@pytest.mark.parametrize('raw', [None, '', ' | ｜ ', [], [None, '', ' ']])
def test_normalizer_ignores_empty_labels(raw):
    from utils.metadata_labels import normalize_metadata_labels
    assert normalize_metadata_labels(raw) == []


def test_normalizer_accepts_api_named_labels_and_preserves_case():
    from utils.metadata_labels import normalize_metadata_labels
    assert normalize_metadata_labels([{'Name': ' HDR|4K '}, {'Name': 'HDR'}, 'hdr']) == ['HDR', '4K', 'hdr']


def test_build_item_does_not_split_actor_or_studio_names():
    item = library.build_item(item_id='1', name='Film', path='/remote/film.mkv',
                              type_full='Movie', year=None, tags='4K|HDR',
                              actors=['Actor A/B|C'], studios=['Studio A/B|C'], nfo_fallback=False)
    assert item.actors == ['Actor A/B|C']
    assert item.studios == ['Studio A/B|C']
    assert item.tags == ['4K', 'HDR']


def test_ef_collector_normalizes_pipe_metadata(tmp_path):
    db = tmp_path / 'jellyfin.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''CREATE TABLE BaseItems(Id TEXT,Name TEXT,Path TEXT,ProductionYear INT,
            Genres TEXT,Tags TEXT,Studios TEXT,Size INT,Type TEXT,IsFolder INT,MediaType TEXT);
            CREATE TABLE Peoples(Id TEXT,Name TEXT); CREATE TABLE PeopleBaseItemMap(ItemId TEXT,PeopleId TEXT);''')
        conn.execute('INSERT INTO BaseItems VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
            '1', 'Film', '/remote/film.mkv', 2024, '剧情|喜剧', '4K|H264|H264', '', 500000000, 'Movie', 0, 'Video'))
    result = library.collect_library(db)
    assert result[0].tags == ['4K', 'H264']
    assert result[0].genres == ['剧情', '喜剧']


def test_api_collector_normalizes_compound_array_labels(monkeypatch):
    monkeypatch.setattr(library_api, 'detect', lambda *a, **k: SimpleNamespace(
        ok=True, kind='jellyfin', describe=lambda: 'test'))
    monkeypatch.setattr(library_api, 'pick_user_id', lambda *a, **k: None)
    monkeypatch.setattr(library_api, '_iter_pages', lambda *a, **k: iter([[{
        'Id': '1', 'Name': 'Film', 'Path': '/remote/film.mkv', 'Type': 'Movie',
        'Tags': ['4K|H264', ' H264 '], 'Genres': ['剧情｜喜剧']}]]))
    items, _kind = library_api.collect_library_api('http://synthetic.invalid', 'test')
    assert items[0].tags == ['4K', 'H264']
    assert items[0].genres == ['剧情', '喜剧']


def test_nfo_fallback_tags_use_same_normalization(monkeypatch):
    monkeypatch.setattr(library, 'read_nfo_features', lambda path: (['4K|HDR', '4K'], [], []))
    item = library.build_item(item_id='1', name='Film', path='/remote/film.mkv', type_full='Movie', year=None)
    assert item.tags == ['4K', 'HDR']

def test_legacy_collector_keeps_labels_and_skips_null_entries(tmp_path):
    import json
    db = tmp_path / 'library.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE TypedBaseItems(guid TEXT,type TEXT,data BLOB,Path TEXT,Name TEXT,IsFolder INT)')
        conn.execute('INSERT INTO TypedBaseItems VALUES(?,?,?,?,?,?)', (
            '1', 'MediaBrowser.Controller.Entities.Movie',
            json.dumps({'Tags': ['4K|H264', None, 'Drama, Comedy'], 'Genres': ['剧情｜喜剧']}).encode(),
            '/remote/film.mkv', 'Film', 0))
    result = library.collect_library(db)
    assert result[0].tags == ['4K', 'H264', 'Drama, Comedy']
    assert result[0].genres == ['剧情', '喜剧']
