"""AI 参与分类 - 纯函数/存储/评分增强 单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, Item  # noqa: E402
from utils.ai_profile import (  # noqa: E402
    AIProfileStore, build_ai_prompt, parse_ai_taste,
)
from utils.scoring import DEC_DELETE, DEC_LIKE, rank_items  # noqa: E402


def _mk(id_, actors=(), tags=(), fav=False):
    return Item(id=id_, name=f"{id_}", path=f"D:\\{id_}.mp4", type_cn="Movie",
                year=2022, genres=[], tags=list(tags), studios=[], actors=list(actors),
                size=2_000_000_000, num=None, bucket=BUCKET_JAV, is_uncensored=False,
                is_favorite=fav, is_played=False)


def test_parse_liked_disliked():
    text = ('```json\n{"liked":[{"kind":"tag","value":"巨乳"},{"kind":"actor","value":"希咲那奈"}],'
            '"disliked":[{"kind":"prefix","value":"OREMO"},{"kind":"tag","value":"人外"}],'
            '"notes":"偏好素人系"}\n```')
    liked, disliked, notes = parse_ai_taste(text)
    assert ("tag", "巨乳") in liked
    assert ("actor", "希咲那奈") in liked
    assert ("tag", "人外") in disliked
    assert ("prefix", "OREMO") not in disliked   # 系列前缀不算"内容不喜欢"，被过滤
    assert notes == "偏好素人系"


def test_store_roundtrip_then_clear(tmp_path):
    store = AIProfileStore(tmp_path / "smart.db")
    store.save({("tag", "巨乳"), ("actor", "A")}, {("prefix", "X")}, "note")
    data = store.load()
    assert data is not None
    assert ("tag", "巨乳") in data["liked_keys"]
    assert ("prefix", "X") in data["disliked_keys"]
    store.clear()
    assert store.load() is None


def test_extra_liked_flips_decision():
    pos = _mk("p1", actors=["A"], fav=True)
    unseen = _mk("u1", actors=["B"], tags=["X"])
    # 无 AI：B/X 不在 liked → unseen 零命中 → delete
    res0 = {r.item.id: r.decision for r in rank_items([pos, unseen], {})}
    assert res0["u1"] == DEC_DELETE
    # 有 AI：point B into liked → unseen 命中 → like
    res1 = {r.item.id: r.decision for r in rank_items(
        [pos, unseen], {}, extra_liked={("actor", "B")})}
    assert res1["u1"] == DEC_LIKE


def test_prompt_contains_sections():
    p = {"n_pos": 10, "n_fav": 2, "n_played": 3, "unc_ratio": 0.5,
         "top_tags": [{"name": "巨乳", "weight": 100}],
         "top_actors": [{"name": "希咲那奈", "fav": 5}],
         "neg_features": [("prefix: OREMO", 3)]}
    txt = build_ai_prompt(p)
    assert "liked" in txt and "disliked" in txt and "kind" in txt
