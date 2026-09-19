"""标签归一与同义合并 - 纯函数层。

用途：把同一概念的不同写法合并为规范 tag，让喜好画像的 Top 标签更干净、命中度更准。
- 日文汉字 ↔ 简体变体统一（顔射→颜射、無碼→无码、髮/髪→发）
- 静态规范映射（确定性）：中出し→中出、潮吹き→潮吹、素人娘→素人、
  ハメ撮り→はめ撮り、美少女系→美少女、個人撮影→个人摄影…
- **只做确定性静态合并，不做跨库统计**：避免像"截断取主流"那样依赖全局count、
  误并 3P/4P、美少女/少女 等碎片词导致全库分类扰动。

本模块只规范特征层的 tag/genre，不影响 Item 原始 genres/tags 的展示字段。
参考：另一套 Jellyfin 工具(nfo-tag-fixer)的 tag_organizer 已实测的词表。
"""
from __future__ import annotations

import re
import unicodedata

# 片假名 → 平假名（同一词不同书写，如 ハメ撮り=はめ撮り；仅用于判定等价）
_KATA2HIRA = {chr(cp): chr(cp - 0x60) for cp in range(0x30A1, 0x30F7)}
# 归一化用标点/空白（含日文长音符 ー）
_PUNCT_RE = re.compile(
    r"[\s\u3000·・,，.。、！!？?：:；;｜|／/\\\-–—_~～*＊\"'「」『』（）()\[\]【】<>〈〉ー～]+")
# 常见日文汉字 ↔ 简体中文 变体（标签常用字）
_KANJI_VAR = {
    "顔": "颜", "體": "体", "髮": "发", "髪": "发", "關": "关", "學": "学",
    "壓": "压", "圖": "图", "畫": "画", "與": "与", "說": "说", "會": "会",
    "覺": "觉", "讓": "让", "發": "发", "點": "点", "萬": "万", "邊": "边",
    "無": "无", "雙": "双", "將": "将", "國": "国", "間": "间", "開": "开",
    "內": "内", "兩": "两", "個": "个", "寫": "写", "實": "实", "隱": "隐",
    "視": "视", "聽": "听", "聲": "声", "審": "审", "讀": "读", "話": "话",
    "語": "语", "對": "对", "見": "见", "愛": "爱", "時": "时", "産": "产",
    "場": "场", "広": "广", "発": "发", "訳": "译", "変": "变", "優": "优",
    "歴": "历", "歳": "岁", "専": "专", "従": "从", "気": "气", "収": "收",
    "運": "运", "選": "选", "連": "连", "達": "达", "東": "东", "児": "儿",
    "組": "组", "総": "总", "絶": "绝", "続": "续", "縄": "绳", "縁": "缘",
    "級": "级", "類": "类", "種": "种", "編": "编", "韓": "韩", "関": "关",
    "碼": "码", "線": "线", "絕": "绝", "澤": "泽", "樣": "样", "機": "机",
    "網": "网", "緒": "绪", "監": "监", "遺": "遗", "覽": "览",
    "複": "复", "雜": "杂", "異": "异", "團": "团", "應": "应", "當": "当",
    "戰": "战", "職": "职", "靈": "灵", "鄉": "乡", "農": "农", "塊": "块",
    "價": "价", "購": "购", "販": "贩", "題": "题", "願": "愿", "額": "额",
    "館": "馆", "飯": "饭", "飲": "饮", "飾": "饰", "馬": "马", "魚": "鱼",
}

# 静态规范映射：变体原词 → 规范 tag（高频、确定性，宁少勿错）
TAG_CANON = {
    # 送假名/口头禅变体
    "中出し": "中出", "潮吹き": "潮吹", "素人娘": "素人",
    "ハメ撮り": "はめ撮り", "個人撮影": "个人摄影",
    # 装饰性后缀（去"系/級"后缀）
    "美少女系": "美少女", "アイドル系": "アイドル", "王牌系": "王牌",
    # 简繁/日汉已由 _KANJI_VAR 覆盖，这里补常见直接写法
    "無碼": "无码",
    "証拠": "证据",
}


def norm_key(name: str) -> str:
    """归一化标签（判定等价用）：NFKC→小写→片假名转平假名→去标点→汉字变体统一。"""
    s = unicodedata.normalize("NFKC", name).lower()
    s = "".join(_KATA2HIRA.get(c, c) for c in s)
    s = _PUNCT_RE.sub("", s)
    s = "".join(_KANJI_VAR.get(c, c) for c in s)
    return s


def canon_tag(name: str) -> str:
    """规范单个 tag：优先静态映射表，其次统一日文汉字→简体；未命中返回原样。

    只做确定性合并；不丢弃、不改语义。
    """
    t = str(name).strip()
    if not t:
        return t
    if t in TAG_CANON:
        return TAG_CANON[t]
    n = norm_key(t)
    if n in _NORM_TO_CANON:
        return _NORM_TO_CANON[n]
    # 统一汉字变体显示（顔→颜 等）
    return "".join(_KANJI_VAR.get(c, c) for c in t)


# 预建 norm形式 → 规范词（让不同书写但归一后相同/在表的写法都能命中）
_NORM_TO_CANON = {}
for _k, _v in TAG_CANON.items():
    _NORM_TO_CANON[norm_key(_k)] = _v
# 凡归一后完全相同于某个规范词，也归一
for _k, _v in list(TAG_CANON.items()):
    _NORM_TO_CANON[norm_key(_v)] = _v


# 技术性/状态/合集等“非内容口味”标签：进入画像会稀释真实偏好。
# 只做展示层/特征层过滤，不改 Item 原始 tags。
NON_CONTENT_TAGS = {
    "4K", "8K", "VR", "3D", "HD", "FHD", "UHD", "SD", "2K",
    "高清", "超清", "字幕", "中文字幕", "无字幕", "中文", "有码", "无码",
    "破解", "流出", "薄码", "薄马赛克", "合集", "精选合集", "精选",
    "经典", "经典老片", "纪录片", "成人电影", "西洋片", "法国", "韩国",
    "台湾", "台湾模特", "主观视角", "出道作品", "首次亮相", "重制版",
    "复刻版", "单体作品", "故事集", "男性形象影片", "写真偶像", "16小时+",
    "4小时+", "ベスト・総集編", "ドキュメンタリー", "イメージビデオ",
}

# 明确的内容口味标签：在展示 Top 标签时优先排前。
PRIORITY_TAGS = {
    "中出", "颜射", "口爆", "口内射精", "足交", "手淫", "乱交", "多P",
    "群交", "NTR", "寝取", "人妻", "痴女", "教师", "学生", "OL", "护士",
    "女仆", "制服", "巨乳", "美少女", "角色扮演", "cosplay", "SM", "束缚",
    "凌辱", "强暴", "轮奸", "放尿", "露出", "野外", "车内", "泳装",
    "丝袜", "黑丝", "白丝", "潮吹", "素人", "熟女",
}


def is_non_content_tag(name: str) -> bool:
    """判断该 tag 是否是技术/状态/合集等非内容偏好标签。"""
    t = str(name).strip()
    if not t:
        return False
    if t in NON_CONTENT_TAGS:
        return True
    n = norm_key(t)
    return n in {norm_key(x) for x in NON_CONTENT_TAGS}


def filter_content_tags(values) -> list[str]:
    """过滤非内容标签，同时做 canon_tag 去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        t = canon_tag(raw)
        if not t or is_non_content_tag(t):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def prioritize_tags(values) -> list[str]:
    """内容标签优先：先过滤非内容标签，再让高优先级口味标签排前。"""
    content = filter_content_tags(values)
    priority = [t for t in content if t in PRIORITY_TAGS]
    others = [t for t in content if t not in PRIORITY_TAGS]
    return priority + others
