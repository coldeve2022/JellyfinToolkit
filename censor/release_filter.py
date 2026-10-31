"""替换版本检索的筛选规则引擎 —— 只负责"这条记录满不满足"，不碰界面。

为什么单独一层
--------------
以前筛选是硬编码的几段 `if`（状态、喜欢、播放各写一遍），加一个维度就要改
`_filter`，而且没法组合、没法保存。这里把"字段 → 怎么取值"和"条件 → 怎么判断"
拆开，界面只负责编辑规则列表。

支持的字段见 :data:`FIELDS`。每个字段自带：标签、类型、可用运算符、取值函数。
取值函数拿 ``(row, ctx)``，ctx 里放外部数据（Jellyfin 快照、文件系统探测），
这样引擎本身不依赖 Qt，也不直接读盘。
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from utils.media_review_query import aggregate_review

__all__ = ["FilterRule", "FieldSpec", "FIELDS", "evaluate", "describe", "fields_for_ui"]

#: 运算符 → 界面标签
OPS = {
    "contains": "包含",
    "not_contains": "不包含",
    "startswith": "开头是",
    "eq": "等于",
    "ne": "不等于",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
    "is_true": "是",
    "is_false": "否",
    "within_days": "在最近 N 天内",
    "older_days": "早于 N 天",
    "never": "从未",
}

_TEXT_OPS = ("contains", "not_contains", "startswith", "eq")
_NUM_OPS = ("eq", "gte", "lte", "gt", "lt")
_BOOL_OPS = ("is_true", "is_false")
_DATE_OPS = ("within_days", "older_days", "never")


@dataclass(frozen=True)
class FilterRule:
    field: str
    op: str
    value: str = ""

    def as_dict(self) -> dict:
        return {"field": self.field, "op": self.op, "value": self.value}

    @staticmethod
    def from_dict(data: dict) -> "FilterRule":
        return FilterRule(str(data.get("field", "")), str(data.get("op", "")),
                          str(data.get("value", "")))


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str                       # text / number / bool / date / enum
    ops: tuple[str, ...]
    getter: Callable[[object, dict], object]
    #: enum 字段的可选值：[(value, label)]
    choices: tuple[tuple[str, str], ...] = ()
    #: 单位提示（只用于界面）
    unit: str = ""


# ── 取值工具 ───────────────────────────────────────────────

def _report(row):
    return getattr(row, "report", None)


def _candidates(row):
    report = _report(row)
    return tuple(getattr(report, "candidates", ()) or ())


def _magnets(row):
    """候选自带的磁链（``ReleaseCandidate.magnets``）。

    不要走 ``report.best.best_magnet``：那个只有"最佳候选"才有，
    按行统计磁链数时会漏掉其余候选。
    """
    out = []
    for candidate in _candidates(row):
        out.extend(getattr(candidate, "magnets", ()) or ())
    return out


def _magnet_bytes(row) -> int:
    """磁链大小统一换成字节。

    注意 ``MagnetLink`` 里存的是 ``size_mb``（MB，整数），不是 ``size_bytes`` ——
    这里曾经按 size_bytes 取，结果永远是 0，大小筛选形同虚设。
    """
    sizes = [int(getattr(m, "size_mb", 0) or 0) * 1024 * 1024 for m in _magnets(row)]
    return max(sizes) if sizes else 0


def _filenames(row) -> str:
    return " ".join(str(p) for p in (getattr(row, "paths", []) or []))


def _tagged(row, words: Iterable[str]) -> bool:
    text = _filenames(row).casefold()
    return any(word.casefold() in text for word in words)


def _favorite(row, ctx) -> str:
    return _review_states(row, ctx)[0]


def _played(row, ctx) -> str:
    return _review_states(row, ctx)[1]


def _review_states(row, ctx):
    lookup = ctx.get("review")
    if not callable(lookup):
        return 'unknown', 'unknown'
    return aggregate_review((lookup(path) or {}) for path in getattr(row, 'paths', []))


def _exists(row, ctx) -> bool:
    checker = ctx.get("exists")
    if callable(checker):
        return any(bool(checker(p)) for p in (getattr(row, "paths", []) or []))
    return any(os.path.isfile(str(p)) for p in (getattr(row, "paths", []) or []))


def _sources(row) -> str:
    names = set()
    for candidate in _candidates(row):
        name = str(getattr(candidate, "source", "") or "")
        if name:
            names.add(name)
    return " ".join(sorted(names))


FIELDS: dict[str, FieldSpec] = {
    spec.key: spec
    for spec in (
        FieldSpec("number", "番号", "text", _TEXT_OPS,
                  lambda row, ctx: getattr(row, "number", "") or ""),
        FieldSpec("filename", "文件路径/文件名", "text", _TEXT_OPS,
                  lambda row, ctx: _filenames(row)),
        FieldSpec("files", "片数（副本数）", "number", _NUM_OPS,
                  lambda row, ctx: len(getattr(row, "paths", []) or []), unit="个"),
        FieldSpec("state", "检索结论", "enum", ("eq", "ne"),
                  lambda row, ctx: getattr(row, "state_key", ""),
                  choices=(("todo", "未查"), ("confirmed", "确有"), ("likely", "疑似"),
                           ("none", "未见"), ("unknown", "未完成/未知"))),
        FieldSpec("lookup_status", "检索执行状态", "enum", ("eq", "ne"),
                  lambda row, ctx: getattr(getattr(_report(row), 'status', None), 'value', 'todo'),
                  choices=(("todo", "未查"), ("completed", "完成"), ("partial", "部分完成"),
                           ("failed", "失败"), ("cancelled", "已取消"))),
        FieldSpec("candidates", "候选条数", "number", _NUM_OPS,
                  lambda row, ctx: len(_candidates(row)), unit="条"),
        FieldSpec("magnets", "磁链条数", "number", _NUM_OPS,
                  lambda row, ctx: len(_magnets(row)), unit="条"),
        FieldSpec("magnet_size", "最大磁链大小", "number", _NUM_OPS,
                  lambda row, ctx: _magnet_bytes(row), unit="字节（填 GB 会自动换算）"),
        FieldSpec("confidence", "无码判定置信度", "number", _NUM_OPS,
                  lambda row, ctx: float(getattr(_report(row), "uncensored_confidence", 0.0) or 0.0),
                  unit="0–1"),
        FieldSpec("sources", "命中来源", "text", _TEXT_OPS,
                  lambda row, ctx: _sources(row)),
        FieldSpec("searched_sources", "尝试过的来源", "text", _TEXT_OPS,
                  lambda row, ctx: ' '.join(sorted(getattr(_report(row), 'sources', {}) or {}))),
        FieldSpec("probe_confidence", "检测结论置信度（最高）", "number", _NUM_OPS,
                  lambda row, ctx: max((r.verdict.confidence for r in getattr(row, 'records', ())), default=0), unit="0–1"),
        FieldSpec("visual_score", "画面检测分数（最高）", "number", _NUM_OPS,
                  lambda row, ctx: max((r.visual_score for r in getattr(row, 'records', ())), default=0), unit="原始分数"),
        FieldSpec("probe_elapsed", "检测耗时（合计）", "number", _NUM_OPS,
                  lambda row, ctx: sum(r.elapsed for r in getattr(row, 'records', ())), unit="秒"),
        FieldSpec("queried", "最近检索时间", "date", _DATE_OPS,
                  lambda row, ctx: float(getattr(_report(row), "queried_at", 0.0) or 0.0),
                  unit="天"),
        FieldSpec("elapsed", "检索耗时", "number", _NUM_OPS,
                  lambda row, ctx: float(getattr(_report(row), "elapsed", 0.0) or 0.0), unit="秒"),
        FieldSpec("cache_hit", "走的是缓存", "bool", _BOOL_OPS,
                  lambda row, ctx: bool(getattr(_report(row), "cache_hit", False))),
        FieldSpec("has_page", "有候选页面链接", "bool", _BOOL_OPS,
                  lambda row, ctx: any(getattr(c, "page_url", "") for c in _candidates(row))),
        FieldSpec("has_uncensored", "有判为无码的候选", "bool", _BOOL_OPS,
                  lambda row, ctx: any(bool(getattr(c, "uncensored", False)) for c in _candidates(row))),
        FieldSpec("exists", "本地文件存在", "bool", _BOOL_OPS, _exists),
        FieldSpec("no_number", "没有番号", "bool", _BOOL_OPS,
                  lambda row, ctx: not bool(getattr(row, "searchable", True))),
        FieldSpec("tag_4k", "文件名含 4K", "bool", _BOOL_OPS,
                  lambda row, ctx: _tagged(row, ("4k", "2160", "uhd"))),
        FieldSpec("tag_60fps", "文件名含 60FPS", "bool", _BOOL_OPS,
                  lambda row, ctx: _tagged(row, ("60fps", "60 fps"))),
        FieldSpec("tag_cnsub", "文件名像中文字幕版", "bool", _BOOL_OPS,
                  lambda row, ctx: _tagged(row, ("-c.", "-ch.", "中字", "chs", "-cn."))),
        FieldSpec("tag_leak", "文件名含流出/破解", "bool", _BOOL_OPS,
                  lambda row, ctx: _tagged(row, ("流出", "破解", "leak", "uncensored"))),
        FieldSpec("favorite", "Jellyfin 喜欢", "enum", ("eq", "ne"),
                  _favorite,
                  choices=(("favorite", "已喜欢"), ("not_favorite", "未喜欢"),
                           ("unknown", "未知/未匹配"))),
        FieldSpec("played", "Jellyfin 播放状态", "enum", ("eq", "ne"),
                  _played,
                  choices=(("played", "已播放"), ("progress", "播放中"),
                           ("unplayed", "未播放"), ("unknown", "未知/未匹配"))),
    )
}


# ── 判断 ───────────────────────────────────────────────────

def _as_float(value, spec_field: str) -> float:
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        raise ValueError('请填写数值')
    # "10gb" / "5 g" 这类写法直接换算成字节，省得用户自己乘
    for unit, factor in (("tb", 1024 ** 4), ("gb", 1024 ** 3), ("mb", 1024 ** 2)):
        if spec_field == 'magnet_size' and text.endswith(unit):
            text = str(float(text[: -len(unit)]) * factor)
            break
    try:
        number = float(text)
    except ValueError:
        raise ValueError('请输入有效数值；磁链大小支持 MB / GB / TB') from None
    if not math.isfinite(number):
        raise ValueError('数值必须为有限值')
    return number


def validate_rule(rule: FilterRule) -> None:
    spec = FIELDS.get(rule.field)
    if spec is None:
        raise ValueError(f'不支持的字段：{rule.field}')
    if rule.op not in spec.ops:
        raise ValueError(f'{spec.label}不支持该运算符')
    if spec.kind == 'number':
        _as_float(rule.value, spec.key)
    elif spec.kind == 'date' and rule.op != 'never':
        if _as_float(rule.value, spec.key) <= 0:
            raise ValueError('天数必须大于零')
    elif spec.kind == 'enum' and rule.value not in dict(spec.choices):
        raise ValueError(f'{spec.label}的取值无效')


def _one(rule: FilterRule, row, ctx) -> bool:
    spec = FIELDS.get(rule.field)
    if spec is None:
        return True
    validate_rule(rule)
    actual = spec.getter(row, ctx)
    op = rule.op

    if op in _BOOL_OPS:
        want = op == "is_true"
        return bool(actual) is want

    if spec.kind == "date":
        stamp = float(actual or 0.0)
        if op == "never":
            return stamp <= 0
        days = _as_float(rule.value, spec.key)
        if stamp <= 0:
            return False
        age = (__import__("time").time() - stamp) / 86400.0
        return age <= days if op == "within_days" else age > days

    if spec.kind == "number":
        left, right = float(actual or 0.0), _as_float(rule.value, spec.key)
        return {"eq": left == right, "ne": left != right, "gt": left > right,
                "gte": left >= right, "lt": left < right, "lte": left <= right}.get(op, True)

    if spec.kind == "enum":
        same = str(actual or "") == str(rule.value or "")
        return same if op == "eq" else (not same if op == "ne" else True)

    text = str(actual or "").casefold()
    needle = str(rule.value or "").casefold()
    if op == "contains":
        return needle in text
    if op == "not_contains":
        return needle not in text
    if op == "startswith":
        return text.startswith(needle)
    if op == "eq":
        return text == needle
    return True


def evaluate(rules: Iterable[FilterRule], logic: str, row, ctx: Optional[dict] = None) -> bool:
    """所有条件按 ``logic``（and / or）组合。没有条件时恒为 True。"""
    active = [rule for rule in rules if rule and rule.field in FIELDS]
    if not active:
        return True
    ctx = ctx or {}
    if str(logic).lower() == "or":
        return any(_one(rule, row, ctx) for rule in active)
    return all(_one(rule, row, ctx) for rule in active)


def describe(rule: FilterRule) -> str:
    """给界面看的一句话描述（筛选面板的 chips / 悬停提示）。"""
    spec = FIELDS.get(rule.field)
    if spec is None:
        return rule.field
    op = OPS.get(rule.op, rule.op)
    if rule.op in _BOOL_OPS:
        return f"{spec.label}：{op}"
    if spec.kind == "enum":
        chosen = dict(spec.choices).get(rule.value, rule.value)
        return f"{spec.label} {op} {chosen}"
    if spec.kind == "date":
        return f"{spec.label}：{op}" if rule.op == "never" else f"{spec.label}：{op.replace('N', str(rule.value))}"
    return f"{spec.label} {op} {rule.value}"


def fields_for_ui() -> list[tuple[str, str, tuple[str, ...], tuple[tuple[str, str], ...], str]]:
    """给对话框用：[(key, label, ops, choices, unit)]，保持声明顺序。"""
    return [(spec.key, spec.label, spec.ops, spec.choices, spec.unit)
            for spec in FIELDS.values()]
