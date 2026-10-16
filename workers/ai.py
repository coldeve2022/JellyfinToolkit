"""Model workers emit suggestions, never trusted user feedback."""
from __future__ import annotations
import json
from PySide6.QtCore import QThread, Signal
from utils.ai_results import parse_recheck_json, parse_dedup_json


def query_llm(base_url: str, key: str, model: str, messages: list, timeout: int = 180) -> str:
    from utils.local_ai import chat_completion
    return chat_completion(base_url, key, model, messages, timeout=timeout)


class _AIWorker(QThread):
    log = Signal(str)
    result = Signal(str)
    finished = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stopped = False

    def stop(self):
        self._stopped = True
        self.requestInterruption()

    def _cancelled(self, **values):
        self.finished.emit(dict(values, error=None, cancelled=True, status="cancelled"))


class AIRecheckWorker(_AIWorker):
    verdict = Signal(dict)

    def __init__(self, candidates: list, base_url: str, key: str, model: str,
                 context: str = "delete", parent=None):
        super().__init__(parent)
        self.candidates = [dict(c) for c in candidates]
        self.base_url, self.key, self.model, self.context = base_url, key, model, context

    def run(self):
        total, completed = len(self.candidates), 0
        try:
            if not total:
                raise ValueError("无候选")
            for start in range(0, total, 25):
                if self._stopped:
                    self._cancelled(total=total, ok=completed)
                    return
                batch = self.candidates[start:start + 25]
                records = [{"i": i, "num": c.get("num"), "name": c.get("name"),
                            "actors": c.get("actors", []), "studios": c.get("studios", []),
                            "genres": c.get("genres", []), "tags": c.get("tags", []),
                            "rule_hit": c.get("hit"), "rule_reason": c.get("reason")}
                           for i, c in enumerate(batch, 1)]
                prompt = ("仅依据提供的元数据做保守复核，已有规则判断并非真实喜好证据。"
                          "不能确定一律UNSURE。不要臆造信息。"
                          '仅返回JSON数组 [{"i":1,"verdict":"KEEP|DELETE|UNSURE","reason":"理由"}]。'
                          f"场景：{self.context}。\n" + json.dumps(records, ensure_ascii=False))
                text = query_llm(self.base_url, self.key, self.model, [{"role": "user", "content": prompt}])
                if self._stopped:
                    self._cancelled(total=total, ok=completed)
                    return
                verdicts = parse_recheck_json(text, batch)
                for row in verdicts:
                    if row["item_id"]:
                        self.verdict.emit(row)
                    self.result.emit(f"{row['item_id'] or '?'}: {row['verdict']} - {row['reason']}")
                    completed += 1
            self.finished.emit({"total": total, "ok": completed, "error": None, "status": "succeeded"})
        except Exception as exc:
            self.log.emit(f"AI复核未完成: {exc}")
            self.finished.emit({"total": total, "ok": completed, "error": str(exc), "status": "failed"})


class AIReportWorker(_AIWorker):
    def __init__(self, profile_summary: dict, base_url: str, key: str, model: str, parent=None):
        super().__init__(parent)
        self.summary = dict(profile_summary)
        self.base_url, self.key, self.model = base_url, key, model

    def run(self):
        try:
            if self._stopped:
                self._cancelled()
                return
            prompt = ("根据以下真实行为/人工反馈统计，写300字以内的中文偏好分析。"
                      "区分样本不足和确知不喜欢；规则预测不能作为用户真实意见。"
                      "没有依据不得建议整系列删除。给出有依据的偏好和可执行探索建议。\n"
                      + json.dumps(self.summary, ensure_ascii=False, default=str))
            text = query_llm(self.base_url, self.key, self.model, [{"role": "user", "content": prompt}])
            if self._stopped:
                self._cancelled()
                return
            self.result.emit(text)
            self.finished.emit({"error": None, "status": "succeeded"})
        except Exception as exc:
            self.log.emit(f"AI报告未完成: {exc}")
            self.finished.emit({"error": str(exc), "status": "failed"})


class AIDedupWorker(_AIWorker):
    verdict = Signal(dict)

    def __init__(self, groups: list[dict], base_url: str, key: str, model: str, parent=None):
        super().__init__(parent)
        self.groups = groups
        self.base_url, self.key, self.model = base_url, key, model

    def run(self):
        count = 0
        try:
            for group in self.groups:
                if self._stopped:
                    self._cancelled(groups=len(self.groups), verdicts=count)
                    return
                items = group["items"]
                if len(items) < 2:
                    continue
                if all(it.get("is_cd") for it in items):
                    result = {"num": group["num"], "keep_ids": [it["id"] for it in items],
                              "delete_ids": [], "reason": "分卷保护；仅人工核对"}
                else:
                    prompt = ("保守审核文件版本，不能仅靠体积宣称分辨率/质量；分卷不得自动删。"
                              "无足够证据可全保留；每项序号必须且只能出现一次。"
                              '只返回JSON对象 {"keep":[1],"delete":[2],"reason":"理由"}。\n'
                              + json.dumps([dict(it, i=i) for i, it in enumerate(items, 1)], ensure_ascii=False))
                    text = query_llm(self.base_url, self.key, self.model,
                                     [{"role": "user", "content": prompt}], timeout=90)
                    result = parse_dedup_json(text, group)
                if self._stopped:
                    self._cancelled(groups=len(self.groups), verdicts=count)
                    return
                self.verdict.emit(result)
                self.result.emit(f"组<{group['num']}>: 保留{result['keep_ids']}，建议删{result['delete_ids']} - {result['reason']}")
                count += 1
            self.finished.emit({"groups": len(self.groups), "verdicts": count, "error": None, "status": "succeeded"})
        except Exception as exc:
            self.log.emit(f"AI去重未完成: {exc}")
            self.finished.emit({"groups": len(self.groups), "verdicts": count, "error": str(exc), "status": "failed"})


class AITasteWorker(_AIWorker):
    def __init__(self, profile_summary: dict, store_path, base_url: str, key: str, model: str,
                 parent=None, context: str = "", fingerprint: str = ""):
        super().__init__(parent)
        self.summary, self.store_path = profile_summary, store_path
        self.base_url, self.key, self.model = base_url, key, model
        self.context, self.fingerprint = context, fingerprint

    def run(self):
        from utils.ai_profile import AIProfileStore, build_ai_prompt, parse_ai_taste
        try:
            if self._stopped:
                self._cancelled(liked=0, disliked=0)
                return
            text = query_llm(self.base_url, self.key, self.model,
                             [{"role": "user", "content": build_ai_prompt(self.summary)}])
            liked, disliked, notes = parse_ai_taste(text, strict=True)
            if self._stopped:
                self._cancelled(liked=0, disliked=0)
                return
            AIProfileStore(self.store_path, context=self.context, fingerprint=self.fingerprint).save(liked, disliked, notes)
            self.result.emit(f"AI偏好建议已保存：喜欢 {len(liked)}，不喜欢 {len(disliked)}；{notes}")
            self.finished.emit({"liked": len(liked), "disliked": len(disliked), "error": None, "status": "succeeded"})
        except Exception as exc:
            self.log.emit(f"AI画像未更新: {exc}")
            self.finished.emit({"liked": 0, "disliked": 0, "error": str(exc), "status": "failed"})
