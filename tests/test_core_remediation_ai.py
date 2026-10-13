"""AI results must be batch-bound suggestions; failures preserve previous valid state."""
import pytest
from utils.ai_profile import AIProfileStore
from utils.ai_scoring import parse_score_json
from workers.ai import AITasteWorker, AIRecheckWorker
from workers.ai_score import AIScoreWorker


def test_invalid_taste_does_not_destroy_old_profile(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    AIProfileStore(db).save({("actor", "ActorA")}, set())
    monkeypatch.setattr("workers.ai.query_llm", lambda *a, **k: "No JSON")
    worker = AITasteWorker({}, db, "http://example.invalid", "", "synthetic")
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert AIProfileStore(db).load()["liked_keys"] == {("actor", "ActorA")}
    assert done[0]["error"]


def test_recheck_maps_duplicate_numbers_to_batch_identity(monkeypatch):
    monkeypatch.setattr("workers.ai.query_llm", lambda *a, **k:
                        '[{"i":1,"verdict":"DELETE","reason":"first only"},'
                        '{"i":2,"verdict":"KEEP","reason":"second"}]')
    candidates = [dict(item_id=i, num="ABC-123", name=i, hit="20%", reason="", actors=[])
                  for i in ("cd1", "cd2")]
    worker = AIRecheckWorker(candidates, "http://example.invalid", "", "model", context="review")
    verdicts = []
    worker.verdict.connect(verdicts.append)
    worker.run()
    assert [(v["item_id"], v["verdict"]) for v in verdicts] == [("cd1", "DELETE"), ("cd2", "KEEP")]


@pytest.mark.parametrize("text", ['null', '{}', '[{"i":1,"score":"NaN"}]',
                                  '[{"i":1,"score":null}]', '[{"i":1,"score":1.4}]'])
def test_malformed_semantic_scores_are_rejected(text):
    assert parse_score_json(text) == {}


def test_invalid_scoring_response_reports_failure_and_preserves_cache(tmp_path, monkeypatch):
    from utils.ai_scoring import AIScoreStore
    db = tmp_path / "state.db"
    AIScoreStore(db).save("id", .8)
    monkeypatch.setattr("workers.ai_score.query_llm", lambda *a, **k: "No JSON")
    worker = AIScoreWorker([{"item_id":"id", "num":"ABC-123", "name":"a"}], "profile",
                           "JAV", db, "http://example.invalid", "", "model")
    done = []
    worker.finished.connect(done.append)
    worker.run()
    assert done[0]["error"]
    assert AIScoreStore(db).get("id") == .8
