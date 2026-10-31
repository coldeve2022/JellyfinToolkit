"""File replacement and old databases must not reuse another file's decisions."""

import os
import sqlite3

from censor.models import CensorState, VideoVerdict
from censor.store import VerdictStore


def test_identity_detects_same_size_same_timestamp_replacement(tmp_path):
    from censor.identity import identify_file

    path = tmp_path / "sample.bin"
    path.write_bytes(b"a" * 200000)
    before = identify_file(path)
    stat = path.stat()
    path.write_bytes(b"b" * 200000)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert identify_file(path) != before


def test_strict_cache_requires_identity_signature_and_completion(tmp_path):
    from censor.models import ProcessingStatus

    store = VerdictStore(tmp_path / "results.db")
    result = VideoVerdict(path="sample", file_identity="v1", analysis_signature="algorithm1")
    store.put(result)
    assert store.get("sample", file_identity="v1", analysis_signature="algorithm1")
    assert store.get("sample", file_identity="v2", analysis_signature="algorithm1") is None
    assert store.get("sample", file_identity="v1", analysis_signature="algorithm2") is None
    result.status = ProcessingStatus.PARTIAL
    store.put(result)
    # 语义已修正（v4）：未完成的分析**不得覆盖**已完成的结论。
    # 同一份身份 + 同一份分析签名，之前已经完整跑完过 → 仍然命中缓存。
    # 以前这里断言"读不到"，等于承认"部分完成会把好结论冲掉"——
    # 那样一来重扫一次失败就把上次的结果白扔了。
    cached = store.get("sample", file_identity="v1", analysis_signature="algorithm1")
    assert cached is not None, "部分完成不应冲掉已完成的结论"
    assert cached.status is ProcessingStatus.COMPLETED


def test_label_version_changes_preserve_history(tmp_path):
    store = VerdictStore(tmp_path / "results.db")
    store.set_label("sample", CensorState.CENSORED, "first", file_identity="v1")
    assert store.get_label("sample", file_identity="v2") is None
    store.set_label("sample", CensorState.UNCENSORED, "second", file_identity="v2")
    assert store.get_label("sample", file_identity="v1") == (CensorState.CENSORED, "first")
    assert store.get_label("sample", file_identity="v2") == (CensorState.UNCENSORED, "second")
    store.clear_probes()
    assert len(store.label_history("sample")) == 2


def test_legacy_database_migrates_without_confirming_unknown_file(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE censor_label (path TEXT PRIMARY KEY, state TEXT NOT NULL, "
            'note TEXT NOT NULL DEFAULT "", labeled_at TEXT NOT NULL DEFAULT "")'
        )
        conn.execute(
            "INSERT INTO censor_label VALUES (?,?,?,?)", ("sample", "censored", "legacy", "2020")
        )
    store = VerdictStore(db)
    assert store.get_label("sample") == (CensorState.CENSORED, "legacy")
    assert store.get_label("sample", file_identity="current") is None
    assert len(store.label_history("sample")) == 1
    assert len(VerdictStore(db).label_history("sample")) == 1


def test_corrupt_json_types_are_treated_as_empty(tmp_path):
    store = VerdictStore(tmp_path / "results.db")
    store.put(VideoVerdict(path="sample"))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE censor_probe SET evidence=?, candidates=?", ("42", '{"x":1}'))
    result = store.get("sample")
    assert result.verdict.evidence == []
    assert result.candidates == []
