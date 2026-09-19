"""演员名清洗 - 纯函数层。

从刮削元数据里提取真实演员名，去掉以下噪声：
- 占位符（素人、複数の素人娘、美人な人妻 等）
- 括号中的系列/年份/国籍/事务所标注（保留罗马音/日文别名）
- 名字前后粘连的描述 token（巨乳女子プロレスラー凛叶 → 凛叶）

只影响评分/画像的特征层，不改 Item.actors 原始展示字段。
"""
from __future__ import annotations

import re

_PLACEHOLDER_TERMS = (
    "素人", "複数", "複数の素人娘", "美人な人妻", "人妻", "熟女",
    "元Fカップグラドル", "女優情報", "管理者様", "編集", "编集",
    "アイドル店員", "ツインテール", "奥さん", "FC2",
    "生意気ツインテール18ちゃん", "生意気ツインテール",
    "抜群なアイドル店員", "モデルボディーの女",
    "地味な眼鏡の巨乳妻", "ギャルママ柔道家", "訳アリ巨乳JD",
    "定時制ギャル", "ギャル２人組", "ギャル2人組", "四十路人妻",
    "素人美熟女ナンパ", "S級素人", "S级素人", "素人奥様", "素人妻",
)

_AGE_RE = re.compile(r"\d+[歳才岁歲]")
_YEAR_RE = re.compile(r"【?\d{4}年?】?")

_SERIES_TAG_RE = re.compile(
    r"パコパコママ|エッチな0930|エッチな4610|天然むすめ|ラグジュTV|"
    r"1000人斬り|人妻斬り|人妻DX|カリビアンコムプレミアム|マドンナ|"
    r"ソープ嬢|トリプルエックス|ニューハーフ|クリスタル|GirlsDelta|"
    r"無垢|カリビアン|ガチん娘|ムラムラ|DMM素人動画|FC2ライブ|元Fカップ",
    re.IGNORECASE,
)

_ANNOTATION_TERMS = re.compile(
    r"英国|ハンガリー|ベトナム|イングランド|俄罗斯混血|USA|JPN|泰国|美国|韩国|"
    r"JETSTREAM|T-POWERS|HEYZO|FALENO|LINX|Gcolle|KUKI|kira☆kira|RealShodo|"
    r"GOT刊|Playboy|Fleur|きらきら|着エロ|ヌードイメージ|女王様|嫁|同人モデル|"
    r"仮|TS|登録|シンデレラオーディショングランプリ|本名|2代目|第\d+期生|デビュー|ニューハーフ",
    re.IGNORECASE,
)

_DESC_TOKEN_RE = re.compile(
    r"店の女|契約|交尾|プロレスラー|の女|禁断|巨乳|耳かき|バニーコレクション|"
    r"の妻|ナンパ|愛人|の義母|の生徒|の同僚|の幼馴染|の彼女|顔出し|ギャル|"
    r"制服|メイド|看護師|店員|先生|会長|の娘|素人|熟女|人妻|ランジェリーナ|"
    r"世田谷の妻|淫乱|レーベル|在宅ワーカー|家賃滞納|いいなり|温泉旅行|"
    r"ワリキリ|ワリキリバイト|バイト|湘南の女|発禁|患者|万引き娘|夫から逃げる",
    re.IGNORECASE,
)

_DESC_PREFIX_RE = re.compile(
    r"^(巨乳女子プロレスラー|巨尻女子プロレスラー|巨乳ヒール女子プロレスラー|"
    r"女子プロレスラー|素人|S級素人|S级素人|色白美巨乳Gカップ美女|"
    r"美人な人妻|抜群なアイドル店員|ギャル２人組|ギャル2人組|"
    r"複数の素人娘|复数の素人娘|素人美熟女ナンパ|素人庭園|"
    r"しろハメ素人|俺の素人-Z-|E★人妻DX|泌尿器科女医|幼稚園先生|"
    r"メイドカフェ店員|巨乳アパレル店員|可愛すぎるス○バ店員|"
    r"色白152cmあざと可愛いコスメ店員)"
)

_DESC_SUFFIX_RE = re.compile(
    r"(先生|女医|メイドカフェ店員|ス○バ店員|アパレル店員|コスメ店員|"
    r"店員|幼稚園先生|美人妻|人妻看護婦|妻たち|人妻|プロレスラー)$|"
    r"^(幼なじみの|アラフィフ|五十路|三十六歳|36歳)"
)

_DESC_PURE_RE = re.compile(
    r"^(定時制ギャル|訳アリ巨乳JD|モデルボディーの女|地味な眼鏡の巨乳妻|"
    r"ギャルママ柔道家|美人な人妻|複数の素人娘|复数の素人娘|"
    r"抜群なアイドル店員|ギャル２人組|ギャル2人組|四十路人妻|"
    r"素人美熟女ナンパ|S級素人|S级素人|素人奥様|素人不明|素人多数|"
    r"素人娘|素人娘达|素人娘達|素人品評会|素人妻|素人人物不明)$"
)

_KNOWN_ALIAS_RE = re.compile(r"[（(](はんな|結城あかり|瑞穂このみ|アニー麗)[)）]")


def _is_placeholder(name: str) -> bool:
    if not name:
        return True
    s = str(name).strip()
    if not s:
        return True
    for term in _PLACEHOLDER_TERMS:
        if term in s:
            return True
    body = re.sub(r"[\(（][^\(（）]*?[\)）]", "", s).strip()
    if _AGE_RE.search(body):
        return True
    return _DESC_PURE_RE.match(body) is not None


def _strip_series_year(name: str) -> str:
    s = str(name)
    s = re.sub(
        r"[\(（]([^\(（）]*?)[\)）]",
        lambda m: "" if (_SERIES_TAG_RE.search(m.group(1))
                          or _YEAR_RE.fullmatch(m.group(1)))
        else m.group(0),
        s,
    )
    s = re.sub(r"[\(（\[]\d{4}年?[\)）\]]", "", s)
    s = re.sub(r"\d{4}年?$", "", s)
    s = re.sub(r"FC2ライブ$", "", s)
    s = re.sub(r"元Fカップグラドル$", "", s)
    return s.strip()


def _strip_annotation(name: str) -> str:
    s = str(name).strip()
    s = s.replace("【", "[").replace("】", "]").replace("（", "(").replace("）", ")")

    def _should_strip(inner: str) -> bool:
        inner = inner.strip()
        if not inner:
            return True
        if _YEAR_RE.fullmatch(inner):
            return True
        if _SERIES_TAG_RE.search(inner) or _ANNOTATION_TERMS.search(inner):
            return True
        # 括号里是日/韩/中文字符，更可能是别名/读音，保留。
        return False

    s = re.sub(r"\[([^\[\]]*)\]",
               lambda m: "" if _should_strip(m.group(1)) else m.group(0), s)
    s = re.sub(r"\(([^()]*)\)",
               lambda m: "" if _should_strip(m.group(1)) else m.group(0), s)
    if s.count("[") != s.count("]") or s.count("(") != s.count(")"):
        s = re.sub(r"[\[\]\(\)]", "", s)
    return s.strip()


def _strip_desc_tokens(name: str) -> str:
    parts = [p.strip() for p in str(name).split(" ") if p.strip()]
    kept = [p for p in parts if not _DESC_TOKEN_RE.search(p)]
    return " ".join(kept) if kept else ""


def _strip_compact_desc(name: str) -> str:
    if not name:
        return name
    if _DESC_PURE_RE.match(name):
        return ""
    s = name
    m = _DESC_PREFIX_RE.match(s)
    if m:
        s = s[m.end():].strip()
    m2 = _DESC_SUFFIX_RE.search(s)
    if m2:
        s = s[:m2.start()].strip()
    return s.strip()


def clean_actor_name(name) -> str:
    """清洗单个演员名，返回可用的名字或空串。"""
    s = str(name or "").strip()
    if not s or _is_placeholder(s):
        return ""
    s = _strip_series_year(s)
    s = _strip_annotation(s)
    if not s or _is_placeholder(s):
        return ""
    s = _strip_compact_desc(s)
    if not s or _is_placeholder(s):
        return ""
    s = _strip_desc_tokens(s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,、;；/")
    return s


def clean_actors(values) -> list[str]:
    """批量清洗演员名：去空、去噪、去重、保序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = clean_actor_name(raw)
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out
