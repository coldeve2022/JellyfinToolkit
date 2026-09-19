"""演员名清洗单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.actor_clean import clean_actor_name, clean_actors  # noqa: E402


def test_placeholder_names_removed():
    assert clean_actor_name("素人") == ""
    assert clean_actor_name("複数の素人娘") == ""
    assert clean_actor_name("美人な人妻") == ""
    assert clean_actor_name("元Fカップグラドル") == ""


def test_series_year_annotation_removed():
    assert clean_actor_name("本田仁美(パコパコママ)") == "本田仁美"
    assert clean_actor_name("HIBIKI（女王様）") == "HIBIKI"
    assert clean_actor_name("某演员(2020年)") == "某演员"


def test_alias_kept():
    assert clean_actor_name("Tanaka Karen (田中可恋)") == "Tanaka Karen (田中可恋)"


def test_compact_description_stripped():
    assert clean_actor_name("巨乳女子プロレスラー凛叶") == "凛叶"
    assert clean_actor_name("ここな先生") == "ここな"


def test_clean_actors_dedup():
    assert clean_actors(["素人", "凛叶", "凛叶", "HIBIKI（女王様）"]) == ["凛叶", "HIBIKI"]
