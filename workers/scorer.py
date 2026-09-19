"""评分+重排后台线程 - 包装 ingest 与 scoring。

v2 说明：默认使用升级版评分引擎 scoring_v2（分桶画像 + 置信度加权 + 邻域传播），
统计指标更准；可用 config.engine 切回 v1（默认 "v2"）。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from config import DATA_DIR
from utils.ai_profile import AIProfileStore
from utils import library_source
from utils.library import Item


def _scoring_engine():
    """按配置选择评分引擎（v2 默认；v1 保留用于对比）。

    Returns:
        (rank_items 函数, decision_counts 函数, 是否支持 ai_scores 参数)
    """
    try:
        from config import ToolkitConfig
        eng = ToolkitConfig.load().engine
        if eng == "v1":
            from utils.scoring import rank_items as rk, decision_counts as dc
            return rk, dc, False
    except Exception:  # noqa: BLE001
        pass
    from utils.scoring_v2 import rank_items as rk, decision_counts as dc
    return rk, dc, True


def _ai_extra():
    """读取 AI 参与分类提炼的标签画像；无则返回空。"""
    ai = AIProfileStore(DATA_DIR / "smart.db").load()
    if not ai:
        return (), ()
    return ai["liked_keys"], ai["disliked_keys"]


def _ai_scores(item_ids):
    """读取本地 LLM 语义打分缓存 {item_id: 0..1}；无则返回空 dict。"""
    try:
        from utils.ai_scoring import AIScoreStore
        return AIScoreStore(DATA_DIR / "smart.db").get_many(item_ids)
    except Exception:  # noqa: BLE001
        return {}


def _cfg_enhance() -> bool:
    """增强匹配总开关（设置页可关，便于对比优化前后）。"""
    try:
        from config import ToolkitConfig
        return bool(ToolkitConfig.load().enhance_match)
    except Exception:  # noqa: BLE001
        return True


class AnalyzeWorker(QThread):
    """采集 + 全库评分，一次性产出排序结果与分层计数。

    ``source`` 传配置对象（``ToolkitConfig``）即可——由 ``utils.library_source``
    决定读本地数据库还是走服务器 API（Jellyfin / Emby 通用）。
    也接受数据库路径字符串，以兼容旧调用。
    """

    log = Signal(str)
    progress = Signal(int)
    finished = Signal(dict)  # {"items":[...], "results":[...], "counts":{...}} 或 {"error":str}

    def __init__(self, source, labels: dict, exclude_keywords: list | None = None,
                 th_keep: float | None = None, th_delete: float | None = None,
                 parent=None):
        super().__init__(parent)
        self.source = source
        self.labels = labels
        self.exclude_keywords = exclude_keywords or []
        self.th_keep = th_keep
        self.th_delete = th_delete
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        try:
            items = library_source.collect(
                self.source, log=self.log.emit,
                progress=self.progress.emit,
                should_stop=lambda: self._stopped,
                exclude_keywords=self.exclude_keywords,
            )
            enhance = _cfg_enhance()
            el, ed = _ai_extra() if enhance else ((), ())
            ascores = _ai_scores([it.id for it in items]) if enhance else {}
            rk, dc, supports_ai = _scoring_engine()
            kwargs = dict(th_keep=self.th_keep, th_delete=self.th_delete,
                          enhance=enhance, extra_liked=el, extra_disliked=ed)
            if supports_ai:
                kwargs["ai_scores"] = ascores
            results = rk(items, self.labels, **kwargs)
            counts = dc(results)
            self.finished.emit({"items": items, "results": results, "counts": counts})
        except Exception as e:  # noqa: BLE001
            self.finished.emit({"error": str(e)})


class RerankWorker(QThread):
    """只重排（不重新采集 DB），用于打标后增量更新。"""

    finished = Signal(dict)  # {"results":[...], "counts":{...}}

    def __init__(self, items: list[Item], labels: dict,
                 th_keep: float | None = None, th_delete: float | None = None,
                 parent=None):
        super().__init__(parent)
        self.items = items
        self.labels = labels
        self.th_keep = th_keep
        self.th_delete = th_delete

    def run(self) -> None:
        enhance = _cfg_enhance()
        el, ed = _ai_extra() if enhance else ((), ())
        ascores = _ai_scores([it.id for it in self.items]) if enhance else {}
        rk, dc, supports_ai = _scoring_engine()
        kwargs = dict(th_keep=self.th_keep, th_delete=self.th_delete,
                      enhance=enhance, extra_liked=el, extra_disliked=ed)
        if supports_ai:
            kwargs["ai_scores"] = ascores
        results = rk(self.items, self.labels, **kwargs)
        counts = dc(results)
        self.finished.emit({"results": results, "counts": counts})
