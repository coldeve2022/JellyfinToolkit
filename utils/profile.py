"""喜好画像 - 纯函数层，从行为信号与人工标签构建"你是什么口味"。

- 正样本：收藏/看完/播放过（排除被手动标"不喜欢/删除"的）
- 每个特征(演员/厂商/类型/标签) 累积 画像强度(行为权重)、收藏数、看过数
- 额外给出：年份偏好、无码偏好、不喜欢特征、画像强度、未看推荐
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional

from utils.library import Item
from utils.scoring import (
    DEC_DELETE, KIND_CN, NEGATIVE_LABELS, Scored, _feat_values, sample_weight,
    item_features, _build_feature_sets_from_map,
)

# Top 特征的最低行为门槛：至少"收藏1次 或 看完1部"才算真实口味，
# 剔除"播放过/有进度就标了一串标签"的凑数项（无收藏/看完，参考性差）。
_MIN_BEHAVIOR = 1
# 不喜欢特征（手动负样本 或 反向推断）最低出现次数，防单条噪声
_NEG_MIN_COUNT = 2


def _cap_vals(values, n=8):
    return [str(v) for v in values[:n]]


def build_profile(items: list[Item], labels: dict[str, str],
                  results: Optional[list[Scored]] = None) -> dict:
    """构建喜好画像。results 可选：用于"未看推荐 Top N"。"""
    pos = [it for it in items if sample_weight(it) > 0
           and labels.get(it.id) not in NEGATIVE_LABELS]
    neg = [it for it in items if labels.get(it.id) in NEGATIVE_LABELS]

    def stat():
        return {"fav": 0, "played": 0, "weight": 0.0}

    actors: dict[str, dict] = defaultdict(stat)
    studios: dict[str, dict] = defaultdict(stat)
    genres: dict[str, dict] = defaultdict(stat)
    tags: dict[str, dict] = defaultdict(stat)
    years: Counter = Counter()
    n_fav = n_played = n_unc = 0
    n_year_unknown = 0

    for it in pos:
        w = sample_weight(it)
        fav = 1 if it.is_favorite else 0
        played = 1 if it.is_played else 0
        n_fav += fav
        n_played += played
        if it.is_uncensored:
            n_unc += 1
        if it.year:
            years[it.year] += 1
        else:
            n_year_unknown += 1
        for kind, vals in _feat_values(it):
            if kind == "actor":
                for a in vals:
                    s = actors[a]
                    s["fav"] += fav
                    s["played"] += played
                    s["weight"] += w
            elif kind == "studio":
                for st in vals:
                    s = studios[st]
                    s["fav"] += fav
                    s["played"] += played
                    s["weight"] += w
            elif kind == "genre":
                for g in vals:
                    s = genres[g]
                    s["fav"] += fav
                    s["played"] += played
                    s["weight"] += w
            elif kind == "tag":
                for t in vals:
                    s = tags[t]
                    s["fav"] += fav
                    s["played"] += played
                    s["weight"] += w

    def top(d: dict, limit=15):
        return sorted(
            ({"name": k, **v} for k, v in d.items()
             if v["fav"] + v["played"] >= _MIN_BEHAVIOR),
            key=lambda x: (-x["weight"], -x["fav"]),
        )[:limit]

    # 不喜欢特征 = 手动负样本(人工标不喜欢/删除) + 反向推断(评分层判删的未看作品)
    # 反向推断依据：未看 + 内容与"喜欢特征集"零交集 -> 被系统建议删除，
    # 这类作品的共同特征即"你不喜欢的倾向"，无需等你手动标不喜欢。
    def _neg_common_name(kind: str) -> str:
        return KIND_CN.get(kind, kind)

    neg_feats: Counter = Counter()
    manual_keys: set[str] = set()     # 手动负样本特征无条件保留
    for it in neg:
        for kind, vals in _feat_values(it):
            if kind == "prefix":      # 系列/厂商前缀不算"内容不喜欢"，展示层剔除
                continue
            for v in vals:
                key = f"{_neg_common_name(kind)}: {v}"
                neg_feats[key] += 1
                manual_keys.add(key)

    if results:
        pos_ids = {it.id for it in pos}
        for r in results:
            it = r.item
            if r.is_seen or it.id in pos_ids or r.is_junk or r.featureless:
                continue
            if r.decision != DEC_DELETE:
                continue
            for kind, vals in _feat_values(it):
                if kind == "prefix":
                    continue
                for v in vals:
                    neg_feats[f"{_neg_common_name(kind)}: {v}"] += 1

    neg_features = [
        kv for kv in neg_feats.most_common(15)
        if kv[1] >= _NEG_MIN_COUNT or kv[0] in manual_keys
    ]

    # 未看推荐：画像特征反查全库未看，按【命中的画像特征绝对权重】取 Top20，并做多样性去重。
    # 排序用绝对权重(actor=3/studio=2/genre=2/tag=1)体现"你喜欢的特征越多越靠前"；
    # 多样性：若某候选与已推荐项命中过多相同的喜欢特征(高度同类/同系列)，则跳过，
    # 避免只有单一弱特征(如 HENTAI 的 prefix=OPIUMUD)就刷屏。
    # 排除 CD 分卷（同一作品多集，非独立作品）与附属文件（junk）。
    recs: list = []
    rec_pool: list = []
    if results:
        feats_map = {it.id: item_features(it) for it in items}
        liked, _ = _build_feature_sets_from_map(items, labels, feats_map)

        def _hit_weight(r) -> float:
            return sum(w for (k, v), w in feats_map.get(r.item.id, {}).items()
                       if (k, v) in liked)

        def _like_hits(r) -> set:
            return {k for k in feats_map.get(r.item.id, {}) if k in liked}

        cand = [
            r for r in results
            if not r.is_seen and not r.is_junk and not r.featureless
            and not r.item.is_cd
            and labels.get(r.item.id) not in NEGATIVE_LABELS
        ]
        # 完整候选池（供 UI「🎲 换一批」随机探索；hit 存浮点便于加权抽样）
        rec_pool = [{
            "num": r.item.num or "", "name": r.item.name, "hit": r.s_content,
            "reason": r.reason, "path": r.item.path,
            "actors": _cap_vals(r.item.actors), "studios": _cap_vals(r.item.studios),
            "genres": _cap_vals(r.item.genres, 4),
        } for r in cand]
        cand.sort(key=_hit_weight, reverse=True)
        chosen: list = []

        def _too_similar(h: set) -> bool:
            # 与任一已选推荐高度同类则跳过：命中特征被完全覆盖(单/弱特征如 prefix)，
            # 或 ≥70% 特征重叠(多特征同类)。防"同一系列/同一弱特征"刷屏。
            for ch in chosen:
                if not h or not ch:
                    continue
                inter = len(h & ch)
                if inter >= 1 and (inter >= min(len(h), len(ch))
                                   or inter / max(len(h), len(ch), 1) >= 0.7):
                    return True
            return False

        for r in cand:
            h = _like_hits(r)
            if _too_similar(h):
                continue
            chosen.append(h)
            recs.append({
                "num": r.item.num or "", "name": r.item.name,
                "hit": f"{r.s_content:.0%}",
                "reason": r.reason,
                "actors": _cap_vals(r.item.actors),
                "studios": _cap_vals(r.item.studios),
                "genres": _cap_vals(r.item.genres, 4),
                "path": r.item.path,
            })
            if len(recs) >= 20:
                break

    # 年份偏好：缺失年份单独归"未知年份"，不静默丢弃
    year_pref = sorted(years.items(), key=lambda kv: -kv[1])
    if n_year_unknown:
        year_pref.append(("未知年份", n_year_unknown))

    # AI 报告用：判别力数据——全库标签频率 vs 正样本中占比、未看池判定分层
    lib_tag: Counter = Counter()
    for it in items:
        for kind, vals in _feat_values(it):
            if kind == "tag":
                lib_tag.update(vals)
    pos_tag_n = {k: v["fav"] + v["played"] for k, v in tags.items()}
    unseen_decision = (Counter(r.decision for r in results if not r.is_seen)
                       if results else Counter())

    return {
        "n_pos": len(pos),
        "n_neg": len(neg),
        "n_fav": n_fav,
        "n_played": n_played,
        "n_unc": n_unc,
        "unc_ratio": round(n_unc / len(pos), 3) if pos else 0.0,
        "actors_covered": len(actors),
        "studios_covered": len(studios),
        "genres_covered": len(genres),
        "total": len(items),
        "unwatched": sum(1 for it in items if it.play_count == 0 and not it.is_played),
        "by_bucket": dict(Counter(it.bucket for it in items)),
        "top_actors": top(actors),
        "top_studios": top(studios),
        "top_genres": top(genres),
        "top_tags": top(tags),
        "year_pref": year_pref,
        "neg_features": neg_features,
        "recs": recs,
        "rec_candidates": rec_pool,
        "lib_tag_freq": lib_tag.most_common(12),
        "pos_tag_count": sorted(pos_tag_n.items(), key=lambda kv: -kv[1])[:12],
        "unseen_decision": dict(unseen_decision),
    }


def profile_to_csv(p: dict) -> str:
    """把画像导出为 CSV 文本。"""
    lines = ["字段,名称,强度,收藏,看过"]
    for label, key in [("Top演员", "top_actors"), ("Top厂商", "top_studios"),
                       ("Top类型", "top_genres"), ("Top标签", "top_tags")]:
        for r in p[key]:
            lines.append(f"{label},{r['name']},{r['weight']:.1f},{r['fav']},{r['played']}")
    lines.append("")
    lines.append("年份偏好")
    for y, n in p["year_pref"]:
        lines.append(f"{y},{n}")
    lines.append("")
    lines.append("不喜欢特征")
    for feat, n in p["neg_features"]:
        lines.append(f"{feat},{n}")
    lines.append("")
    lines.append("为你推荐(未看)")
    lines.append("番号,名称,命中度,演员,厂商")
    for r in p["recs"]:
        lines.append(f"{r['num']},{r['name']},{r['hit']},{'/'.join(r['actors'])},{'/'.join(r['studios'])}")
    return "\n".join(lines)
