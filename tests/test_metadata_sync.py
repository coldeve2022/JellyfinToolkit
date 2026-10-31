"""Offline, synthetic media only: manual-label metadata transactions."""
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from censor.identity import identify_file
from censor.models import CensorState as S
from censor.store import VerdictStore
from censor.metadata_sync import MetadataSyncService
from utils import jellyfin_api as api


@pytest.fixture
def setup(tmp_path):
    # Explicit legacy mapping exercises compatibility with saved payloads/receipts.
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"synthetic media")
    cfg = SimpleNamespace(censor_sync_nfo=True, censor_sync_jellyfin=True,
        censor_sync_create_nfo=True, censor_sync_tag_censored="像素化:有",
        censor_sync_tag_uncensored="像素化:未见", jellyfin_server="",
        jellyfin_api_key="", library_path_mappings=[], library_scope_ids=[],
        library_scope_paths=[])
    store = VerdictStore(tmp_path / "verdicts.db")
    service = MetadataSyncService(cfg, store)
    return video, cfg, store, service


def test_missing_tag_settings_use_current_defaults(setup):
    video, cfg, store, sync = setup
    del cfg.censor_sync_tag_censored
    del cfg.censor_sync_tag_uncensored
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert tags(video.with_suffix('.nfo')) == ['有码']


@pytest.mark.parametrize('backfill', [False, True])
@pytest.mark.parametrize('user_owned_new_tag', [False, True])
def test_explicit_replay_migrates_legacy_receipts_and_withdraws_safely(
        setup, monkeypatch, backfill, user_owned_new_tag):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    nfo = video.with_suffix('.nfo')
    baseline = ['keep', '像素化:未见']
    nfo.write_text('<movie><tag>keep</tag><tag>像素化:未见</tag></movie>', encoding='utf-8')
    item['Tags'] = list(baseline)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert tags(nfo) == item['Tags'] == ['keep', '像素化:有']
    if user_owned_new_tag:
        nfo.write_text('<movie><tag>keep</tag><tag>像素化:有</tag><tag>有码</tag></movie>', encoding='utf-8')
        item['Tags'].append('有码')
        baseline.append('有码')
    cfg.censor_sync_tag_censored, cfg.censor_sync_tag_uncensored = '有码', '无码'
    restarted = MetadataSyncService(cfg, store)
    # Loading settings and restarting do not enqueue completed historical records.
    before = nfo.read_bytes(), list(item['Tags']), len(calls)
    restarted.run_pending()
    assert (nfo.read_bytes(), item['Tags'], len(calls)) == before
    assert store.get_label(str(video), file_identity=identify_file(video).serialize())[0] == S.CENSORED
    if backfill:
        assert restarted.enqueue_confirmed() == 1
    else:
        restarted.enqueue(str(video), identify_file(video).serialize(), S.CENSORED)
    restarted.run_pending()
    assert tags(nfo) == item['Tags'] == ['keep', '有码']
    confirm((video, cfg, store, restarted), None)
    restarted.run_pending()
    assert tags(nfo) == item['Tags']
    assert set(tags(nfo)) == set(baseline)


def test_migrated_mapping_uncertain_ack_withdrawal_keeps_unconfirmed_tags(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    original = api._req

    def uncertain(method, *args, **kwargs):
        result = original(method, *args, **kwargs)
        return (0, None) if method == 'POST' else result

    monkeypatch.setattr(api, '_req', uncertain)
    cfg.censor_sync_tag_censored, cfg.censor_sync_tag_uncensored = '有码', '无码'
    sync.enqueue_confirmed()
    sync.run_pending()
    assert item['Tags'] == ['keep', '有码']
    assert sync.records()[0]['jellyfin'] == 'failed'
    monkeypatch.setattr(api, '_req', original)
    confirm(setup, None)
    sync.run_pending()
    assert item['Tags'] == ['keep', '有码']
    assert sync.records()[0]['jellyfin'] == 'failed'
    assert sync.records()[0]['jellyfin_error']


def confirm(setup, state):
    video, cfg, store, service = setup
    identity = identify_file(video).serialize()
    if state is None:
        store.remove_label(str(video), identity)
    else:
        store.set_label(str(video), state, file_identity=identity)
    return service.enqueue(str(video), identity, state)


def tags(path):
    return [x.text for x in ET.parse(path).getroot().findall("tag")]


def test_existing_gbk_nfo_without_encoding_declaration_uses_shared_reader(setup):
    video, cfg, store, sync = setup
    nfo = video.with_suffix('.nfo')
    nfo.write_bytes('<movie><title>中文标题</title><tag>保留</tag></movie>'.encode('gbk'))
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert sync.records()[0]['nfo'] == 'done'
    root = ET.parse(nfo).getroot()
    assert root.findtext('title') == '中文标题'
    assert [node.text for node in root.findall('tag')] == ['保留', '像素化:有']


def test_changed_tag_mapping_requeues_completed_intent(setup):
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    sync.run_pending()
    prior = sync.records()[0]['revision']
    cfg.censor_sync_tag_censored = 'custom-positive'
    sync.enqueue_confirmed()
    assert sync.records()[0]['revision'] > prior
    sync.run_pending()
    assert tags(video.with_suffix('.nfo')) == ['custom-positive']


def test_completed_withdrawal_starts_a_new_ownership_cycle(setup):
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    sync.run_pending()
    confirm(setup, None)
    sync.run_pending()
    nfo = video.with_suffix('.nfo')
    nfo.write_text('<movie><tag>像素化:有</tag></movie>', encoding='utf-8')
    confirm(setup, S.UNCENSORED)
    sync.run_pending()
    confirm(setup, None)
    sync.run_pending()
    assert tags(nfo) == ['像素化:有']


def test_same_stem_multiple_video_versions_cannot_share_nfo(setup):
    video, cfg, store, sync = setup
    video.with_suffix('.mkv').write_bytes(b'another version')
    video.with_suffix('.nfo').write_text('<movie><tag>keep</tag></movie>', encoding='utf-8')
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert tags(video.with_suffix('.nfo')) == ['keep']
    assert sync.records()[0]['nfo'] == 'failed'


def test_uncertain_first_post_withdrawal_keeps_an_explicit_unresolved_state(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    original = api._req
    def uncertain(method, url, key, **kwargs):
        if method == 'POST':
            original(method, url, key, **kwargs)
            return 0, None
        return original(method, url, key, **kwargs)
    monkeypatch.setattr(api, '_req', uncertain)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert '像素化:有' in item['Tags']
    confirm(setup, None)
    sync.run_pending()
    row = sync.records()[0]
    assert row['jellyfin'] in ('failed', 'waiting')
    assert row['jellyfin_error']
    assert '像素化:有' in item['Tags']


def fake_api(monkeypatch, setup, items=None, fail=False):
    video, cfg, store, service = setup
    cfg.jellyfin_server, cfg.jellyfin_api_key = "http://synthetic.invalid", "SECRET"
    item = {"Id": "one", "Name": "test", "Path": str(video), "Tags": ["keep"],
            "Genres": ["genre"], "UserData": {"Played": True}, "MediaSources": []}
    calls = []
    monkeypatch.setattr(api, "_iter_items", lambda *a, **k: iter([items or [item.copy()]]))
    def req(method, url, key, body=None, **kw):
        calls.append((method, url, body))
        if url.endswith("/Users"):
            return 200, [{"Id": "u"}]
        if method == "GET":
            return 200, dict(item, Tags=list(item["Tags"]))
        if fail:
            return 500, None
        item.update(body)
        return 204, None
    monkeypatch.setattr(api, "_req", req)
    return item, calls


def test_nfo_change_withdraw_and_restart_preserve_fields(setup):
    video, cfg, store, sync = setup
    nfo = video.with_suffix(".nfo")
    nfo.write_text('<movie><!--comment--><title>A</title><tag>keep</tag>'
                   '<tag>像素化:未见</tag><actor><tag>actor</tag></actor><genre>G</genre></movie>', encoding="utf-8")
    confirm(setup, S.CENSORED)
    assert sync.run_pending()["waiting"] == 1
    assert tags(nfo) == ["keep", "像素化:有"]
    confirm(setup, S.UNCENSORED)
    sync.run_pending()
    assert tags(nfo) == ["keep", "像素化:未见"]
    confirm(setup, None)
    MetadataSyncService(cfg, store).run_pending()
    assert tags(nfo) == ["keep", "像素化:未见"]
    assert "<!--comment-->" in nfo.read_text(encoding="utf-8")
    assert ET.parse(nfo).find("actor/tag").text == "actor"


def test_manual_only_unknown_and_withdraw_without_receipt_are_noop(setup):
    video, cfg, store, sync = setup
    nfo = video.with_suffix(".nfo")
    nfo.write_text('<movie><tag>像素化:有</tag></movie>', encoding="utf-8")
    identity = identify_file(video).serialize()
    assert sync.enqueue(str(video), identity, S.UNKNOWN) == 0
    sync.enqueue(str(video), identity, S.CENSORED)
    sync.run_pending()
    assert tags(nfo) == ["像素化:有"]
    sync.enqueue(str(video), identity, None)
    sync.run_pending()
    assert tags(nfo) == ["像素化:有"]


def test_idempotent_same_revision_and_only_failed_endpoint_retries(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, fail=True)
    first = confirm(setup, S.CENSORED)
    assert confirm(setup, S.CENSORED) == first
    sync.run_pending()
    nfo = video.with_suffix(".nfo")
    stamp = nfo.stat().st_mtime_ns
    assert sync.status()["failed"] == 1
    item, calls = fake_api(monkeypatch, setup)
    MetadataSyncService(cfg, store).run_pending(retry_failed=True)
    assert nfo.stat().st_mtime_ns == stamp
    assert item["Tags"] == ["keep", "像素化:有"]
    posted = [c[2] for c in calls if c[0] == "POST"]
    assert posted[0]["Genres"] == ["genre"]
    assert "UserData" not in posted[0] and "MediaSources" not in posted[0]
    assert "SECRET" not in json.dumps(sync.records())


def test_ambiguous_server_path_refused(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, items=[{"Id": "a", "Path": str(video)}, {"Id": "b", "Path": str(video)}])
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert sync.status()["failed"] == 1
    assert not [c for c in calls if c[0] == "POST"]


def test_file_replaced_and_shared_fallback_rejected(setup):
    video, cfg, store, sync = setup
    (video.parent / "second.mp4").write_bytes(b"second")
    (video.parent / "movie.nfo").write_text("<movie/>")
    # Rename so the folder's movie.nfo is a fallback, not its same-name NFO.
    renamed = video.rename(video.with_name("first.mp4"))
    setup = renamed, cfg, store, sync
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert sync.status()["failed"] >= 1
    assert not renamed.with_suffix(".nfo").exists()
    confirm(setup, S.UNCENSORED)
    renamed.write_bytes(b"replacement")
    sync.run_pending()
    assert sync.status()["stale"] >= 1


def test_xml_concurrent_change_not_overwritten(setup, monkeypatch):
    from utils import file_ops
    video, cfg, store, sync = setup
    nfo = video.with_suffix(".nfo")
    nfo.write_text("<movie><title>old</title></movie>")
    original = file_ops.safe_write_text
    def race(path, text, **kw):
        nfo.write_text("<movie><title>external</title></movie>")
        return original(path, text, **kw)
    monkeypatch.setattr(file_ops, "safe_write_text", race)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert "external" in nfo.read_text(encoding="utf-8")
    assert sync.status()["failed"] == 1


def test_new_revision_during_post_cannot_overwrite_new_status(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    req = api._req
    def changing(method, url, key, **kw):
        if method == "POST":
            confirm(setup, S.UNCENSORED)
        return req(method, url, key, **kw)
    monkeypatch.setattr(api, "_req", changing)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert sync.status()["pending"] >= 1
    monkeypatch.setattr(api, "_req", req)
    sync.run_pending()
    assert item["Tags"] == ["keep", "像素化:未见"]


def test_enqueue_confirmed_skips_historical_version(setup):
    video, cfg, store, sync = setup
    identity = identify_file(video).serialize()
    store.set_label(str(video), S.CENSORED, file_identity=identity)
    assert sync.enqueue_confirmed() == 1
    video.write_bytes(b"new version")
    assert sync.enqueue_confirmed() == 0


def test_server_fresh_get_and_mapping_and_scope(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, items=[{"Id": "one", "Path": "/remote/movie.mp4"}])
    cfg.library_path_mappings = [{"remote": "/remote", "local": str(video.parent)}]
    cfg.library_scope_paths = ["/remote"]
    item["Path"] = "/remote/movie.mp4"
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert item["Tags"] == ["keep", "像素化:有"]
    # A changed fresh GET path must be refused, even if another mapping aliases it.
    cfg.library_path_mappings += [{"remote": "/outside", "local": str(video.parent)}]
    item["Path"] = "/outside/movie.mp4"
    confirm(setup, S.UNCENSORED)
    count = len([c for c in calls if c[0] == "POST"])
    sync.run_pending()
    assert len([c for c in calls if c[0] == "POST"]) == count
    assert sync.status()["failed"] == 1


def test_api_cancel_after_committed_post_records_success(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    req = api._req
    stopped = [False]
    def request(method, url, key, **kw):
        result = req(method, url, key, **kw)
        if method == "POST":
            stopped[0] = True
        return result
    monkeypatch.setattr(api, "_req", request)
    confirm(setup, S.CENSORED)
    sync.run_pending(should_stop=lambda: stopped[0])
    assert sync.records()[0]["jellyfin"] == "done"
    assert item["Tags"] == ["keep", "像素化:有"]


def test_waiting_does_not_rehash_file_until_configured(setup, monkeypatch):
    import censor.metadata_sync as module
    confirm(setup, S.CENSORED)
    sync = setup[3]
    sync.run_pending()
    monkeypatch.setattr(module, "identify_file", lambda *a: pytest.fail("waiting busy polls media"))
    sync.run_pending()


def test_secrets_never_persist_in_exception_or_server_receipt(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    cfg.jellyfin_server += "/?api_key=SECRET"
    confirm(setup, S.CENSORED)
    sync.run_pending()
    import sqlite3
    with sqlite3.connect(sync.db_path) as db:
        assert "SECRET" not in str(db.execute("SELECT * FROM censor_metadata_receipt").fetchall())
    confirm(setup, S.UNCENSORED)
    monkeypatch.setattr(api, "update_tags_genres", lambda *a, **kw: (_ for _ in ()).throw(ValueError("SECRET")))
    sync.run_pending()
    assert "SECRET" not in str(sync.records())


def test_failed_first_api_write_withdraw_preserves_user_managed_tag(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, fail=True)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    item["Tags"].append("像素化:有")  # independently added after the failed request
    confirm(setup, None)
    sync.run_pending()
    assert item["Tags"] == ["keep", "像素化:有"]
    assert sync.records()[0]["jellyfin"] in ("failed", "waiting")


def test_failed_change_then_withdraw_removes_prior_owned_tag(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    req = api._req
    monkeypatch.setattr(api, "_req", lambda method, *args, **kw: (500, None) if method == "POST" else req(method, *args, **kw))
    confirm(setup, S.UNCENSORED)
    sync.run_pending()
    monkeypatch.setattr(api, "_req", req)
    confirm(setup, None)
    sync.run_pending()
    assert item["Tags"] == ["keep"]


def test_nfo_creation_never_overwrites_concurrent_creator(setup, monkeypatch):
    from utils import file_ops
    video, cfg, store, sync = setup
    nfo = video.with_suffix(".nfo")
    replace = file_ops.safe_replace
    def race(source, target, **kw):
        nfo.write_text("<movie><title>external</title></movie>")
        return replace(source, target, **kw)
    monkeypatch.setattr(file_ops, "safe_replace", race)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert "external" in nfo.read_text(encoding="utf-8")
    assert sync.records()[0]["nfo"] == "failed"


def test_batch_only_indexes_once_and_cancellation_does_not_write(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    second = video.with_name("second.mp4")
    second.write_bytes(b"second")
    confirm(setup, S.CENSORED)
    confirm((second, cfg, store, sync), S.UNCENSORED)
    batches = []
    def iterator(*a, **k):
        batches.append(1)
        return iter([[item, {"Id": "two", "Path": str(second)}]])
    monkeypatch.setattr(api, "_iter_items", iterator)
    assert sync.run_pending(should_stop=lambda: True)["cancelled"]
    assert not calls
    sync.run_pending()
    assert len(batches) == 1


def test_disabled_endpoint_explicit_replay_can_enable(setup):
    video, cfg, store, sync = setup
    cfg.censor_sync_nfo = False
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert not video.with_suffix(".nfo").exists()
    cfg.censor_sync_nfo = True
    sync.enqueue_confirmed()
    sync.run_pending()
    assert tags(video.with_suffix(".nfo")) == ["像素化:有"]


def test_conflicting_same_prefix_mappings_refused(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, items=[{"Id": "one", "Path": "/remote/movie.mp4"}])
    item["Path"] = "/remote/movie.mp4"
    cfg.library_path_mappings = [{"remote": "/remote", "local": str(video.parent)},
                                 {"remote": "/remote", "local": str(video.parent / "other")}]
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert not [c for c in calls if c[0] == "POST"]
    assert sync.records()[0]["jellyfin"] == "failed"


def test_unapplied_withdraw_needs_neither_server_nor_folder_nfo(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, fail=True)
    cfg.censor_sync_nfo = False
    confirm(setup, S.CENSORED)
    sync.run_pending()
    cfg.jellyfin_server = ""
    confirm(setup, None)
    sync.run_pending()
    assert sync.records()[0]["jellyfin"] == "waiting"


def test_setting_changes_only_requeue_affected_endpoint(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    nfo = video.with_suffix(".nfo")
    stamp = nfo.stat().st_mtime_ns
    cfg.library_scope_paths = [str(video.parent)]
    sync.enqueue_confirmed()
    assert sync.records()[0]["nfo"] == "done"
    assert sync.records()[0]["jellyfin"] == "pending"
    sync.run_pending()
    assert nfo.stat().st_mtime_ns == stamp
    count = len([c for c in calls if c[0] == "POST"])
    cfg.censor_sync_create_nfo = False
    sync.enqueue_confirmed()
    assert sync.records()[0]["jellyfin"] == "done"
    sync.run_pending()
    assert len([c for c in calls if c[0] == "POST"]) == count


def test_uncertain_withdraw_retry_after_manual_verification(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    original = api._req
    def uncertain(method, *args, **kw):
        result = original(method, *args, **kw)
        return (0, None) if method == "POST" else result
    monkeypatch.setattr(api, "_req", uncertain)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    confirm(setup, None)
    sync.run_pending()
    assert sync.records()[0]["jellyfin"] == "failed"
    assert "像素化:有" in item["Tags"]
    item["Tags"].remove("像素化:有")  # user verifies/removes the uncertain addition
    monkeypatch.setattr(api, "_req", original)
    restarted = MetadataSyncService(cfg, store)
    restarted.run_pending(retry_failed=True)
    assert restarted.records()[0]["jellyfin"] == "done"
    assert item["Tags"] == ["keep"]
    assert restarted._receipt(restarted.records()[0], "jellyfin") is None


def test_noop_api_get_does_not_mark_post_attempted(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    item["Tags"].append("像素化:有")
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert not [c for c in calls if c[0] == "POST"]
    receipt = sync._receipt(sync.records()[0], "jellyfin")
    assert receipt["attempted"] == 0
    confirm(setup, None)
    sync.run_pending()
    assert item["Tags"] == ["keep", "像素化:有"]


def test_same_stem_custom_extension_rejects_missing_nfo_creation(setup):
    video, cfg, store, sync = setup
    cfg.video_extensions = [".customvideo"]
    video.with_suffix(".customvideo").write_bytes(b"second")
    confirm(setup, S.CENSORED)
    sync.run_pending()
    assert not video.with_suffix(".nfo").exists()
    assert sync.records()[0]["nfo"] == "failed"


def test_legacy_receipt_schema_migrates_and_restart_preserves_baseline(setup, monkeypatch):
    import sqlite3
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    item["Tags"].append("像素化:未见")
    confirm(setup, S.CENSORED)
    sync.run_pending()
    with sqlite3.connect(sync.db_path) as db:
        for column in ("attempted", "confirmed_added"):
            db.execute("ALTER TABLE censor_metadata_receipt DROP COLUMN " + column)
        for column in ("nfo_fingerprint", "jellyfin_fingerprint"):
            db.execute("ALTER TABLE censor_metadata_sync DROP COLUMN " + column)
    restarted = MetadataSyncService(cfg, store)
    confirm((video, cfg, store, restarted), None)
    restarted.run_pending()
    assert item["Tags"] == ["keep", "像素化:未见"]


def test_legacy_uncertain_receipt_migration_preserves_unowned_tag(setup, monkeypatch):
    import sqlite3
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup, fail=True)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    item["Tags"].append("像素化:有")
    with sqlite3.connect(sync.db_path) as db:
        for column in ("attempted", "confirmed_added"):
            db.execute("ALTER TABLE censor_metadata_receipt DROP COLUMN " + column)
    restarted = MetadataSyncService(cfg, store)
    confirm((video, cfg, store, restarted), None)
    restarted.run_pending()
    assert "像素化:有" in item["Tags"]
    assert restarted.records()[0]["jellyfin"] == "failed"


def test_legacy_pending_withdrawal_cannot_report_owned_tag_removed_when_it_remains(setup):
    import sqlite3
    video, cfg, store, sync = setup
    confirm(setup, S.CENSORED)
    sync.run_pending()
    confirm(setup, None)
    with sqlite3.connect(sync.db_path) as db:
        for column in ("attempted", "confirmed_added"):
            db.execute("ALTER TABLE censor_metadata_receipt DROP COLUMN " + column)
    restarted = MetadataSyncService(cfg, store)
    restarted.run_pending()
    if "像素化:有" in tags(video.with_suffix(".nfo")):
        assert restarted.records()[0]["nfo"] in ("failed", "waiting")


def test_unsent_prepared_intent_noop_withdraw_retires_baseline(setup, monkeypatch):
    video, cfg, store, sync = setup
    item, calls = fake_api(monkeypatch, setup)
    update = api.update_tags_genres
    def unsent(*args, **kw):
        kw["tag_transform"](list(item["Tags"]))
        return False
    monkeypatch.setattr(api, "update_tags_genres", unsent)
    confirm(setup, S.CENSORED)
    sync.run_pending()
    confirm(setup, None)
    sync.run_pending()
    assert sync._receipt(sync.records()[0], "jellyfin") is None
    item["Tags"].append("像素化:有")
    monkeypatch.setattr(api, "update_tags_genres", update)
    confirm(setup, S.UNCENSORED)
    sync.run_pending()
    confirm(setup, None)
    sync.run_pending()
    assert item["Tags"] == ["keep", "像素化:有"]
