"""评分引擎 - 纯函数层，与 Qt 解耦，可单元测试。

方法论（核心原则）：
- 未看 ≠ 要删。但"未看"不是"没有意见"的理由。
- 已看(收藏/看完/播放过) = 正样本，构建"喜欢特征集"（你从未喜欢的演员/厂商/类型…）。
- 对未看作品用【命中度】预测：
    它的内容特征里有多少比例命中过你的"喜欢特征集"。
    命中率越高 -> 越可能喜欢；命中率≈0 -> 大概率不喜欢（取立场，进建议删除）。
    没有特征(无元数据) 或 命中率居中 -> 才需要人工复核。
- 严禁按"是否看过/收藏"直接判删，但允许"内容零命中"作为删除证据。
- 补充特征：番号前缀(厂商)可当特征，解决"未知演员"作品的信息不足。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from utils.library import BUCKET_OTHER, BUCKET_TRAILER, Item

# 特征类别权重（基础；最终权重 = 类别基权 × TF-IDF 区分度）
FEAT_WEIGHT = {
    "actor": 3.0,
    "studio": 2.0,
    "prefix": 2.0,   # 番号前缀=厂商系列
    "genre": 2.0,
    "tag": 1.0,
    "uncensored": 0.3,
    "bucket": 0.0,   # 排除：全库共享，无判别力
    "year": 0.0,     # 排除
}

# 无信息占位特征（不能当作"你喜欢的特征"）
NOISE_FEATURES = {"未知演员", "未知女优"}
STUDIO_TAIL_CHARS = "|"
# 刮削垃圾 token（元数据标注：片商:/系列:），不算类型特征。
# 注意：不能一刀切"纯字母数字"，否则会误杀 MOODYZ/SOD 等真厂商名。
JUNK_TOKEN = re.compile(
    r"^(发行|片商|系列|主演|制作|标签|发行方|発行)\s*[:|]|"
    r"^\d{3,4}x\d{3,4}$"
)

# 通用技术标签（分辨率/编码/容器/字幕/码率状态/帧率/清晰度）——
# 它们出现在几乎每部作品上，不算"内容偏好"。既不能进喜欢特征集，
# 也不能进命中度分母（否则会稀释真实内容命中度，刷屏 Top 标签）。
# 无码/有码 已由 is_uncensored 特征(权重0.3)单独表达，此处一并剔除避免双重计分。
TECH_TOKEN = re.compile(
    r"^(?:"
    r"\d{2,4}\s*[pP]|"                 # 1080P 720P 480P 2160P
    r"[248]\s*[kK]|"                   # 4K 8K 2K
    r"h\s*\d{2,4}|"                    # H264 H265
    r"x\s*\d{3,4}|"                    # x264 x265
    r"hevc|avc|av1|mpeg4?|"            # 编码
    r"mp4|mkv|avi|ts|mov|wmv|flv|rmvb|m2ts|webm|vob|"   # 容器
    r"中字|字幕|外挂|内嵌|中文|无字幕|"  # 字幕/语言
    r"无码|有码|薄码|"                   # 码率状态
    r"\d+\s*fps|"                       # 帧率
    r"hd|sd|fhd|uhd|full\s*hd|高清|超清"  # 清晰度
    r")$",
    re.IGNORECASE,
)


def _clean_value(v: str) -> str:
    return v.strip().strip(STUDIO_TAIL_CHARS).strip()


def _is_junk_token(v: str) -> bool:
    if JUNK_TOKEN.search(v):
        return True
    if TECH_TOKEN.match(v):
        return True
    return ":" in v or "|" in v


# 人工标签取值
LABLE_KEEP = "KEEP"
LABEL_LIKE = "PLAYED_LIKE"
LABEL_DISLIKE = "DISLIKE"
LABEL_DELETE = "DELETE"
LABEL_REVIEW = "REVIEW"
NEGATIVE_LABELS = {LABEL_DISLIKE, LABEL_DELETE}
POSITIVE_LABELS = {LABLE_KEEP, LABEL_LIKE}

# 决策
DEC_KEEP = "keep"
DEC_LIKE = "like"
DEC_DELETE = "delete"
DEC_REVIEW = "review"

# 特征进入"喜欢特征集"的最低累积行为权重（>=1.5：1 个收藏，或 2 次看完等）
LIKED_THRESHOLD = 1.5
# 特征进入"不喜欢特征集"的最低权重
DISLIKED_THRESHOLD = 0.5

# 默认参数
DEFAULT_WEIGHTS = {"content": 1.0, "quality": 0.1, "dedup": 1.0}
DEFAULT_TH_KEEP = 0.55      # 命中度 >= 此 -> 可能喜欢
DEFAULT_TH_DELETE = 0.25    # 命中度 <= 此 -> 建议删除（取立场）


@dataclass
class Scored:
    item: Item
    s_content: float = 0.0     # S1 命中度 0..1
    s_behavior: float = 0.0    # S2 行为真值分
    s_quality: float = 0.5     # S3 质量辅助
    s_dedup: float = 0.0       # S4 去重惩罚
    final: float = 0.0
    decision: str = DEC_REVIEW
    featureless: bool = False  # 无任何可用特征 -> 只能人工
    is_junk: bool = False      # 垃圾占位文件(trailer/fanart/极小文件)
    reason: str = ""           # 判定理由（给用户看）

    @property
    def is_seen(self) -> bool:
        return self.s_behavior > 0


KIND_CN = {"actor": "演员", "studio": "厂商", "genre": "类型", "tag": "标签"}


def _feat_names(feats: dict, wanted: set, n: int = 4) -> list[str]:
    out = []
    for (kind, val) in feats:
        if (kind, val) in wanted:
            out.append(f"{KIND_CN.get(kind, kind)} {val}")
            if len(out) >= n:
                break
    return out


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def is_junk_item(it: Item) -> bool:
    """预告/占位/附属文件（trailer、fanart、theme 片头、极小文件）→ 直接建议删除，不参与品味判断。"""
    from utils.library import is_junk_attachment_path
    # 优先复用基于"文件名/路径"的附属判定（与修复转码页共用同一套规则，避免漂移）
    if is_junk_attachment_path(it.path or "", it.name or ""):
        return True
    # 极小文件（<10MB）视为占位/缩略图，不参与品味判断
    if it.size is not None and 0 < it.size < 10 * 1024 * 1024:
        return True
    return False


def num_prefix(it: Item) -> Optional[str]:
    if not it.num:
        return None
    p = it.num.split("-")[0]
    return p if p else None


def _split_tokens(values) -> list[str]:
    """把原始字段拆成干净 token：逗号/管道符都拆，去除垃圾 token 与噪声。"""
    out = []
    for v in values:
        for piece in re.split(r"[|,]", str(v)):
            piece = _clean_value(piece)
            if piece and not _is_junk_token(piece):
                out.append(piece)
    return out


def _feat_values(it: Item):
    """清洗后的特征列表。排除 未知演员、垃圾 token；按 | 与 , 双重拆分。

    genre/tag 额外做同义合并(canon_tag，中出し→中出 等)，让画像/命中度用规范词。
    studio/actor 保持原样（厂商/演员名不做合并）。
    """
    from utils.actor_clean import clean_actors
    from utils.tag_norm import filter_content_tags, prioritize_tags

    out = []
    actors = [a for a in clean_actors(it.actors) if a not in NOISE_FEATURES]
    studios = _split_tokens(it.studios)
    genres = filter_content_tags(_split_tokens(it.genres))
    tags = prioritize_tags(_split_tokens(it.tags))
    out.append(("actor", actors))
    out.append(("studio", studios))
    pfx = num_prefix(it)
    if pfx:
        out.append(("prefix", [pfx]))
    out.append(("genre", genres))
    out.append(("tag", tags))
    if it.is_uncensored:
        out.append(("uncensored", ["1"]))
    return out


def item_features(it: Item) -> dict[tuple, float]:
    """特征 -> 权重 的原始映射（不归一化）。"""
    feats: dict[tuple, float] = {}
    for kind, values in _feat_values(it):
        w = FEAT_WEIGHT.get(kind, 1.0)
        for v in values:
            key = (kind, v)
            feats[key] = feats.get(key, 0.0) + w
    return feats


def sample_weight(it: Item) -> float:
    """行为真值分（未看=0）。"""
    w = 0.0
    if it.is_favorite:
        w += 2.0
    if it.is_played:
        w += 1.0
    elif it.has_progress:
        w += 0.6
    elif it.play_count > 0:
        w += 0.3
    return w


def quality_score(it: Item) -> float:
    base = 0.5
    if it.size is not None and it.size > 0:
        mb = it.size / (1024 * 1024)
        if mb < 150:
            base -= 0.1
        elif mb > 1200:
            base += 0.1
    return base


# ── 去重（同一番号，真重复时标记较差版本为 loser）──────────
def group_losers(grp: list[Item]) -> set[str]:
    """组内判定应删的差版本 id。CD 全分卷组 = 同一作品分集，一律不删。"""
    if all(i.is_cd for i in grp):
        return set()

    def _rank(it: Item) -> tuple:
        return (1 if it.is_uncensored else 0,
                0 if it.is_cd else 1,
                it.size or 0,
                it.year or 0)

    best = max(grp, key=_rank)
    return {it.id for it in grp if it is not best}


def dedup_loser_ids(items: list[Item]) -> set[str]:
    losers: set[str] = set()
    for _num, grp in _dedup_groups_raw(items):
        losers |= group_losers(grp)
    return losers


def _dedup_groups_raw(items: list[Item]):
    """按番号分组（不含 trailer/other/附属文件），仅返回 >=2 的组。

    排除 is_junk_item：theme/fanart/backdrop/trailer/极小占位文件——它们不是作品，
    不应作为"同一番号的版本"参与去重比较（否则会把 Jellyfin 生成的片头/背景
    当成某个版本的"重复项"而误删正片）。
    """
    groups: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if is_junk_item(it):
            continue
        if it.num and it.bucket not in (BUCKET_TRAILER, BUCKET_OTHER):
            groups[it.num].append(it)
    return [(num, grp) for num, grp in groups.items() if len(grp) >= 2]


def dedup_groups(items: list[Item]) -> list[dict]:
    """重复版本分组：供去重页展示。

    Returns:
        [{"num": 番号, "items": [Item...], "losers": {id...}, "all_cd": bool,
          "cd_index": {item_id: cd号}}]
    """
    out = []
    for num, grp in _dedup_groups_raw(items):
        out.append({
            "num": num,
            "items": grp,
            "losers": group_losers(grp),
            "all_cd": all(i.is_cd for i in grp),
        })
    return out


# ── 命中度评分 ─────────────────────────────
def _build_feature_sets_from_map(items: list[Item], labels: dict[str, str],
                                 feats_map: dict[str, dict],
                                 extra_liked=(), extra_disliked=(),
                                 idf: Optional[dict] = None):
    """从已看正样本 + 人工负样本，产出 喜欢/不喜欢 特征集（特征向量缓存复用）。

    extra_liked/extra_disliked：AI 参与分类时提炼的标签 key（(kind,val) 集合），
    直接点亮进喜欢/不喜欢集——不引入新权重、不改变原始相似度算法，
    只有当某部作品自身带该标签时才参与命中。
    """
    pos_acc: dict[tuple, float] = defaultdict(float)
    neg_acc: dict[tuple, float] = defaultdict(float)

    def _w(k, fw):
        return fw * (idf.get(k, 1.0) if idf else 1.0)

    for it in items:
        lab = labels.get(it.id)
        w = sample_weight(it)
        feats = feats_map[it.id]
        if lab in NEGATIVE_LABELS:
            for k, fw in feats.items():
                neg_acc[k] += _w(k, fw)
        elif w > 0:
            for k, fw in feats.items():
                pos_acc[k] += _w(k, fw) * w

    liked = {k for k, v in pos_acc.items() if v >= LIKED_THRESHOLD} | set(extra_liked)
    disliked = {k for k, v in neg_acc.items() if v >= DISLIKED_THRESHOLD} | set(extra_disliked)
    return liked, disliked


def idf_weights(feats_map: dict[str, dict]) -> dict[tuple, float]:
    """TF-IDF 逆文档频率：idf(t)=log((1+N)/(1+df_t))+1。

    出现越多的特征(通用词,df 高)权重越低、稀有偏好(df 低)权重越高——
    解决"中出/巨乳 等通用标签权重过高，稀释真实内容命中度"。
    """
    n = max(1, len(feats_map))
    df: dict[tuple, int] = defaultdict(int)
    for feats in feats_map.values():
        for k in feats:
            df[k] += 1
    return {k: math.log((1 + n) / (1 + d)) + 1.0 for k, d in df.items()}


def coverage_score(feats: dict[tuple, float], liked: set, disliked: set,
                   idf: Optional[dict] = None) -> Optional[float]:
    """命中度 = (命中喜欢的特征权重 - 0.5*命中不喜欢的) / 全部特征权重。

    权重乘 TF-IDF 区分度（idf 传 None 时等效原逻辑，向后兼容）。
    返回 None 表示无特征（信息不足，只能人工）。
    """
    total = sum(w * (idf.get(k, 1.0) if idf else 1.0) for k, w in feats.items())
    if total <= 0:
        return None
    pos_w = sum(w * (idf.get(k, 1.0) if idf else 1.0)
                for k, w in feats.items() if k in liked)
    neg_w = sum(w * (idf.get(k, 1.0) if idf else 1.0)
                for k, w in feats.items() if k in disliked)
    return _clamp01((pos_w - 0.5 * neg_w) / total)


def rank_items(
    items: list[Item],
    labels: dict[str, str],
    weights: Optional[dict] = None,
    th_keep: Optional[float] = None,
    th_delete: Optional[float] = None,
    extra_liked=(),
    extra_disliked=(),
    enhance: bool = True,
) -> list[Scored]:
    """对全库排序并分层决策。labels: {item_id: LABLE_*}。

    extra_liked/extra_disliked：AI 参与分类提炼的标签 key，并入 liked/disliked 集合。
    enhance=True：叠加「相似锚点(质心余弦) + 题材共现」增量，供与关掉对比。
    """
    W = weights or DEFAULT_WEIGHTS
    wc = W.get("content", 1.0)
    wq = W.get("quality", 0.1)
    wd = W.get("dedup", 1.0)
    thk = DEFAULT_TH_KEEP if th_keep is None else th_keep
    thd = DEFAULT_TH_DELETE if th_delete is None else th_delete

    # 特征向量只算一次，供画像与逐条评分复用
    feats_map: dict[str, dict] = {it.id: item_features(it) for it in items}
    idf = idf_weights(feats_map)
    liked, disliked = _build_feature_sets_from_map(
        items, labels, feats_map, extra_liked=extra_liked,
        extra_disliked=extra_disliked, idf=idf)
    losers = dedup_loser_ids(items)

    # 增强用：正样本质心（Rocchio 弱化版）、题材共现对、特征→代表正样本
    pos_centroid: dict[tuple, float] = defaultdict(float)
    coocc_set: set = set()
    feat_anchor: dict = {}
    if enhance:
        for it in items:
            if labels.get(it.id) in NEGATIVE_LABELS or sample_weight(it) <= 0:
                continue
            f = feats_map[it.id]
            for k, w in f.items():
                pos_centroid[k] += w
                if w >= feat_anchor.get(k, (None, 0))[1]:
                    feat_anchor[k] = (it.num or "", w)
            keys = list(f)
            for a in range(len(keys)):
                for b in range(a + 1, len(keys)):
                    coocc_set.add((keys[a], keys[b]))
    norm_pc = math.sqrt(sum(v * v for v in pos_centroid.values()))

    out: list[Scored] = []
    for it in items:
        lab = labels.get(it.id)
        behavior = sample_weight(it)
        sq = quality_score(it)
        sd = 0.35 if it.id in losers else 0.0

        # 垃圾占位文件：直接建议删除（已看过/手动保留的除外）
        if behavior == 0 and is_junk_item(it) and lab not in POSITIVE_LABELS:
            out.append(_mk(it, 0.0, behavior, sq, sd, -0.5, DEC_DELETE,
                           featureless=True, is_junk=True,
                           reason="Jellyfin 附属文件（trailer/theme/缩略图），非作品"))
            continue

        if lab in POSITIVE_LABELS:
            out.append(_mk(it, 1.0, behavior, sq, 0.0, 2.0, DEC_KEEP,
                           reason="你已手动标记保留"))
            continue
        if lab in NEGATIVE_LABELS:
            out.append(_mk(it, 0.0, behavior, sq, 0.0, -1.0, DEC_DELETE,
                           reason="你已手动标记删除"))
            continue

        feats = feats_map[it.id]
        cov = coverage_score(feats, liked, disliked, idf=idf)
        featureless = cov is None
        # 无任何"有判别力"特征（只有年份/分桶等权重0项）→ 信息不足，不可判删
        has_content = any(FEAT_WEIGHT.get(k[0], 0) > 0 for k in feats)

        kv = cov
        anchor_note = ""
        if enhance and not featureless:
            # 增强：相似锚点(质心余弦) + 题材共现(候选 liked 特征对的既有共现占比)
            rn = math.sqrt(sum(w * w for w in feats.values())) or 1e-9
            pc = norm_pc or 1e-9
            dot = sum(w * pos_centroid.get(k, 0.0) for k, w in feats.items())
            anchor_j = max(0.0, dot / (rn * pc))
            keys = [k for k in feats if k in liked]
            co = 0.0
            if len(keys) >= 2:
                tot = len(keys) * (len(keys) - 1) // 2
                pairs = sum(1 for a in range(len(keys)) for b in range(a + 1, len(keys))
                            if (keys[a], keys[b]) in coocc_set)
                co = pairs / tot
            kv = _clamp01(cov + 0.25 * anchor_j + 0.10 * co)
            if keys and anchor_j >= 0.15:
                best = max((feat_anchor.get(k, ("", 0.0)) for k in keys), key=lambda t: t[1])
                if best[0]:
                    anchor_note = f"；⟶ 最像你喜欢的 {best[0]}（整体相似 {anchor_j:.0%}）"

        if behavior > 0:
            decision, final = DEC_KEEP, 1.0 + min(behavior, 1.0)
            reason = "已看过/收藏（行为真值）"
        elif featureless or not has_content:
            decision, final = DEC_REVIEW, 0.0
            reason = "无可用元数据（演员/类型/标签均缺失），无法预测"
        elif it.is_cd:
            # CD 分卷 = 同一作品分集，不能凭内容自动删，交给人/去重页整体处理
            decision, final = DEC_REVIEW, wc * cov + wq * sq - wd * sd
            reason = "CD 分卷（同一作品多集），需整体判断"
        elif kv >= thk:
            decision, final = DEC_LIKE, wc * kv + wq * sq - wd * sd
            _m = _feat_names(feats, liked)
            reason = f"命中度 {cov:.0%}" + (f"：命中你喜欢的 { '、'.join(_m) }" if _m else "，与你喜欢的内容高度相似") + anchor_note
        elif kv <= thd:
            decision, final = DEC_DELETE, wc * kv + wq * sq - wd * sd
            _d = _feat_names(feats, disliked)
            reason = f"命中度仅 {cov:.0%}，与你喜欢的内容几乎无交集"
            if _d:
                reason += f"；且命中你不喜欢：{ '、'.join(_d) }"
            if sd:
                reason += "；且为重复差版本"
            # 附上候选自身的元数据（供人工判断：无交集≠没信息）
            _ci = []
            if it.actors:
                _ci.append("演员:" + ",".join(str(x) for x in it.actors[:3]))
            if it.genres:
                _ci.append("类型:" + ",".join(str(x) for x in it.genres[:2]))
            if it.tags:
                _ci.append("标签:" + ",".join(str(x) for x in it.tags[:4]))
            if it.size_mb:
                _ci.append(f"{it.size_mb}MB")
            if it.year:
                _ci.append(f"{it.year}年")
            if _ci:
                reason += "｜候选内容: " + " ".join(_ci)
            else:
                reason += "｜候选无任何元数据(仅标题)，可打开核对"
        else:
            decision, final = DEC_REVIEW, wc * kv + wq * sq - wd * sd
            reason = f"命中度 {cov:.0%}，介于两者之间，需要你判断" + anchor_note

        out.append(_mk(it, cov if cov is not None else 0.0, behavior, sq, sd,
                       final, decision, featureless=featureless, reason=reason))
    return out


def _mk(it, sc, sb, sq, sd, final, decision, featureless=False, is_junk=False,
        reason="") -> Scored:
    return Scored(
        item=it, s_content=round(sc, 3), s_behavior=round(sb, 2),
        s_quality=round(sq, 3), s_dedup=sd, final=round(final, 3),
        decision=decision, featureless=featureless, is_junk=is_junk, reason=reason,
    )


def decision_counts(results: list[Scored]) -> dict[str, int]:
    c = Counter(r.decision for r in results)
    return {
        "keep": c[DEC_KEEP],
        "like": c[DEC_LIKE],
        "delete": c[DEC_DELETE],
        "review": c[DEC_REVIEW],
        "featureless": sum(1 for r in results if r.featureless),
        "keep_seen_truth": sum(1 for r in results if r.decision == DEC_KEEP and r.is_seen),
    }
