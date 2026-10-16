"""One evidence snapshot for workers, CLI and automation. Pure business layer, no Qt."""
from __future__ import annotations

from utils.labels import LabelStore, legacy_protection_labels
from utils.ai_profile import AIProfileStore
from utils.ai_scoring import AIScoreStore
from utils.state_context import state_context, score_fingerprint, resolved_identity
from utils.profile import build_profile


def _get(cfg, field, default=None):
    return cfg.get(field, default) if isinstance(cfg, dict) else getattr(cfg, field, default)


def score_snapshot(items, cfg, labels=None, *, th_keep=None, th_delete=None):
    """Return a coherent result/profile from one source, label, config and cache version."""
    from config import DATA_DIR
    from utils import scoring, scoring_v2
    from utils.media_filter import filter_media
    items = filter_media(items, check_local_missing=False)
    configured_context = state_context(cfg)
    context = state_context(cfg, items)
    # Feedback supplied before auto-detection belongs to an unknown/old identity, not this viewer.
    labels = dict(LabelStore(DATA_DIR / "smart.db", context=context).all()
                  if labels is None or configured_context != context else labels)
    user_id, source_identity = resolved_identity(items, cfg)
    # Fill facts only for duplicate candidates; keep cache identity equal to the returned snapshot.
    scoring.dedup_groups(items, labels)
    fingerprint = score_fingerprint(items, labels, cfg)
    engine = scoring if _get(cfg, "engine", "v2") == "v1" else scoring_v2
    enhance = bool(_get(cfg, "enhance_match", True))
    liked, disliked, semantic, warnings = (), (), {}, []
    if enhance and bool(_get(cfg, "ai_enabled", True)):
        try:
            taste = AIProfileStore(DATA_DIR / "smart.db", context=context, fingerprint=fingerprint).load()
            if taste:
                liked, disliked = taste["liked_keys"], taste["disliked_keys"]
            semantic = AIScoreStore(DATA_DIR / "smart.db", context=context, fingerprint=fingerprint).get_many(
                [it.id for it in items])
        except Exception:
            warnings.append("AI缓存不可读取，已使用纯规则；既有缓存未修改")
    kwargs = dict(enhance=enhance, extra_liked=liked, extra_disliked=disliked,
                  th_keep=_get(cfg, "score_th_keep", 55) / 100 if th_keep is None else th_keep,
                  th_delete=_get(cfg, "score_th_delete", 25) / 100 if th_delete is None else th_delete)
    if engine is scoring_v2:
        kwargs["ai_scores"] = semantic
    results = engine.rank_items(items, labels, **kwargs)
    legacy_protection = legacy_protection_labels(DATA_DIR / "smart.db", items, labels)
    for result in results:
        if result.item.id in legacy_protection and result.decision == scoring.DEC_DELETE:
            result.decision = scoring.DEC_KEEP
            result.reason = "旧反馈待确认：保守保留该实体；未用于当前用户画像"
    if legacy_protection:
        warnings.append(f"{len(legacy_protection)}项旧保留反馈待确认，当前仅保护删除，不训练画像")
    from utils.media_filter import LibraryItems
    snapshot_items = LibraryItems(items, getattr(items, "collection_stats", {}))
    return {"items": snapshot_items, "results": results, "counts": engine.decision_counts(results),
            "labels": labels, "profile": build_profile(items, labels, results),
            "source_context": context, "fingerprint": fingerprint, "warnings": warnings,
            "resolved_user_id": user_id, "resolved_source_identity": source_identity,
            "legacy_pending": list(legacy_protection), "legacy_protection": legacy_protection,
            "status": "succeeded"}
