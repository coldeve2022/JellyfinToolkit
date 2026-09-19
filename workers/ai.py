"""本地 LLM 二次校验 - 通过 OpenAI 兼容接口调用本地部署模型(如 Ollama / LM Studio)。

仅用标准库 urllib，无第三方依赖，打包也能用。默认关闭；在设置页启用并填端点。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal


def query_llm(base_url: str, key: str, model: str, messages: list,
              timeout: int = 180) -> str:
    """调用 /chat/completions，返回助手文本。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": messages, "temperature": 0.2}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


class AIRecheckWorker(QThread):
    """对一批候选做 AI 二次复核（支持「可能喜欢」/「建议删除」两种场景）。

    emit per-item 判定行（num 判定 - 理由）与完成汇总。
    """

    log = Signal(str)
    result = Signal(str)      # "num VERDICT - reason"
    finished = Signal(dict)   # {"total":n, "ok":n, "error":str|None}

    def __init__(self, candidates: list, base_url: str, key: str, model: str,
                 context: str = "delete", parent=None):
        """candidates: [{"num","name","hit","reason","actors","studios","genres"}]
        context: "like"（可能喜欢·值得看吗）或 "delete"（建议删除·会不会误删）"""
        super().__init__(parent)
        self.candidates = candidates
        self.base_url, self.key, self.model = base_url, key, model
        self.context = context

    def run(self) -> None:
        if not self.candidates:
            self.finished.emit({"total": 0, "ok": 0, "error": "无候选"})
            return
        lines = []
        for it in self.candidates[:25]:
            lines.append(
                f"- {it['num'] or '?'} | {it['name'][:40]} | 命中{it['hit']} | "
                f"原因:{it['reason']} | 演员:{','.join(it['actors'][:4])}")
        if self.context == "like":
            head = (
                "以下是按「内容与你已看喜欢的相似度」筛出的【可能喜欢】候选视频。"
                "请逐条判断它是否真的值得看，给出: 格式『番号: KEEP 或 DELETE 或 UNSURE - 简短中文理由』。"
            )
        elif self.context == "review":
            head = (
                "以下是因信息不足/边界而【待复核】的视频。请结合命中信息判断：它更可能值得保留(契合你的口味/质量尚可)还是该删，"
                "给出: 格式『番号: KEEP 或 DELETE 或 UNSURE - 简短中文理由』。"
                "只有明确的才给 KEEP/DELETE，拿不准一律 UNSURE。"
            )
        else:
            head = (
                "以下是按「内容与你已看喜欢的相似度」筛出的【建议删除】候选视频。"
                "请逐条判断是否可能属于误删（低质/与你画像不符/重复），给出: 格式『番号: KEEP 或 DELETE 或 UNSURE - 简短中文理由』。"
            )
        prompt = head + "基于条目信息判断，不要臆造不存在的元数据。\n\n" + "\n".join(lines)
        try:
            text = query_llm(self.base_url, self.key, self.model,
                             [{"role": "user", "content": prompt}])
            self.result.emit(text)
            n = 0
            for raw in text.splitlines():
                s = raw.strip()
                if s and ("KEEP" in s.upper() or "DELETE" in s.upper() or "UNSURE" in s.upper()):
                    self.result.emit(s)
                    n += 1
            self.finished.emit({"total": len(self.candidates), "ok": n, "error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ AI 调用失败: {e}")
            self.finished.emit({"total": len(self.candidates), "ok": 0, "error": str(e)})


class AIReportWorker(QThread):
    """用本地 LLM 把喜好画像统计写成一段深度分析报告。"""

    log = Signal(str)
    result = Signal(str)     # 报告文本
    finished = Signal(dict)

    def __init__(self, profile_summary: dict, base_url: str, key: str, model: str,
                 parent=None):
        super().__init__(parent)
        self.summary = profile_summary
        self.base_url, self.key, self.model = base_url, key, model

    def run(self) -> None:
        p = self.summary
        top_a = "、".join(f"{a['name']}({a['fav']}收藏)" for a in p["top_actors"][:8])
        top_s = "、".join(f"{s['name']}({s['fav']}收藏)" for s in p["top_studios"][:6])
        top_g = "、".join(g["name"] for g in p["top_genres"][:8])
        top_tags = "、".join(f"{t['name']}({t['fav']}收藏)" for t in p.get("top_tags", [])[:8])
        years = "、".join(f"{y}({n}部)" for y, n in p["year_pref"][:6])
        neg_txt = "、".join(f"{k}({n})" for k, n in p.get("neg_features", [])[:6]) or "（暂无）"
        buckets = "、".join(f"{k}:{v}" for k, v in p.get("by_bucket", {}).items()) or "（未知）"
        # 判别力数据：全库标签频率 vs 正样本中出现次数（差值大=你的独有强偏好）
        lib_tags = "、".join(f"{k}(全库{n})" for k, n in p.get("lib_tag_freq", [])[:10])
        pos_tags = "、".join(f"{k}(你{n})" for k, n in p.get("pos_tag_count", [])[:10])
        ud = p.get("unseen_decision", {})
        unseen_txt = f"高匹配{ud.get('like', 0)} / 低匹配{ud.get('delete', 0)} / 待复核{ud.get('review', 0)}"
        prompt = (
            "这是我 Jellyfin 媒体库的喜好画像统计。请写一份 300 字以内的中文「偏好分析报告」，"
            "必须给出有具体依据、可执行的判断，禁止复述数据。\n\n"
            f"【观看画像】看过 {p['n_pos']} 部、收藏 {p['n_fav']}、看完 {p['n_played']}，"
            f"无码偏好 {p['unc_ratio']:.0%}。\n"
            f"- Top 演员：{top_a}；\n- Top 厂商：{top_s}；\n- Top 题材：{top_g}；\n- Top 标签：{top_tags}；\n"
            f"- 年份偏好：{years}；\n- 不喜欢倾向(内容类)：{neg_txt}。\n"
            f"【标签判别力对比】全库标签频率：{lib_tags}。\n你已看作品中标签次数：{pos_tags}。\n"
            f"【未看池】全库 {p.get('total', 0)} 部、未看 {p.get('unwatched', 0)} 部；"
            f"未看判定：{unseen_txt}；分桶：{buckets}。\n"
            "请严格分三节输出：\n"
            "1) 独有强偏好：对比全库频率，点出哪几个标签是你远超全库水平的独特口味（这是你选片的关键）；\n"
            "2) 未看池机会：根据未看判定分层，指出未看池里有多少高匹配作品可以直接看、"
            "哪些大类与你的画像背离最多、建议优先清理；\n"
            "3) 具体行动：给出 1-2 个具体建议（如优先补哪些厂商/演员/题材的未看、哪些系列前缀可删）。"
        )
        try:
            text = query_llm(self.base_url, self.key, self.model,
                             [{"role": "user", "content": prompt}])
            self.result.emit(text)
            self.finished.emit({"error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ AI 调用失败: {e}")
            self.finished.emit({"error": str(e)})


class AIDedupWorker(QThread):
    """让 AI 判定重复组：真重复→指出保留哪个版本/删差；不同作品→整组保留。

    emit result 行（组 num + 保留项）与完成汇总。结果由 UI 应用。
    """

    log = Signal(str)
    result = Signal(str)      # "组<num>: KEEP 或 保留<序号>删除<序号列表> - 理由"
    finished = Signal(dict)   # {"groups":n, "verdicts":n, "error":str|None}

    def __init__(self, groups: list[dict], base_url: str, key: str, model: str,
                 parent=None):
        super().__init__(parent)
        self.groups = groups          # [{num, items:[{id,name,size,is_cd,cd_index,is_uncensored,...}]}]
        self.base_url, self.key, self.model = base_url, key, model

    def run(self) -> None:
        verdicts = 0
        try:
            for g in self.groups:
                items = g["items"]
                if len(items) < 2:
                    continue
                lines = [f"组<{g['num']}> 同一番号的版本："]
                for i, it in enumerate(items, 1):
                    cd_tag = (f"CD{it.get('cd_index') or ''}" if it.get("is_cd") else "完整")
                    size = it.get("size_mb") or ""
                    lines.append(
                        f"  [{i}] {str(it.get('name', ''))[:36]} | {cd_tag} "
                        f"| {'无码' if it.get('is_uncensored') else '有码'} | {size}")
                prompt = (
                    "以下是同一番号的多个文件(同一作品的不同版本或分卷)。任务：判断它们是否真重复；"
                    "若真重复，指出应该保留哪个版本[序号](更完整/更清晰/命名更规范)并删除其余；"
                    "若它们其实是不同文件(如 CD 分卷需全保留)则整组保留。"
                    "只输出『组<番号>: KEEP 或 保留<序号>删除<序号列表> - 一句话理由』。\n\n" + "\n".join(lines)
                )
                text = query_llm(self.base_url, self.key, self.model,
                                 [{"role": "user", "content": prompt}], timeout=90)
                self.result.emit(f"组<{g['num']}>: {text.strip()}")
                verdicts += 1
            self.finished.emit({"groups": len(self.groups), "verdicts": verdicts, "error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ AI 去重判定失败: {e}")
            self.finished.emit({"groups": len(self.groups), "verdicts": verdicts, "error": str(e)})


class AITasteWorker(QThread):
    """让 AI 提炼「喜欢/不喜欢标签画像」，并写回评分规则（存 ai_profile 供重排自动加载）。

    结果存库后，下一次全库评分/打标重排会自动把它并入 liked/disliked 集，
    真正影响分类——而不是只写一篇报告。
    """

    log = Signal(str)
    result = Signal(str)      # 提炼结果摘要
    finished = Signal(dict)   # {"liked":n, "disliked":n, "error":str|None}

    _KIND_CN = {"actor": "演员", "studio": "厂商", "genre": "类型",
                "tag": "标签", "prefix": "系列"}

    def __init__(self, profile_summary: dict, store_path, base_url: str, key: str,
                 model: str, parent=None):
        super().__init__(parent)
        self.summary = profile_summary
        self.store_path = store_path
        self.base_url, self.key, self.model = base_url, key, model

    def run(self) -> None:
        from utils.ai_profile import AIProfileStore, build_ai_prompt, parse_ai_taste
        try:
            prompt = build_ai_prompt(self.summary)
            text = query_llm(self.base_url, self.key, self.model,
                             [{"role": "user", "content": prompt}])
            liked, disliked, notes = parse_ai_taste(text)
            AIProfileStore(self.store_path).save(liked, disliked, notes)
            self.result.emit(self._summary(liked, disliked, notes))
            self.finished.emit({"liked": len(liked), "disliked": len(disliked), "error": None})
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"❌ AI 调用失败: {e}")
            self.finished.emit({"liked": 0, "disliked": 0, "error": str(e)})

    def _summary(self, liked, disliked, notes) -> str:
        def _fmt(keys):
            return "、".join(f"{self._KIND_CN.get(k, k)}:{v}" for k, v in sorted(keys)) or "（无）"
        s = (f"✅ AI 已提炼：喜欢特征 {len(liked)} 项 → {_fmt(liked)}\n"
             f"不喜欢特征 {len(disliked)} 项 → {_fmt(disliked)}\n"
             "已写入评分规则，下次「生成画像」或打标重排即生效。")
        if notes:
            s += f"\n\n{notes}"
        return s
