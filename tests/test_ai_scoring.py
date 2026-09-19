"""ai_scoring + AIScoreStore 单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.ai_scoring import (  # noqa: E402
    AIScoreStore, build_score_prompt, parse_score_json, fuse_content,
)


def test_fuse_content():
    assert fuse_content(0.5, None) == 0.5
    assert fuse_content(0.5, 0.8) == 0.5 * 0.6 + 0.8 * 0.4
    assert 0.0 <= fuse_content(0.0, 0.0) <= 1.0


def test_parse_score_json():
    text = '```json\n[{"i":1,"score":0.9,"note":"很像"},{"i":2,"score":0.1}]\n```'
    parsed = parse_score_json(text)
    assert parsed[1]["score"] == 0.9
    assert parsed[2]["score"] == 0.1


def test_parse_score_json_bad():
    assert parse_score_json("连不上，无输出") == {}
    assert parse_score_json("[not json") == {}


def test_build_score_prompt_includes_candidates():
    p = build_score_prompt("JAV", "演员:希咲那奈", [{"num": "MIAA-1", "name": "x", "actors": ["a"]}])
    assert "希咲那奈" in p
    assert "MIAA-1" in p


def test_store_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    s = AIScoreStore(db)
    s.save("id1", 0.88, "note")
    s.save("id2", 0.2)
    assert s.get("id1") == 0.88
    assert abs(s.get("id2") - 0.2) < 1e-9
    many = s.get_many(["id1", "id2"])
    assert many["id1"] == 0.88
    assert len(s.all_hint()) == 2
    s.clear()
    assert s.get("id1") is None


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
