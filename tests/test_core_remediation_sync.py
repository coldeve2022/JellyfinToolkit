"""Synthetic API exchanges only: concurrent tags and rollback never lose new metadata."""
from utils import jellyfin_api


def server(monkeypatch, tags):
    state = {"Id": "item", "Name": "Synthetic", "Tags": list(tags), "Genres": []}
    posts = []
    def request(method, url, key, body=None, **kwargs):
        if url.endswith("/Users"):
            return 200, [{"Id":"admin", "Policy":{"IsAdministrator":True}}]
        if method == "GET":
            return 200, dict(state)
        posts.append(body)
        state.update(body)
        return 204, None
    monkeypatch.setattr(jellyfin_api, "_req", request)
    return state, posts


def plan():
    return [{"item_id":"item", "name":"Synthetic", "path":"Z:/synthetic.mp4",
             "before_tags":["original", "发行:dirty"], "before_genres":[],
             "after_tags":["original"], "after_genres":[], "removed":["发行:dirty"]}]


def test_clean_recomputes_from_latest_tags(monkeypatch):
    state, posts = server(monkeypatch, ["original", "发行:dirty", "concurrent-added"])
    assert jellyfin_api.apply_clean("http://example.invalid", "synthetic", plan()) == 1
    assert posts[0]["Tags"] == ["original", "concurrent-added"]


def test_restore_preserves_tags_added_after_clean(monkeypatch):
    state, posts = server(monkeypatch, ["original", "concurrent-added"])
    assert jellyfin_api.restore_backup("http://example.invalid", "synthetic", plan()) == 1
    assert state["Tags"] == ["original", "concurrent-added", "发行:dirty"]


def test_cancel_clean_does_not_write_more_items(monkeypatch):
    state, posts = server(monkeypatch, ["original", "发行:dirty"])
    assert jellyfin_api.apply_clean("http://example.invalid", "synthetic", plan(),
                                    should_stop=lambda: True) == 0
    assert posts == []


def test_backup_refuses_different_server(tmp_path):
    path = tmp_path / "backup.json"
    jellyfin_api.save_dirty_backup(plan(), str(path), server="http://one.invalid")
    import pytest
    with pytest.raises(jellyfin_api.JellyfinError):
        jellyfin_api.load_dirty_backup(str(path), server="http://two.invalid")


def test_backup_preserves_reverse_proxy_path_case(tmp_path):
    import pytest
    path = tmp_path / "backup.json"
    jellyfin_api.save_dirty_backup(plan(), str(path), server="HTTP://EXAMPLE.INVALID/Jellyfin")
    assert jellyfin_api.load_dirty_backup(str(path), server="http://example.invalid/Jellyfin")
    with pytest.raises(jellyfin_api.JellyfinError):
        jellyfin_api.load_dirty_backup(str(path), server="http://example.invalid/jellyfin")


def test_sync_address_uses_same_normalization_as_collection():
    assert jellyfin_api._base(" example.invalid:8096/ ") == "http://example.invalid:8096"


def test_cancelled_clean_worker_never_posts(monkeypatch):
    from workers.jellyfin_sync import JellyfinCleanWorker
    state, posts = server(monkeypatch, ["original", "发行:dirty"])
    worker = JellyfinCleanWorker("http://example.invalid", "synthetic")
    worker.stop()
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0]["cancelled"] and posts == []


def test_readonly_sync_scan_prepares_a_reviewable_plan(monkeypatch):
    from workers.jellyfin_sync import JellyfinScanWorker
    calls = []
    def request(method, url, key, **kwargs):
        calls.append(method)
        return 200, {"Items":[{"Id":"item", "Name":"Synthetic", "Tags":["发行:dirty"], "Genres":[]}],
                     "TotalRecordCount":1}
    monkeypatch.setattr(jellyfin_api, "_req", request)
    worker = JellyfinScanWorker("http://example.invalid", "synthetic")
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0]["plan"][0]["item_id"] == "item"
    assert calls == ["GET"]


def test_clean_worker_partial_failure_keeps_backup_and_actual_count(tmp_path, monkeypatch):
    import config
    from workers.jellyfin_sync import JellyfinCleanWorker
    config.set_data_root(tmp_path)
    posts = []
    def request(method, url, key, body=None, **kwargs):
        if url.endswith("/Users"):
            return 200, [{"Id":"admin", "Policy":{"IsAdministrator":True}}]
        if method == "GET":
            return 200, {"Id":url.rsplit("/",1)[-1], "Name":"Synthetic", "Tags":["发行:dirty"], "Genres":[]}
        posts.append(body)
        return (204, None) if len(posts) == 1 else (500, None)
    monkeypatch.setattr(jellyfin_api, "_req", request)
    rows = plan() + [dict(plan()[0], item_id="second")]
    worker = JellyfinCleanWorker("http://example.invalid", "synthetic", refresh=False, dirty_plan=rows)
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0]["cleaned"] == 1 and done[0]["error"]
    assert len(jellyfin_api.load_dirty_backup(done[0]["backup"], server="http://example.invalid")) == 2


def test_shared_sync_transaction_preserves_completed_items_after_network_error(tmp_path, monkeypatch):
    posts = []
    def request(method, url, key, body=None, **kwargs):
        if url.endswith("/Users"):
            return 200, [{"Id":"admin"}]
        if method == "GET":
            return 200, {"Id":url.rsplit("/",1)[-1], "Name":"Synthetic", "Tags":["发行:dirty"], "Genres":[]}
        posts.append(body)
        if len(posts) == 2:
            raise jellyfin_api.JellyfinError("synthetic network error")
        return 204, None
    monkeypatch.setattr(jellyfin_api, "_req", request)
    result = jellyfin_api.execute_clean_plan("http://example.invalid", "synthetic",
        plan() + [dict(plan()[0], item_id="second")], str(tmp_path / "backup.json"), refresh=False)
    assert result["cleaned"] == 1 and result["status"] == "failed"
    assert result["backup"] and result["error"]
