"""AI 参与分类 - 提炼「喜欢/不喜欢标签画像」并回写评分规则。

纯函数层（可单测）+ 轻量存储（SQLite smart.db 新表 ai_profile）：
- build_ai_prompt(p)      构造问 AI 的输入
- parse_ai_taste(text)    解析 AI 输出为 (liked_keys, disliked_keys, notes)
- AIProfileStore          持久化/读取/清除 AI 画像

回写机制（在 utils/scoring.py、workers/scorer.py 消费）：
AI 提炼的特征不外造权重，而是作为「额外点亮」的 key 并入 liked/disliked 集合——
只有当某部作品本身带这个标签时才参与命中，因此不改变你原始的相似度算法。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

WANTED_KINDS = {"actor", "studio", "genre", "tag", "prefix"}

# 中文别名 → 规范 kind
_KIND_MAP = {
    "actor": "actor", "av女优": "actor", "演员": "actor", "女优": "actor",
    "studio": "studio", "厂商": "studio", "公司": "studio", "厂牌": "studio",
    "genre": "genre", "类型": "genre", "题材": "genre", "情色类型": "genre",
    "tag": "tag", "标签": "tag", "tags": "tag",
    "prefix": "prefix", "系列": "prefix", "番号前缀": "prefix",
}


def _to_keys(items, exclude=()) -> set:
    keys = set()
    for it in items or []:
        if isinstance(it, dict):
            kind, value = it.get("kind"), it.get("value")
        else:
            parts = re.split(r"[:：]", str(it), 1)
            if len(parts) == 2:
                kind, value = parts[0].strip(), parts[1].strip()
            else:
                kind, value = "tag", str(it).strip()
        if not value:
            continue
        k = _KIND_MAP.get(str(kind).strip().lower()) or str(kind).strip().lower()
        if k in WANTED_KINDS and k not in exclude:
            keys.add((k, value))
    return keys


def parse_ai_taste(text: str):
    """解析 AI 输出为 (liked_keys, disliked_keys, notes)。容错 markdown 代码块与前后缀。

    disliked 解析时排除 prefix(系列前缀)——用户认为厂商/系列差异不算"内容不喜欢"。
    """
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        t = t[a:b + 1]
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return set(), set(), ""
    liked = _to_keys(data.get("liked"))
    disliked = _to_keys(data.get("disliked"), exclude={"prefix"})
    notes = str(data.get("notes") or "").strip()
    return liked, disliked, notes


def build_ai_prompt(p: dict) -> str:
    """基于画像统计构造要 AI 提炼标签的 prompt。"""
    lines = [
        "这是我 Jellyfin 媒体库的喜好画像统计，请提炼出「我喜欢/可能喜欢」与「我不喜欢/可能不喜欢」的"
        "标签画像，用来改进内容分类规则。",
        f"- 正样本(看过/收藏/播放) {p.get('n_pos', 0)} 部，收藏 {p.get('n_fav', 0)}，"
        f"看完 {p.get('n_played', 0)}，无码偏好 {p.get('unc_ratio', 0):.0%}。",
    ]
    if p.get("top_tags"):
        lines.append("- Top标签(强度): " + "、".join(
            f"{x['name']}({x['weight']:.0f})" for x in p["top_tags"][:12]))
    if p.get("top_actors"):
        lines.append("- Top演员(收藏): " + "、".join(
            f"{x['name']}({x['fav']})" for x in p["top_actors"][:8]))
    if p.get("top_studios"):
        lines.append("- Top厂商: " + "、".join(
            f"{x['name']}({x['fav']})" for x in p["top_studios"][:6]))
    if p.get("top_genres"):
        lines.append("- Top类型: " + "、".join(x["name"] for x in p["top_genres"][:10]))
    if p.get("neg_features"):
        lines.append("- 疑似不喜欢(反向推断): " + "、".join(
            f"{k}{n}x" for k, n in p["neg_features"][:10]))
    lines += [
        "",
        "请只输出一个 JSON 对象，不要任何解释，格式：",
        '{"liked":[{"kind":"tag","value":"巨乳"},{"kind":"actor","value":"希咲那奈"}],'
        '"disliked":[{"kind":"actor","value":"xxx"},{"kind":"tag","value":"yyy"}],'
        '"notes":"1-2句对口味主线的判断"}',
        "kind 只允许 actor/studio/genre/tag/prefix 之一；value 用简体中文或番号前缀原文。"
        "只填你有把握的，宁可少不要臆造。",
        "重要：disliked 只提炼内容类（actor/studio/genre/tag），"
        "不要输出 prefix(系列前缀)等厂商/系列差异——它们不构成内容不喜欢。",
    ]
    return "\n".join(lines)


class AIProfileStore:
    """持久化 AI 提炼的画像。每次独立短连接，线程安全。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ai_profile(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def save(self, liked: set, disliked: set, notes: str = "") -> None:
        payload = json.dumps({
            "liked": sorted([list(k) for k in liked]),
            "disliked": sorted([list(k) for k in disliked]),
            "notes": notes,
        }, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO ai_profile(id, payload, updated_at) VALUES(1,?,?)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                                                 updated_at=excluded.updated_at""",
                (payload, datetime.now().isoformat(timespec="seconds")),
            )

    def load(self) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, updated_at FROM ai_profile WHERE id=1"
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return {
            "liked_keys": {tuple(k) for k in data.get("liked", [])},
            "disliked_keys": {tuple(k) for k in data.get("disliked", [])},
            "notes": data.get("notes", ""),
            "created_at": row[1],
        }

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ai_profile WHERE id=1")
