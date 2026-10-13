"""AI 语义打分后台线程 - 对"待复核/边界"作品分批让本地 LLM 打分并缓存。

与 AIRecheckWorker 的区别：本 worker 产出【0..1 的内容相似度分数】并写缓存，
供评分引擎融合；而 AIRecheckWorker 只给出 KEEP/DELETE/UNSURE 的离散判定。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from collections import defaultdict

from utils.ai_scoring import (
    AIScoreStore, build_score_prompt, parse_score_json,
)
from workers.ai import query_llm


class AIScoreWorker(QThread):
    """对一批候选做 AI 语义打分，写 AIScoreStore 缓存。

    分批(max 25/批)串行，结果逐条落库；连不上时 finished 带 error(非致命)。
    """

    log = Signal(str)
    finished = Signal(dict)   # {"scored":n, "error":str|None}

    def __init__(self, candidates: list[dict], profile_line: str, bucket_cn: str,
                 store_path, base_url: str, key: str, model: str,
                  parent=None, context: str = "", fingerprint: str = ""):
        """candidates: [{"item_id","num","name","actors","studios","genres","tags"}]"""
        super().__init__(parent)
        self.candidates = candidates
        self.profile_line = profile_line
        self.bucket_cn = bucket_cn
        self.store_path = store_path
        self.base_url, self.key, self.model = base_url, key, model
        self.context, self.fingerprint = context, fingerprint
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if not self.candidates:
            self.finished.emit({"scored": 0, "error": None})
            return
        try:
            store = AIScoreStore(self.store_path, context=self.context, fingerprint=self.fingerprint)
        except Exception as exc:
            self.finished.emit({"scored": 0, "error": str(exc), "status": "failed"})
            return
        # 分批
        by_bucket = defaultdict(list)
        for candidate in self.candidates:
            by_bucket[candidate.get("bucket") or self.bucket_cn].append(candidate)
        batches = [group[i:i + 25] for group in by_bucket.values() for i in range(0, len(group), 25)]
        total = 0
        for batch in batches:
            if self._stopped:
                self.finished.emit({"scored": total, "error": None, "cancelled": True, "status": "cancelled"})
                return
            bucket = batch[0].get("bucket") or self.bucket_cn
            line = self.profile_line.get(bucket, "（本分类暂无有效正样本）") if isinstance(self.profile_line, dict) else self.profile_line
            prompt = build_score_prompt(batch[0].get("bucket_cn") or self.bucket_cn, line, batch)
            try:
                text = query_llm(self.base_url, self.key, self.model,
                                 [{"role": "user", "content": prompt}], timeout=180)
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"❌ AI 打分失败(本次跳过，评分退回纯规则): {e}")
                self.finished.emit({"scored": total, "error": str(e)})
                return
            if self._stopped:
                self.finished.emit({"scored": total, "error": None, "cancelled": True, "status": "cancelled"})
                return
            parsed = parse_score_json(text)
            if not any(i in parsed for i in range(1, len(batch) + 1)):
                self.finished.emit({"scored": total, "error": "AI没有返回本批次有效评分；旧缓存已保留", "status": "failed"})
                return
            try:
                for i, cand in enumerate(batch, 1):
                    v = parsed.get(i)
                    if v:
                        store.save(cand["item_id"], v["score"], v.get("note", ""))
                        total += 1
            except Exception as exc:
                self.finished.emit({"scored": total, "error": str(exc), "status": "failed"})
                return
            self.log.emit(f"🤖 AI 打分完成一批：{len(batch)} 条，有效 {len([k for k in parsed])} 条")
        self.finished.emit({"scored": total, "error": None, "status": "succeeded"})
