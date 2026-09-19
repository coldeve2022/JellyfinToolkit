"""喜好画像 profile 纯函数单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, Item  # noqa: E402
from utils.profile import build_profile, profile_to_csv  # noqa: E402
from utils.scoring import LABEL_DELETE, rank_items  # noqa: E402


def _mk(id_, num, actors=(), studios=(), genres=(), tags=(), fav=False,
        played=False, unc=False, year=2022):
    return Item(id=id_, name=f"{num}-{id_}", path=f"D:\\{num}-{id_}.mp4",
                type_cn="Movie", year=year, genres=list(genres), tags=list(tags),
                studios=list(studios), actors=list(actors), size=100,
                num=num, bucket=BUCKET_JAV, is_uncensored=unc,
                is_favorite=fav, is_played=played)


def test_profile_tops_and_prefs():
    items = [
        _mk("p1", "P-1", actors=["希咲那奈", "乙アリス"], studios=["MOODYZ"],
            genres=["中出"], tags=["制服"], fav=True, played=True),
        _mk("p2", "P-2", actors=["希咲那奈"], studios=["MOODYZ"],
            genres=["中出"], played=True),
        _mk("p3", "P-3", actors=["完全陌生演员"], studios=["ZZZ"],
            genres=["未知题材"]),
    ]
    res = rank_items(items, {})
    p = build_profile(items, {}, res)
    assert p["n_pos"] == 2                       # 只看过两部
    assert p["n_fav"] == 1
    assert p["actors_covered"] == 2              # 希咲那奈 + 乙アリス
    assert p["top_actors"][0]["name"] == "希咲那奈"
    assert p["top_studios"][0]["name"] == "MOODYZ"
    assert p["top_genres"][0]["name"] == "中出"


def test_recs_cover_all_unseen_hits():
    # 推荐 = 画像特征反查全库未看（不再只取 like 筛漏网）
    pos = _mk("p1", "P-1", actors=["希咲那奈"], studios=["MOODYZ"],
              genres=["中出"], fav=True, played=True)
    sim = _mk("s1", "S-1", actors=["希咲那奈"], studios=["MOODYZ"], genres=["中出"])
    sim.size = 2_000_000_000   # 避免被误判为附属小文件
    res = rank_items([pos, sim], {})
    p = build_profile([pos, sim], {}, res)
    names = [r["name"] for r in p["recs"]]
    assert "S-1-s1" in names
    assert p["recs"][0]["hit"].endswith("%")


def test_recs_dedup_similar():
    # 多样性去重：一大批只靠同一弱特征(prefix/中出)命中的候选，不应全部进推荐
    pos = _mk("p1", "P-1", actors=["希咲那奈"], studios=["MOODYZ"],
              genres=["中出"], fav=True)
    items = [pos]
    for i in range(6):
        it = _mk(f"u{i}", f"OP-{i}", genres=["中出"])
        it.size = 2_000_000_000
        items.append(it)
    res = rank_items(items, {})
    p = build_profile(items, {}, res)
    # 这6部都只靠"中出"一个喜欢特征命中，与已选完全同类 → 只应保留极少数
    assert 0 <= len(p["recs"]) <= 2


def test_negative_features():
    neg = _mk("n1", "N-1", actors=["乙里"], studios=["SOD"])
    pos = _mk("p1", "P-1", actors=["希咲那奈"], studios=["MOODYZ"], fav=True)
    labels = {neg.id: LABEL_DELETE}
    p = build_profile([pos, neg], labels)
    assert p["n_neg"] == 1
    feats = dict(p["neg_features"])
    assert any(k.startswith("演员") and "乙里" in k for k in feats)


def test_csv_export_contains_sections():
    p = build_profile([_mk("p1", "P-1", actors=["甲"], studios=["S"], fav=True)], {})
    csv = profile_to_csv(p)
    assert "Top演员" in csv and "年份偏好" in csv and "为你推荐" in csv
