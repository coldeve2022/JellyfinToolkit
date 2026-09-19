"""AI 语义打分 - 把本地 LLM 作为"算法的一部分"，与规则命中度融合。

背景：纯规则命中度靠"内容特征是否命中过你喜欢史"。但有些作品标签全是泛词
（如 中出/巨乳 等刷屏项），规则分趋中、难以取立场。此时让本地 LLM 结合
"这些词与你的画像主线"做语义推断，给出 0..1 的"内容相似度"语义分，
与规则分做加权融合，把待复核中的语义高/低者提出来。

设计要点：
1. 【增量 + 缓存】对全库不逐一调 LLM（2000 次太慢）。只对"规则分落在边界区
   (th_delete..th_keep 之间，即待复核)"的作品分批让 AI 打分，结果存
   smart.db 的 ai_score 表(item_id, score, verdict, updated_at)，重排时读取。
2. 【失败即降级】AI 连不上 / 无候选时，返回空，评分退回纯规则（不报错、不阻塞）。
3. 【融合公式】final_content = w_rule * rule_hit + w_ai * ai_score
   （w_rule 默认 0.6，w_ai 默认 0.4；ai_score 缺失则 final = rule_hit）。
纯函数层，仅标准库；存储用轻量 SQLite（注入路径，可单测）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── AI 打分 prompt ─────────────────────────────────
def build_score_prompt(bucket_cn: str, profile_line: str, candidates: list[dict]) -> str:
    """构造让 AI 给"每个候选 + 你的画像"打分的内容。

    profile_line：你的画像主线（Top 演员/厂商/类型/标签 + 无码偏好）。
    candidates: [{"num","name","actors","studios","genres","tags"}]
    """
    lines = [
        f"这是我的【{bucket_cn}】收藏偏好画像主线：",
        profile_line,
        "",
        "下面是一批我「待判断是否合口味」的作品，请只依据画线与每条作品的元数据，"
        "判断它们与我的口味【内容相似度】，只输出相似度不能臆造不存在的元数据。",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"[{i}] {c.get('num') or '?'} | {str(c.get('name', ''))[:40]}"
            f" | 演:{','.join(c.get('actors', [])[:4])}"
            f" | 商:{','.join(c.get('studios', [])[:3])}"
            f" | 类:{','.join(c.get('genres', [])[:4])}"
            f" | 标:{','.join(c.get('tags', [])[:5])}"
        )
    lines += [
        "",
        "请只输出一个 JSON 数组，每个元素 format：",
        '{"i":序号,"score":0到1的小数,"note":"一句话理由"}',
        "score 越接近1越像我会喜欢，越接近0越不像。宁可保守，不要臆造。",
    ]
    return "\n".join(lines)


def parse_score_json(text: str) -> dict[int, dict]:
    """解析 AI 输出的 JSON 数组为 {序号: {score, note}}。容错 markdown 代码块。"""
    import re
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    a, b = t.find("["), t.rfind("]")
    if a >= 0 and b > a:
        t = t[a:b + 1]
    try:
        arr = json.loads(t)
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in arr:
        if isinstance(item, dict) and "i" in item:
            try:
                idx = int(item["i"])
                sc = max(0.0, min(1.0, float(item.get("score", 0.5))))
            except (TypeError, ValueError):
                continue
            out[idx] = {"score": sc, "note": str(item.get("note", ""))}
    return out


# ── AI 打分缓存存储 ─────────────────────────────────
class AIScoreStore:
    """持久化 AI 语义打分结果（增量缓存）。每次独立短连接，线程安全。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_score(
                    item_id TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    note TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )

    def save(self, item_id: str, score: float, note: str = "") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO ai_score(item_id, score, note, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET score=excluded.score,
                     note=excluded.note, updated_at=excluded.updated_at""",
                (item_id, round(score, 3), note, datetime.now().isoformat(timespec="seconds")),
            )

    def get(self, item_id: str) -> Optional[float]:
        with self._conn() as c:
            row = c.execute(
                "SELECT score FROM ai_score WHERE item_id=?", (item_id,)
            ).fetchone()
            return row[0] if row else None

    def get_many(self, ids) -> dict[str, float]:
        with self._conn() as c:
            q = ",".join("?" * len(ids))
            if not ids:
                return {}
            rows = c.execute(
                f"SELECT item_id, score FROM ai_score WHERE item_id IN ({q})",
                list(ids),
            ).fetchall()
            return {r[0]: r[1] for r in rows}

    def all_hint(self) -> dict[str, dict]:
        """返回全部 AI 打分条目（供画像页可视化 AI 语义分）。"""
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT item_id, score, note, updated_at FROM ai_score").fetchall()
            return {r["item_id"]: dict(r) for r in rows}

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM ai_score")


def fuse_content(rule_hit: float, ai_score: Optional[float],
                 w_rule: float = 0.6, w_ai: float = 0.4) -> float:
    """融合规则命中度与 AI 语义分。ai_score 为 None 时退化为 rule_hit。"""
    if ai_score is None:
        return rule_hit
    return rule_hit * w_rule + ai_score * w_ai
