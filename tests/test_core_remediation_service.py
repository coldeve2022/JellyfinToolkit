"""The GUI and automation consume one config and state snapshot."""
from types import SimpleNamespace
import pytest

from utils.library import Item
from utils.labels import LabelStore
from utils.ai_scoring import AIScoreStore
from utils.state_context import state_context, score_fingerprint


def config():
    return SimpleNamespace(jellyfin_server="http://example.invalid", jellyfin_user_id="viewer",
                           jellyfin_db_path="", library_source="api", engine="v2", enhance_match=True,
                           ai_enabled=True, ai_model="model", ai_base_url="http://llm.invalid/v1",
                           score_th_keep=55, score_th_delete=25)


def items():
    return [Item("liked","Liked","Z:/liked.mp4","Movie",2024,actors=["ActorA"],bucket="jav",size=500_000_000),
            Item("next","Next","Z:/next.mp4","Movie",2024,actors=["ActorA"],bucket="jav",size=500_000_000)]


def test_snapshot_reads_scoped_feedback_for_all_consumers():
    import config as global_config
    from utils.scoring_service import score_snapshot
    cfg, rows = config(), items()
    LabelStore(global_config.DATA_DIR / "smart.db", context=state_context(cfg)).set("liked", "PLAYED_LIKE")
    result = score_snapshot(rows, cfg)
    assert result["results"][1].decision == "like"
    assert result["profile"]["n_pos"] == 1
    assert result["labels"]["liked"] == "PLAYED_LIKE"


def test_disabled_ai_never_consumes_existing_scores():
    import config as global_config
    from utils.scoring_service import score_snapshot
    cfg, rows = config(), items()
    rows[0].is_favorite = True
    rows[1].actors = ["OtherActor"]
    labels = {}
    fingerprint = score_fingerprint(rows, labels, cfg)
    AIScoreStore(global_config.DATA_DIR / "smart.db", context=state_context(cfg), fingerprint=fingerprint).save("next", 1)
    cfg.ai_enabled = False
    result = score_snapshot(rows, cfg, labels)
    assert result["results"][1].s_content == 0


def test_rerank_error_always_emits_completion():
    from workers.scorer import RerankWorker
    rows = items()
    rows[0].size = "invalid database value"
    worker = RerankWorker(rows, {})
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done and done[0]["error"]


def test_cancelled_ingest_is_not_partial_success(monkeypatch):
    from workers.ingest import IngestWorker
    monkeypatch.setattr("utils.library_source.collect", lambda *a, **k: items()[:1])
    worker = IngestWorker(config())
    worker.stop()
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0].get("cancelled") and "summary" not in done[0]


def test_direct_db_collection_cancellation_never_returns_partial_library(tmp_path):
    from utils import library, library_source
    from test_core_remediation_data import make_ef_db
    db = tmp_path / "jellyfin.db"
    make_ef_db(db)
    with pytest.raises(library_source.CollectionCancelled):
        library.collect_library(str(db), should_stop=lambda: True)


@pytest.mark.parametrize("kind", ["ingest", "analyze"])
def test_running_job_keeps_its_selected_viewer_when_settings_change(tmp_path, kind):
    from config import ToolkitConfig
    from workers.ingest import IngestWorker
    from workers.scorer import AnalyzeWorker
    from test_core_remediation_data import make_ef_db
    db = tmp_path / "jellyfin.db"
    make_ef_db(db)
    cfg = ToolkitConfig(library_source="db", jellyfin_db_path=str(db), jellyfin_user_id="viewer")
    worker = (IngestWorker if kind == "ingest" else AnalyzeWorker)(cfg)
    cfg.jellyfin_user_id = "other"
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0]["items"][0].is_played is True


def test_automatic_sole_viewer_change_never_reuses_unknown_or_old_labels():
    import config as global_config
    from utils.scoring_service import score_snapshot
    cfg = config()
    cfg.jellyfin_user_id = ""
    row = items()[1]
    row.behavior_user_id = "first-viewer"
    row.source_identity = "server:http://example.invalid"
    unknown = LabelStore(global_config.DATA_DIR / "smart.db", context=state_context(cfg))
    unknown.set(row.id, "DELETE")
    first = score_snapshot([row], cfg, unknown.all())
    assert first["labels"] == {}
    assert first["resolved_user_id"] == "first-viewer"
    LabelStore(global_config.DATA_DIR / "smart.db", context=first["source_context"]).set(row.id, "DELETE")
    row.behavior_user_id = "new-sole-viewer"
    second = score_snapshot([row], cfg)
    assert second["source_context"] != first["source_context"]
    assert second["labels"] == {}
    assert second["results"][0].decision == "review"


def test_binding_resolved_identity_is_nonblocking_and_invalidates_on_config_change():
    from utils.state_context import bind_resolved_context
    cfg = config()
    cfg.library_source = "auto"
    cfg.jellyfin_user_id = ""
    context = bind_resolved_context(cfg, {"resolved_user_id":"actual-viewer",
                                         "resolved_source_identity":"db:synthetic-resolved-db"})
    assert cfg.jellyfin_user_id == "actual-viewer"
    assert state_context(cfg) == context
    cfg.jellyfin_server = "http://different.invalid"
    assert state_context(cfg) != context


def test_legacy_keep_prevents_automatic_delete_without_becoming_taste():
    import config as global_config
    from utils.scoring_service import score_snapshot
    cfg, rows = config(), items()
    rows[0].actors = ["TrustedActor"]
    rows[0].is_favorite = True
    rows[1].actors = ["DifferentActor"]
    LabelStore(global_config.DATA_DIR / "smart.db").set(rows[1].id, "PLAYED_LIKE")
    result = score_snapshot(rows, cfg, {})
    assert result["results"][1].decision == "keep"
    assert result["profile"]["n_pos"] == 1
    assert result["labels"] == {}
    assert result["legacy_protection"] == {"next": "KEEP"}
