"""scoring 评分引擎单元测试 - 未看≠删除，内容预测为主。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, Item  # noqa: E402
from utils.scoring import (  # noqa: E402
    DEC_DELETE, DEC_KEEP, DEC_LIKE, DEC_REVIEW,
    LABEL_DELETE, LABEL_DISLIKE,
    decision_counts, dedup_loser_ids, rank_items, sample_weight,
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
    # 一部"乱内容"但有收藏 -> 真值保留（不管内容分）
    liked = _mk("L", fav=True, actors=["X"])
    weird = _mk("W", actors=["ZZZZZ_unknown_QQ"])
    res = rank_items([liked, weird], {})
    d = {r.item.id: r.decision for r in res}
    assert d["L"] == DEC_KEEP
    # 已看(行为>0) === 保留，即使用户没收藏
    seen = _mk("S", played=True, actors=["abc"])
    res = rank_items([liked, seen], {})
    assert {r.item.id: r.decision for r in res}["S"] == DEC_KEEP


def test_unseen_prediction_like_vs_delete():
    # 正样本：喜欢演员"希咲那奈"与厂商"MOODYZ"
    pos = _mk("POS", fav=True, actors=["希咲那奈"], studios=["MOODYZ"], genres=["中出"])
    # 未看、但同演员同厂商 -> 可能喜欢
    similar = _mk("SIM", actors=["希咲那奈"], studios=["MOODYZ"], genres=["中出"])
    # 未看、特征与喜好零交集 -> 大概率不喜欢（取立场：建议删除）
    alien = _mk("ALIEN", actors=["AAA_unknown_x"], studios=["ZZZ_unknown_studio"],
                 genres=["QQQ_adult_kid"])
    # 未看、完全没有可用特征 -> 信息不足，只能待复核
    blank = _mk("BLANK", actors=[], studios=[], genres=(), tags=())
    res = rank_items([pos, similar, alien, blank], {})
    tab = {r.item.id: r for r in res}
    assert tab["SIM"].decision == DEC_LIKE
    assert tab["SIM"].s_content > 0.6
    assert tab["ALIEN"].decision == DEC_DELETE
    assert tab["ALIEN"].s_content < 0.21
    assert tab["BLANK"].decision == DEC_REVIEW
    assert tab["BLANK"].featureless


def test_negative_centroid_pulls_to_delete():
    # 正样本 + 一个负样本（人工标不喜欢：演员"乙里"）
    pos = _mk("POS", fav=True, actors=["希咲那奈"], studios=["MOODYZ"])
    neg = _mk("NEG", actors=["乙里"], studios=["SOD"])
    # 未看但与负样本同特征 -> 删除候选
    overlap_neg = _mk("OVL", actors=["乙里"], studios=["SOD"])
    res = rank_items([pos, neg, overlap_neg], {neg.id: LABEL_DELETE})
    tab = {r.item.id: r for r in res}
    assert tab["OVL"].decision == DEC_DELETE


def test_manual_negative_override():
    pos = _mk("POS", fav=True, actors=["阿"], studios=["S"])
    # 即使内容相似，人工标不喜欢 -> 删除
    sim = _mk("SIM", actors=["阿"], studios=["S"])
    labels_ = {sim.id: LABEL_DISLIKE}
    res = rank_items([pos, sim], labels_)
    tab = {r.item.id: r for r in res}
    assert tab["SIM"].decision == DEC_DELETE


def test_dedup_loser():
    coded = _mk("C", num="MIAA-743", bucket=BUCKET_JAV, size=500 * 1024 * 1024)
    unc = _mk("U", num="MIAA-743", bucket=BUCKET_JAV, unc=True, size=2000 * 1024 * 1024)
    losers = dedup_loser_ids([coded, unc])
    assert coded.id in losers and unc.id not in losers


def test_cd_kept_in_dedup():
    c1 = _mk("C1", num="FC2-123", bucket=BUCKET_JAV, cd=True)
    c2 = _mk("C2", num="FC2-123", bucket=BUCKET_JAV, cd=True)
    assert dedup_loser_ids([c1, c2]) == set()


def test_decision_counts():
    pos = _mk("P", fav=True)
    res = rank_items([pos], {})
    c = decision_counts(res)
    assert c["keep"] == 1 and c["review"] == 0
