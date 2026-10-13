"""Batch-bound model suggestions. Business numbers are never file identities."""
from __future__ import annotations
import json
import re


def json_payload(text, root):
    value = (text or "").strip()
    block = re.search(r"\x60{3}(?:json)?\s*(.*?)\x60{3}", value, re.S)
    if block:
        value = block.group(1).strip()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI没有返回有效结构化结果；没有应用任何新判断") from exc
    if not isinstance(parsed, root):
        raise ValueError("AI结果结构无效；没有应用任何新判断")
    return parsed


def parse_recheck_json(text, candidates):
    results = {}
    duplicates = set()
    for row in json_payload(text, list):
        if not isinstance(row, dict):
            continue
        index = row.get("i")
        verdict = str(row.get("verdict", "")).upper()
        if (not isinstance(index, int) or isinstance(index, bool)
                or not 1 <= index <= len(candidates) or verdict not in {"KEEP", "DELETE", "UNSURE"}):
            continue
        if index in results:
            duplicates.add(index)
        candidate = candidates[index - 1]
        results[index] = {"item_id": str(candidate.get("item_id") or candidate.get("id") or ""),
                          "verdict": verdict, "reason": str(row.get("reason") or "")[:500]}
    for index in duplicates:
        results.pop(index, None)
    if not results:
        raise ValueError("AI没有返回本批次可验证的有效判断")
    return [results[index] for index in sorted(results)]


def parse_dedup_json(text, group):
    payload = json_payload(text, dict)
    items = group["items"]
    keep, delete = payload.get("keep"), payload.get("delete")
    if not isinstance(keep, list) or not isinstance(delete, list):
        raise ValueError("AI去重结果缺少keep/delete列表")
    for values in (keep, delete):
        if any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= len(items) for i in values):
            raise ValueError("AI去重结果包含不属于本组的序号")
    if set(keep) & set(delete) or len(set(keep + delete)) != len(items):
        raise ValueError("AI去重结果相互冲突或不完整")
    if not keep:
        raise ValueError("AI不能建议删除整组且不保留任何版本")
    if any(items[i - 1].get("is_cd") for i in delete):
        raise ValueError("AI不能自动建议删除受保护的分卷")
    return {"num": group["num"], "keep_ids": [items[i - 1]["id"] for i in keep],
            "delete_ids": [items[i - 1]["id"] for i in delete],
            "reason": str(payload.get("reason") or "")[:500]}
