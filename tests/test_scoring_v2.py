"""scoring_v2 评分引擎单元测试 - 分桶画像 + 置信度传播。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, BUCKET_UNC, BUCKET_HENTAI, Item  # noqa: E402
from utils.scoring_v2 import (  # noqa: E402
    DEC_DELETE, DEC_KEEP, DEC_LIKE, DEC_REVIEW,
    LABEL_DELETE,
    decision_counts, rank_items, build_bucket_profiles,
    item_features,
)


def _mk(name="N", id_=None, bucket=BUCKET_JAV, actors=None, studios=None,
        genres=(), tags=(), year=2022, size=None,
        fav=False, played=False, progress=False, pic=0, num=None,
        unc=False, cd=False):
    return Item(
        id=id_ or name, name=name, path=f"{name}.mp4", type_cn="Movie",
        year=year, genres=list(genres), tags=list(tags),
        studios=list(studios or ()), actors=list(actors or ()),
        size=size, num=num, is_cd=cd, bucket=bucket, is_uncensored=unc,
        is_favorite=fav, is_played=played, has_progress=progress,
        play_count=pic,
    )


def test_behavior_truth_overrides_content():
    liked = _mk("L", fav=True, actors=["X"])
    res = rank_items([liked], {})
    assert {r.item.id: r.decision for r in res}["L"] == DEC_KEEP


def test_unseen_prediction():
    pos = _mk("POS", fav=True, actors=["希咲那奈"], studios=["MOODYZ"], genres=["中出"])
    similar = _mk("SIM", actors=["希咲那奈"], studios=["MOODYZ"], genres=["中出"])
    alien = _mk("ALIEN", actors=["AAA_unknown_x"], studios=["ZZZ_unknown_studio"],
                 genres=["QQQ_adult_kid"])
    blank = _mk("BLANK", actors=[], studios=[], genres=(), tags=())
    res = rank_items([pos, similar, alien, blank], {})
    tab = {r.item.id: r for r in res}
    assert tab["SIM"].decision == DEC_LIKE
    assert tab["SIM"].s_content > 0.6
    assert tab["ALIEN"].decision == DEC_DELETE
    assert tab["BLANK"].decision == DEC_REVIEW
    assert tab["BLANK"].featureless


def test_bucket_separation():
    """分桶画像：JAV 的正样本不应污染无码桶的判定。"""
    # JAV 正样本：喜欢演员"希咲那奈"（只在该桶内）
    jav_pos = _mk("JP", bucket=BUCKET_JAV, fav=True, actors=["希咲那奈"])
    # 无码桶的未看：同演员但属于无码桶——分桶后不该被 JAV 桶的画像误抬分
    unc_same_actor = _mk("US", bucket=BUCKET_UNC, actors=["希咲那奈"], unc=True)
    # 无码桶的空作品
    unc_blank = _mk("UB", bucket=BUCKET_UNC, actors=[], genres=())
    items = [jav_pos, unc_same_actor, unc_blank]
    res = rank_items(items, {})
    tab = {r.item.id: r for r in res}
    # 分桶后，无码桶用无码桶的画像（这里无正样本），所以同演员不再被抬到 LIKE
    assert tab["US"].decision != DEC_LIKE
    assert tab["UB"].decision == DEC_REVIEW


def test_bucket_profiles_built():
    jav = _mk("A", bucket=BUCKET_JAV, fav=True, actors=["X"], genres=["中出"])
    unc = _mk("B", bucket=BUCKET_UNC, fav=True, actors=["Y"], unc=True)
    items = [jav, unc]
    feats = {it.id: item_features(it) for it in items}
    profiles = build_bucket_profiles(items, {}, feats)
    assert BUCKET_JAV in profiles
    assert BUCKET_UNC in profiles
    # JAV 桶 liked 应有 "X"，无码桶 liked 应有 "Y"
    jav_liked, _ = profiles[BUCKET_JAV]
    unc_liked, _ = profiles[BUCKET_UNC]
    assert ("actor", "X") in jav_liked
    assert ("actor", "Y") in unc_liked
    assert ("actor", "X") not in unc_liked


def test_manual_negative_override():
    pos = _mk("POS", fav=True, actors=["阿"], studios=["S"])
    sim = _mk("SIM", actors=["阿"], studios=["S"])
    res = rank_items([pos, sim], {sim.id: LABEL_DELETE})
    tab = {r.item.id: r for r in res}
    assert tab["SIM"].decision == DEC_DELETE


def test_junk_detected():
    junk = _mk("J", num="fanart1", size=5 * 1024 * 1024)
    res = rank_items([junk], {})
    tab = {r.item.id: r for r in res}
    assert tab["J"].decision == DEC_DELETE
    assert tab["J"].is_junk


def test_decision_counts():
    pos = _mk("P", fav=True)
    res = rank_items([pos], {})
    assert decision_counts(res)["keep"] == 1


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
