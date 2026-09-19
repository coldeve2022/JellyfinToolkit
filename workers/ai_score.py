"""AI 语义打分后台线程 - 对"待复核/边界"作品分批让本地 LLM 打分并缓存。

与 AIRecheckWorker 的区别：本 worker 产出【0..1 的内容相似度分数】并写缓存，
供评分引擎融合；而 AIRecheckWorker 只给出 KEEP/DELETE/UNSURE 的离散判定。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

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
                 parent=None):
        """candidates: [{"item_id","num","name","actors","studios","genres","tags"}]"""
        super().__init__(parent)
        self.candidates = candidates
        self.profile_line = profile_line
        self.bucket_cn = bucket_cn
        self.store_path = store_path
        self.base_url, self.key, self.model = base_url, key, model
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if not self.candidates:
            self.finished.emit({"scored": 0, "error": None})
            return
        store = AIScoreStore(self.store_path)
        # 分批
        batches = [self.candidates[i:i + 25] for i in range(0, len(self.candidates), 25)]
        total = 0
        for batch in batches:
            if self._stopped:
                break
            prompt = build_score_prompt(self.bucket_cn, self.profile_line, batch)
            try:
                text = query_llm(self.base_url, self.key, self.model,
                                 [{"role": "user", "content": prompt}], timeout=180)
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"❌ AI 打分失败(本次跳过，评分退回纯规则): {e}")
                self.finished.emit({"scored": total, "error": str(e)})
                return
            parsed = parse_score_json(text)
            for i, cand in enumerate(batch, 1):
                v = parsed.get(i)
                if v:
                    store.save(cand["item_id"], v["score"], v.get("note", ""))
                    total += 1
            self.log.emit(f"🤖 AI 打分完成一批：{len(batch)} 条，有效 {len([k for k in parsed])} 条")
        self.finished.emit({"scored": total, "error": None})
