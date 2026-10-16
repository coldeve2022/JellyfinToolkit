"""Conservative main-media selection and explicit row/file/work count contracts."""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections import Counter


_EXTRA_TYPES = frozenset({"clip", "trailer", "behindthescenes", "deletedscene", "interview",
                          "scene", "sample", "themesong", "themevideo", "featurette", "short"})


class LibraryItems(list):
    """A compatible list carrying aggregate collection diagnostics, never private names."""
    def __init__(self, items=(), collection_stats=None):
        super().__init__(items)
        self.collection_stats = dict(collection_stats or {})


def flag(value):
    return value is True or value == 1 or str(value).strip().lower() in {"true", "1"}


def known_extra(value):
    """Only documented specific extras; legacy 0/Unknown or new unknown enums stay usable."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1 <= value <= 11
    if not isinstance(value, str):
        return False
    text = value.strip()
    if re.fullmatch(r"[1-9][0-9]*", text):
        return 1 <= int(text) <= 11
    return text.rsplit(".", 1)[-1].replace("_", "").replace(" ", "").lower() in _EXTRA_TYPES


def normalized_path(path):
    text = str(path).replace("\\", "/")
    if ntpath.splitdrive(text)[0] or text.startswith("//"):
        return ntpath.normpath(text).replace("\\", "/").casefold()
    return posixpath.normpath(text)


def physical_key(item, cache=None):
    path = str(getattr(item, "path", "") or "")
    if not path:
        return "id:" + str(item.id)
    if getattr(item, "_physical_path", None) == path:
        return item._physical_key
    key = normalized_path(path)
    cache = {} if cache is None else cache
    if key not in cache:
        # Counting must not probe thousands of local/NAS files. Reuse a known key, otherwise
        # report distinct normalized video paths, not a verified inode inventory.
        cache[key] = "path:" + key
    item._physical_path, item._physical_key = path, cache[key]
    return cache[key]


class MediaFilter:
    """One scan caches file facts and parent listings, keeping unreachable remote paths."""
    def __init__(self, stats=None, listing_cache=None):
        self.stats = Counter(stats or {})
        self.stats.setdefault("raw_video_entries", 0)
        self.files, self.directories, self.ids = {}, {}, set()
        self.listing_cache = listing_cache if listing_cache is not None else {}

    def reason(self, path, *, type_full="", extra_type=None, is_virtual=False,
               location_type="", is_deleted=False, authoritative_type=True, **_unused):
        from utils.library import is_junk_attachment_path
        if flag(is_deleted):
            return "excluded_deleted"
        if flag(is_virtual) or str(location_type).casefold() == "virtual":
            return "excluded_virtual"
        if known_extra(extra_type):
            return "excluded_extra_type"
        if str(type_full).rsplit(".", 1)[-1].casefold() == "trailer":
            return "excluded_trailer"
        if not str(path or "").strip():
            return "excluded_no_path"
        # Display titles are untrusted for this purpose: a movie can be named Interview or Theme.
        primary_type = authoritative_type and str(type_full).rsplit(".", 1)[-1].casefold() in {"movie", "episode", "musicvideo"}
        if not primary_type and is_junk_attachment_path(path):
            return "excluded_attachment_path"
        return None

    def _missing_local(self, path):
        if "://" in path:
            return False
        parent, filename = os.path.dirname(path), os.path.basename(path)
        if not parent:
            return False
        if parent not in self.directories:
            names = self.listing_cache.get(parent)
            if names is None:
                return False
            self.directories[parent] = {name.casefold() if os.name == "nt" else name for name in names}
        names = self.directories[parent]
        filename = filename.casefold() if os.name == "nt" else filename
        return names is not None and filename not in names

    def accept(self, path, *, item_id="", count_raw=True, check_missing=True, **facts):
        if count_raw:
            self.stats["raw_video_entries"] += 1
        reason = self.reason(path, **facts)
        if reason is None and item_id and item_id in self.ids:
            reason = "duplicate_item_ids"
        if reason is None and check_missing and self._missing_local(str(path)):
            reason = "excluded_missing_local_file"
        if reason:
            self.stats[reason] += 1
            return False
        if item_id:
            self.ids.add(item_id)
        return True

    def collection(self, items):
        kept = []
        for item in items:
            if self._missing_local(item.path):
                self.stats["excluded_missing_local_file"] += 1
            else:
                kept.append(item)
        return LibraryItems(kept, self.stats)


def filter_media(items, *, log=None, should_stop=None, check_local_missing=True, listing_cache=None):
    """Reapply the same policy after mappings or to caller-provided Item snapshots."""
    inherited = getattr(items, "collection_stats", None)
    selector = MediaFilter(inherited, listing_cache if check_local_missing else None)
    kept = []
    for item in items:
        if should_stop and should_stop():
            from utils.library import LibraryCollectionCancelled
            raise LibraryCollectionCancelled("采集已取消；部分结果不参与统计")
        if selector.accept(item.path, item_id=item.id, count_raw=inherited is None,
                           check_missing=check_local_missing,
                           type_full=getattr(item, "raw_type", None) or item.type_cn,
                           authoritative_type=bool(getattr(item, "raw_type", None)),
                           extra_type=getattr(item, "extra_type", None),
                           is_virtual=getattr(item, "is_virtual", False),
                           is_deleted=getattr(item, "is_deleted", False),
                           location_type=getattr(item, "location_type", "")):
            kept.append(item)
    result = selector.collection(kept)
    if log:
        removed = result.collection_stats["raw_video_entries"] - len(result)
        log(f"正片采集：原始视频记录 {result.collection_stats['raw_video_entries']} 条，排除附属/虚拟/已删除等 {removed} 条，保留 {len(result)} 条库记录")
    return result


def count_media(items):
    """Records are not files; identified works group numbers/server version links only."""
    keys, cache = {}, {}
    for item in items:
        keys[item.id] = physical_key(item, cache)
    parents = {}
    def root(node):
        parents.setdefault(node, node)
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node
    def join(left, right):
        parents[root(left)] = root(right)
    for item in items:
        node = "id:" + str(item.id)
        join(node, keys[item.id])
        if item.num:
            join(node, "num:" + item.num.upper())
        primary = getattr(item, "primary_version_id", None)
        if primary and str(primary).replace("-", "").strip("0"):
            join(node, "id:" + str(primary))
    works = {root(key) for key in keys.values()}
    stats = dict(getattr(items, "collection_stats", {}))
    files = len(set(keys.values()))
    return {"entry_count": len(items), "file_count": files, "work_count": len(works),
            "library_entries": len(items), "physical_videos": files, "works": len(works),
            "collection_stats": stats,
            "filtered_out": max(0, stats.get("raw_video_entries", len(items)) - len(items))}

def profile_evidence(items, labels):
    """Merge only training/statistical evidence; original IDs and feedback remain untouched."""
    from dataclasses import replace
    from utils.scoring import (sample_weight, LABEL_LIKE, LABLE_KEEP, LABEL_REVIEW,
                               LABEL_DELETE, NEGATIVE_LABELS)
    grouped, cache = {}, {}
    for item in items:
        grouped.setdefault(physical_key(item, cache), []).append(item)
    evidence, feedback, aliases = [], {}, {}
    for key, group in grouped.items():
        trusted = [it for it in group if it.behavior_known]
        base = max(trusted or group, key=sample_weight)
        merged = replace(base)
        for field in ("actors", "studios", "genres", "tags"):
            setattr(merged, field, list(dict.fromkeys(v for it in group for v in getattr(it, field))))
        merged.is_favorite = any(it.is_favorite for it in trusted)
        merged.is_played = any(it.is_played for it in trusted)
        merged.play_count = max((it.play_count for it in trusted), default=0)
        merged.has_progress = any(it.has_progress for it in trusted)
        merged.behavior_known = bool(trusted)
        values = {labels[it.id] for it in group if it.id in labels}
        if LABEL_LIKE in values and values & NEGATIVE_LABELS:
            # Conflicting explicit alias feedback is unresolved, never two training examples.
            value = LABEL_REVIEW
            merged.is_favorite = merged.is_played = merged.has_progress = False
            merged.play_count = 0
        elif values & NEGATIVE_LABELS:
            value = LABEL_DELETE
        elif LABEL_LIKE in values:
            value = LABEL_LIKE
        elif LABLE_KEEP in values:
            value = LABLE_KEEP
        else:
            value = next(iter(values), None)
        if value:
            feedback[merged.id] = value
        evidence.append(merged)
        aliases[key] = merged
    return evidence, feedback, aliases
