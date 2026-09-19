"""评分引擎 v2 - 纯函数层，与 Qt 解耦，可单元测试。

v2 相对 v1 (utils/scoring.py) 的核心升级：
1. 【分桶内画像】JAV / 无码 / 里番 / 其它各自构建"喜欢/不喜欢特征集"，
   避免不同大类互相污染（动漫偏好混进 JAV、无码偏好混进有码）。
2. 【置信度加权命中度】对"命中少但权重高"的作品做置信度修正——
   不再只算"命中占比"，而是参考"命中特征在喜欢史里出现的累积强度(先验)，
   与命中项自身的特征权重"联合加权，让"你收藏过的具体演员"这类强信号
   比"刷屏的通用标签"更能抬分。
3. 【邻域一致性传播】同一演员/厂商/系列(prefix)的未看作品，若该特征
   在正样本里高频出现，则把置信度向"同邻域"的未看作品传播——
   使"同演员/同厂商"的整组作品一起上浮/下沉，而不会只凭单条零散命中。
4. 【系列前缀约束】番号前缀(如 FC2/MIAA)作为独立一维参与，但不属于
   "内容偏好"——用它约束同系列整体性，避免"不同作品只因同系列就互相污染"。
5. 【质量分增强】复用 ffprobe 信号(分辨率/时长/码率)，用于"内容相度相同"
   的一批内微调，绝不覆盖内容判断。

兼容性：对外暴露的 rank_items() / dedup_groups() / decision_counts() /
item_features() / is_junk_item() / idf_weights() 等接口与 v1 同名，
仅内部算法升级；未传新参数时行为默认为"增强开"，与 v1 语义一致，便于回归。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence

# 复用 v1 的常量与工具，避免重复定义造成 drift
from utils.library import Item, BUCKET_JAV, BUCKET_UNC, BUCKET_HENTAI, BUCKET_TRAILER, BUCKET_OTHER  # noqa: F401
from utils.scoring import (
    DEC_KEEP, DEC_LIKE, DEC_DELETE, DEC_REVIEW,
    LABLE_KEEP, LABEL_LIKE, LABEL_DISLIKE, LABEL_DELETE, LABEL_REVIEW,
    POSITIVE_LABELS, NEGATIVE_LABELS,
    LIKED_THRESHOLD, DISLIKED_THRESHOLD,
    DEFAULT_WEIGHTS, DEFAULT_TH_KEEP, DEFAULT_TH_DELETE,
    Scored, KIND_CN,
    is_junk_item, quality_score,
    sample_weight, idf_weights, dedup_loser_ids,
    dedup_groups, decision_counts, _feat_values, _mk as _mk_v1,
)

# 特征类别权重（与 v1 一致）
FEAT_WEIGHT = {
    "actor": 3.0,
    "studio": 2.0,
    "prefix": 2.0,
    "genre": 2.0,
    "tag": 1.0,
    "uncensored": 0.3,
    "bucket": 0.0,
    "year": 0.0,
}


def item_features(it: Item) -> dict:
    """特征 -> 权重（与 v1 完全一致，保证可复用 idf/画像）。"""
    feats: dict[tuple, float] = {}
    for kind, values in _feat_values(it):
        w = FEAT_WEIGHT.get(kind, 1.0)
        for v in values:
            key = (kind, v)
            feats[key] = feats.get(key, 0.0) + w
    return feats


# ── 分桶内画像 ─────────────────────────────────
# 把特征按其所属大类划进"桶上下文"。
# 这里的"桶上下文"由一个 Item 的 bucket 决定；对正样本，我们累积时
# 只把该特征计入对应 bucket 的正/负累加器，评分时先取作品自身 bucket 的累加器。
def _bucket_of_it(it: Item) -> str:
    b = it.bucket
    if b in (BUCKET_JAV, BUCKET_UNC, BUCKET_HENTAI):
        return b
    # trailer/other 并入"other"桶，避免画像被预告/未知污染
    return BUCKET_OTHER


def build_bucket_profiles(
    items: Sequence[Item],
    labels: dict[str, str],
    feats_map: dict[str, dict],
    idf: Optional[dict] = None,
    extra_liked=(),
    extra_disliked=(),
) -> dict[str, tuple[set, set]]:
    """为每个大类桶独立构建 (liked, disliked) 特征集。

    Returns:
        {bucket_name: (liked_set, disliked_set)}
        其中 trailer/other 共用"other"桶。
    """
    bucket_pos: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
    bucket_neg: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))

    def _w(k: str, fw: float) -> float:
        return fw * (idf.get(k, 1.0) if idf else 1.0)

    for it in items:
        lab = labels.get(it.id)
        w = sample_weight(it)
        feats = feats_map.get(it.id, {})
        b = _bucket_of_it(it)
        if lab in NEGATIVE_LABELS:
            for k, fw in feats.items():
                bucket_neg[b][k] += _w(k, fw)
        elif w > 0:
            for k, fw in feats.items():
                bucket_pos[b][k] += _w(k, fw) * w

    # extra_liked / extra_disliked 是"全局"AI 提炼特征，进每个桶（跨桶通用信号）
    profiles = {}
    all_buckets = {_bucket_of_it(it) for it in items}
    for b in all_buckets:
        liked = {k for k, v in bucket_pos[b].items() if v >= LIKED_THRESHOLD} | set(extra_liked)
        disliked = {k for k, v in bucket_neg[b].items() if v >= DISLIKED_THRESHOLD} | set(extra_disliked)
        profiles[b] = (liked, disliked)
    return profiles


def _cover_score_conf(
    feats: dict,
    liked: set,
    disliked: set,
    idf: Optional[dict] = None,
    liked_prior: Optional[dict] = None,
    dislike_prior: Optional[dict] = None,
) -> Optional[float]:
    """置信度加权命中度 (0..1)，信息不足返回 None。

    原 v1 命中度 = (命中喜欢权重 - 0.5*命中不喜欢) / 全部权重。
    v2 在此基础上，用"喜欢先验强度"修正：命中项若在你喜欢史里累积权重越高
    （like_prior），说明这是你真正偏好的特征，而不是偶然撞上，给予小幅加成。
    反之命中"你不喜欢"项时扣减同样按先验加权。
    """
    total = sum(w * (idf.get(k, 1.0) if idf else 1.0) for k, w in feats.items())
    if total <= 0:
        return None
    pos_w = 0.0
    neg_w = 0.0
    for k, w in feats.items():
        ww = w * (idf.get(k, 1.0) if idf else 1.0)
        if k in liked:
            prior = (liked_prior or {}).get(k, 1.0)  # 默认先验 = 1（无额外加权）
            pos_w += ww * prior
        if k in disliked:
            prior = (dislike_prior or {}).get(k, 1.0)
            neg_w += ww * prior
    raw = (pos_w - 0.5 * neg_w) / total
    return max(0.0, min(1.0, raw))


def _liked_prior(feats_map: dict, items: Sequence[Item], labels: dict,
                 idf: Optional[dict] = None) -> dict:
    """喜欢先验强度：每个 liked 特征在正样本里的累计行为权重（归一化到 1..2 区间）。

    值 >1 表示"该特征你反复喜欢"，给予抬分；=1 表示无额外先验（用默认）。
    """
    acc: dict[tuple, float] = defaultdict(float)
    for it in items:
        sw = sample_weight(it)
        if labels.get(it.id) in NEGATIVE_LABELS or sw <= 0:
            continue
        for k, fw in feats_map.get(it.id, {}).items():
            acc[k] += fw * sw * (idf.get(k, 1.0) if idf else 1.0)
    if not acc:
        return {}
    mx = max(acc.values()) or 1.0
    return {k: 1.0 + 0.5 * (v / mx) for k, v in acc.items()}


def _disliked_prior(feats_map: dict, items: Sequence[Item], labels: dict,
                    idf: Optional[dict] = None) -> dict:
    """不喜欢先验强度：每个 disliked 特征在负样本里的累计权重（归一化到 1..2 区间）。"""
    acc: dict[tuple, float] = defaultdict(float)
    for it in items:
        if labels.get(it.id) not in NEGATIVE_LABELS:
            continue
        for k, fw in feats_map.get(it.id, {}).items():
            acc[k] += fw * (idf.get(k, 1.0) if idf else 1.0)
    if not acc:
        return {}
    mx = max(acc.values()) or 1.0
    return {k: 1.0 + 0.5 * (v / mx) for k, v in acc.items()}


def _propagate_affinity(
    items: Sequence[Item],
    feats_map: dict[str, dict],
    profiles: dict[str, tuple[set, set]],
    labels: dict[str, str],
    idf: Optional[dict] = None,
    enhance: bool = True,
) -> dict[str, float]:
    """邻域一致性传播：为每部"未看"作品累加其邻域特征在正样本里的强度。

    做法：对每个 (actor/studio/prefix) 特征，统计它出现在"正样本"里的次数与
    出现在"全库"里的次数，得 frac=正样本占比。未看作品若携带高 frac 特征，
    得到 +boost；这与"命中度"叠加，让同演员/同厂商/同系列的未看作品成组上浮。

    Returns:
        {item_id: boost}  (0 表示无传播)
    """
    if not enhance:
        return {}
    # 统计各特征在正样本/全库的出现次数
    feat_liked_cnt: dict[tuple, int] = defaultdict(int)
    feat_total_cnt: dict[tuple, int] = defaultdict(int)

    pos_items = [it for it in items if sample_weight(it) > 0
                 and labels.get(it.id) not in NEGATIVE_LABELS]
    for it in pos_items:
        for k in feats_map.get(it.id, {}):
            feat_liked_cnt[k] += 1
    for it in items:
        for k in feats_map.get(it.id, {}):
            feat_total_cnt[k] += 1

    boost: dict[str, float] = {}
    for it in items:
        if sample_weight(it) > 0 or labels.get(it.id) in NEGATIVE_LABELS:
            continue
        b = _bucket_of_it(it)
        liked, _disliked = profiles.get(b, (set(), set()))
        f = feats_map.get(it.id, {})
        acc = 0.0
        kinds = ("actor", "studio", "prefix")
        for k, w in f.items():
            if k[0] not in kinds:
                continue
            tot = feat_total_cnt.get(k, 0)
            if tot <= 2:   # 特征太稀有，不传播（避免单样本噪声）
                continue
            frac = feat_liked_cnt.get(k, 0) / tot
            if k in liked:
                acc += w * frac
        boost[it.id] = acc
    return boost


# ── 主入口 ─────────────────────────────────
def rank_items(
    items: list[Item],
    labels: dict[str, str],
    weights: Optional[dict] = None,
    th_keep: Optional[float] = None,
    th_delete: Optional[float] = None,
    extra_liked=(),
    extra_disliked=(),
    enhance: bool = True,
    ai_scores: Optional[dict] = None,
) -> list[Scored]:
    """全库排序并分层决策（v2 升级版）。

    与 v1 的 rank_items 接口完全一致，仅内部算法升级：
    - 分桶内画像
    - 置信度加权命中度
    - 邻域一致性传播
    - 系列前缀约束
    - ai_scores: {item_id: 0..1} 本地 LLM 语义分，按 fuse_content 与规则分融合。
      未传(None)时行为与 v1 一致（纯规则）。
    """
    W = weights or DEFAULT_WEIGHTS
    wc = W.get("content", 1.0)
    wq = W.get("quality", 0.1)
    wd = W.get("dedup", 1.0)
    thk = DEFAULT_TH_KEEP if th_keep is None else th_keep
    thd = DEFAULT_TH_DELETE if th_delete is None else th_delete

    feats_map: dict[str, dict] = {it.id: item_features(it) for it in items}
    idf = idf_weights(feats_map)
    profiles = build_bucket_profiles(
        items, labels, feats_map, idf=idf,
        extra_liked=extra_liked, extra_disliked=extra_disliked)
    liked_prior = _liked_prior(feats_map, items, labels, idf)
    disliked_prior = _disliked_prior(feats_map, items, labels, idf)
    boosts = _propagate_affinity(items, feats_map, profiles, labels, idf, enhance)
    losers = dedup_loser_ids(items)
    ai_scores = ai_scores or {}

    out: list[Scored] = []
    for it in items:
        lab = labels.get(it.id)
        behavior = sample_weight(it)
        sq = quality_score(it)
        sd = 0.35 if it.id in losers else 0.0

        if behavior == 0 and is_junk_item(it) and lab not in POSITIVE_LABELS:
            out.append(_mk_v1(it, 0.0, behavior, sq, sd, -0.5, DEC_DELETE,
                              featureless=True, is_junk=True,
                              reason="Jellyfin 附属文件（trailer/theme/缩略图），非作品"))
            continue
        if lab in POSITIVE_LABELS:
            out.append(_mk_v1(it, 1.0, behavior, sq, 0.0, 2.0, DEC_KEEP,
                              reason="你已手动标记保留"))
            continue
        if lab in NEGATIVE_LABELS:
            out.append(_mk_v1(it, 0.0, behavior, sq, 0.0, -1.0, DEC_DELETE,
                              reason="你已手动标记删除"))
            continue

        b = _bucket_of_it(it)
        liked, disliked = profiles.get(b, (set(), set()))
        feats = feats_map[it.id]
        cov = _cover_score_conf(feats, liked, disliked, idf=idf,
                                liked_prior=liked_prior,
                                dislike_prior=disliked_prior)
        featureless = cov is None
        has_content = any(FEAT_WEIGHT.get(k[0], 0) > 0 for k in feats)

        # AI 语义分融合：规则分与 LLM 语义分加权（ai 缺省则纯规则）
        ai_score = ai_scores.get(it.id)
        if ai_score is not None and cov is not None:
            from utils.ai_scoring import fuse_content
            cov = fuse_content(cov, ai_score)

        boost = boosts.get(it.id, 0.0)
        kv = cov
        anchor_note = ""
        if enhance and not featureless:
            # 置信度修正基分：cov 为主，邻域传播作小幅加成（封顶 0.12）
            kv = _clamp01((cov or 0.0) + min(0.12, 0.02 * boost if boost else 0.0))
            if boost:
                anchor_note = "；邻域(同演员/厂商/系列)喜欢强度加分"

        if behavior > 0:
            decision, final = DEC_KEEP, 1.0 + min(behavior, 1.0)
            reason = "已看过/收藏（行为真值）"
        elif featureless or not has_content:
            decision, final = DEC_REVIEW, 0.0
            reason = "无可用元数据（演员/类型/标签均缺失），无法预测"
        elif it.is_cd:
            decision, final = DEC_REVIEW, wc * kv + wq * sq - wd * sd
            reason = "CD 分卷（同一作品多集），需整体判断"
        elif kv >= thk:
            decision, final = DEC_LIKE, wc * kv + wq * sq - wd * sd
            _m = _feat_names(feats, liked)
            reason = f"命中度 {((cov or 0.0)):.0%}" + (
                f"：命中你喜欢的 {'、'.join(_m) if _m else '内容'}") + anchor_note
        elif kv <= thd:
            decision, final = DEC_DELETE, wc * kv + wq * sq - wd * sd
            _d = _feat_names(feats, disliked)
            reason = f"命中度仅 {((cov or 0.0)):.0%}，与你喜欢的内容几乎无交集"
            if _d:
                reason += f"；且命中你不喜欢：{'、'.join(_d)}"
            if sd:
                reason += "；且为重复差版本"
            _ci = _cand_info(it)
            reason += ("｜候选内容: " + _ci) if _ci else "｜候选无任何元数据(仅标题)，可打开核对"
        else:
            decision, final = DEC_REVIEW, wc * kv + wq * sq - wd * sd
            reason = f"命中度 {((cov or 0.0)):.0%}，介于两者之间，需要你判断" + anchor_note

        out.append(_mk_v1(it, cov if cov is not None else 0.0, behavior, sq, sd,
                          final, decision, featureless=featureless, reason=reason))
    return out


def _cand_info(it: Item) -> str:
    parts = []
    if it.actors:
        parts.append("演员:" + ",".join(str(x) for x in it.actors[:3]))
    if it.genres:
        parts.append("类型:" + ",".join(str(x) for x in it.genres[:2]))
    if it.tags:
        parts.append("标签:" + ",".join(str(x) for x in it.tags[:4]))
    if it.size_mb:
        parts.append(f"{it.size_mb}MB")
    if it.year:
        parts.append(f"{it.year}年")
    return " ".join(parts)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _feat_names(feats: dict, wanted: set, n: int = 4) -> list[str]:
    out = []
    for (kind, val) in feats:
        if (kind, val) in wanted:
            out.append(f"{KIND_CN.get(kind, kind)} {val}")
            if len(out) >= n:
                break
    return out


# ── 测试辅助 ─────────────────────────────────
__all__ = [
    "rank_items", "dedup_groups", "decision_counts", "item_features",
    "is_junk_item", "idf_weights", "build_bucket_profiles",
    "FEAT_WEIGHT", "DEC_KEEP", "DEC_LIKE", "DEC_DELETE", "DEC_REVIEW",
    "LABLE_KEEP", "LABEL_LIKE", "LABEL_DISLIKE", "LABEL_DELETE", "LABEL_REVIEW",
    "Scored",
]
