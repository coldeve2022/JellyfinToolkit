"""媒体库数据采集的纯函数层 — 从 Jellyfin 数据库读取并构造成统一模型。

依赖：仅标准库(sqlite3)，与 Qt 完全解耦，可单元测试。
本模块只读数据库，绝不写入任何数据。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from utils.nfo_source import read_nfo_features, read_nfo_title

# ── 分类桶 ──
BUCKET_JAV = "jav"                  # 常规有码番号
BUCKET_UNC = "uncensored"           # 无码 / FC2 / PPV 等
BUCKET_HENTAI = "hentai"            # 里番 / H 动画（opiumud / kuroinu 等）
BUCKET_TRAILER = "trailer"          # 预告
BUCKET_OTHER = "other"              # 无法判断

UNCENSORED_PREFIX = {
    "FC2", "PPV", "HEYZO", "CARIB", "1PONDO", "TOKYOHOT", "HEYDOUGA",
    "MURAMURA", "PACOPACO", "GACHINCO", "10MU", "LUXU", "SIRO", "SORA",
}
HENTAI_MARKERS = [
    "opiumud", "kuroinu", "裏番", "里番", "hanime",
    "pixy", "mugen", "乳交", "yoshiwara",
]


@dataclass
class Item:
    """一部作品的统一模型（聚合 DB 元数据 + 行为 + 文件事实）。"""
    id: str
    name: str
    path: str
    type_cn: str                            # Movie / Video / Trailer 简短名
    year: Optional[int]
    genres: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    studios: list = field(default_factory=list)
    actors: list = field(default_factory=list)
    size: Optional[int] = None              # 字节
    num: Optional[str] = None               # 番号核心，如 MIAA-743、FC2-123456
    is_cd: bool = False                     # 是否 CD 分卷
    cd_index: Optional[int] = None
    bucket: str = BUCKET_OTHER              # 大类分桶
    is_uncensored: bool = False
    unknown_actor: bool = False
    # 行为
    is_favorite: bool = False
    play_count: int = 0
    is_played: bool = False
    has_progress: bool = False
    last_played: Optional[str] = None
    rating: Optional[float] = None
    cover: Optional[str] = None           # 本地封面图路径（打标卡片预览用，允许为空）

    @property
    def size_mb(self) -> Optional[float]:
        if self.size is None:
            return None
        return round(self.size / (1024 * 1024), 1)

    @property
    def has_cover(self) -> bool:
        """是否有可用的本地封面。"""
        return bool(self.cover and os.path.isfile(self.cover))


def _split_csv(s: Optional[str]) -> list:
    if not s:
        return []
    return [x.strip() for x in str(s).split(",") if x.strip()]


def extract_number(name: str) -> Optional[str]:
    """从文件名/标题提取番号核心，含 FC2 长数字与常规 字母-数字。"""
    if not name:
        return None
    n = re.sub(r"\.(mp4|mkv|avi|ts|mov|wmv|flv|rmvb)$", "", name, flags=re.I)
    m = re.search(r"\bfc2\s*[-_ .]?\s*(\d{5,7})", n, re.I)
    if m:
        return f"FC2-{m.group(1)}"
    m = re.search(r"(?<![A-Za-z0-9])([A-Za-z]{2,8})\s*[-_ ]\s*(\d{2,6})(?![0-9])", n)
    if not m:
        m = re.search(r"(?<![A-Za-z0-9])([A-Za-z]{2,8})(\d{2,6})(?![0-9])", n)
    if m:
        return f"{m.group(1).upper()}-{m.group(2)}"
    return None


def detect_cd(name: str) -> tuple[Optional[int], bool]:
    """检测是否为 CD 分卷（FC2-xxx-cd3 等），返回 (cd编号, 是否CD)。"""
    if not name:
        return None, False
    m = re.search(r"(?:^|[-_.\s])[cC][dD]\s*(\d{1,2})", name)
    if m:
        return int(m.group(1)), True
    return None, False


_BARE_CODE_RE = re.compile(
    r"^(?:(?:fc2)\s*[-_. ]?\s*(?:ppv\s*[-_. ]?)?\d{5,7}"
    r"|[a-z]{2,8}\s*[-_. ]?\s*\d{2,6})"
    r"(?:\s*[-_. ]?\s*(?:cd|disc|part)\s*\d{1,2})*$",
    re.IGNORECASE,
)


def is_bare_code_name(name: str) -> bool:
    """判断名称是否只是裸番号（如 FC2-3581881 / MIAA-743-cd1），没有可读标题。"""
    if not name:
        return True
    s = name.strip()
    if not s:
        return True
    return bool(_BARE_CODE_RE.fullmatch(s))


def classify_bucket(name: str, path: str, num: Optional[str], type_full: str) -> tuple[str, bool]:
    """判定大类分桶与是否无码。"""
    low = f"{name} {path}".lower()
    typ = type_full.split(".")[-1].lower() if type_full else ""

    if "trailer" in typ or "/trailer" in low or name.lower().startswith("trailer"):
        return BUCKET_TRAILER, False

    if any(marker in low for marker in HENTAI_MARKERS):
        return BUCKET_HENTAI, False

    if num:
        prefix = num.split("-")[0].upper()
        if prefix in UNCENSORED_PREFIX or prefix == "FC2":
            return BUCKET_UNC, True
        return BUCKET_JAV, False

    return BUCKET_OTHER, False


def _readonly_conn(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)


# ── 数据库架构探测 ──────────────────────────────────────────
# Jellyfin 10.9+ 换成 EF Core 生成的 jellyfin.db（BaseItems/Peoples/...）；
# Jellyfin 10.8 及更早、以及 Emby 用的是 library.db（TypedBaseItems，元数据在 data 列 JSON 里）。
# 两者表结构完全不同，SQL 不能混用，必须先探测再分派。
SCHEMA_JELLYFIN_EF = "jellyfin_ef"     # BaseItems 架构（Jellyfin 10.9+）
SCHEMA_LEGACY = "legacy"               # TypedBaseItems 架构（Jellyfin ≤10.8 / Emby）
SCHEMA_UNKNOWN = "unknown"


def table_names(conn: sqlite3.Connection) -> set:
    """列出库中所有表名（小写）。"""
    try:
        return {str(r[0]).lower() for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    except sqlite3.Error:
        return set()


def detect_db_schema(db_path: str | Path) -> str:
    """判断一个 SQLite 文件属于哪种媒体服务器架构。

    Returns:
        ``jellyfin_ef`` / ``legacy`` / ``unknown``（文件不存在或读不了也返回 unknown）
    """
    p = Path(db_path)
    if not p.is_file():
        return SCHEMA_UNKNOWN
    try:
        conn = _readonly_conn(str(p))
    except Exception:  # noqa: BLE001
        return SCHEMA_UNKNOWN
    try:
        names = table_names(conn)
    finally:
        conn.close()
    if "baseitems" in names:
        return SCHEMA_JELLYFIN_EF
    if "typedbaseitems" in names:
        return SCHEMA_LEGACY
    return SCHEMA_UNKNOWN


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    """PRAGMA 读列名（小写）；表不存在返回空集。"""
    try:
        return {str(r[1]).lower() for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _guid_str(value) -> str:
    """把 legacy 架构里的 guid 统一成字符串。

    Emby / Jellyfin ≤10.8 把 .NET Guid 存成 16 字节 BLOB（``Guid.ToByteArray()``
    的小端混合序），也可能存成 TEXT。两种都兜住，保证同一台机器上 id 稳定。
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)) and len(value) == 16:
        try:
            return str(uuid.UUID(bytes_le=bytes(value)))
        except Exception:  # noqa: BLE001
            return bytes(value).hex()
    return str(value).strip().strip("{}")


def _json_field(payload: dict, *names):
    """从 data JSON 里按候选键名取值（大小写不敏感），找不到返回 None。"""
    if not isinstance(payload, dict):
        return None
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def _as_list(value) -> list:
    """把 JSON 字段收敛成字符串列表（单值也接受）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    return [text] if text else []



# 文件名含这些关键词的图片通常是背景/横幅/缩略图，不是封面
_COVER_EXCLUDE = ("fanart", "banner", "thumb", "backdrop", "logo",
                  "clearlogo", "scene", "extra", "spread", "chapter")
_COVER_EXT = (".jpg", ".jpeg", ".png", ".webp")


def split_path_name(p: str) -> str:
    """取路径末端的文件名 —— **同时认 ``/`` 和 ``\\``**。

    Jellyfin / Emby 库里存的路径可能是另一个平台写下的（库在 Windows 上刮削、
    之后服务迁到 Linux，库里存的仍是反斜杠分隔的路径），而 POSIX 的
    ``os.path.basename`` 不把反斜杠当分隔符，会把整串当成文件名 ——
    于是附属文件判定、封面查找全部失效。这是实测出来的，不是理论问题。
    """
    return str(p).replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1]


#: 附属文件（预告片/主题视频/背景图等）的固定文件名（不含扩展名）。
#: Jellyfin 生成这些文件用于界面展示，**不是正片** —— 不该参与分析、修复、
#: 更不该给它生成字幕。
AUXILIARY_STEMS = frozenset({
    "trailer", "theme", "theme_video", "theme-video", "theme_videos",
    "theme_video_1", "backdrop", "poster", "sample", "thumb", "fanart",
    "behind the scenes", "deleted scenes", "interview", "featurette", "short",
})

#: 预告片/样片的两种写法：`xxx-trailer`、`xxx.sample`。
#: ``[\s._\-]`` 是分隔符（下划线、点、连字符、空格都算）。
#: 刻意**不做** ``trailer-xxx`` 的"前缀"判定 —— 那会把正片
#: 《Trailer Park Boys》误判成预告片（实测踩过），而前缀写法本身很少见。
_AUX_SUFFIX_RE = re.compile(r"(?:^|[\s._\-])(trailer|sample)s?$", re.I)

#: ``theme_video`` 是最明确的附属标记：正常片名不会带下划线的这个词，
#: 所以用"包含"判定；从而同时覆盖 ``theme_video.mp4`` 与
#: ``ABC-001-theme_video.mp4`` 这两种 Jellyfin 命名。
_AUX_THEME_VIDEO_RE = re.compile(r"theme[\s._\-]?video", re.I)

#: 附属**目录**名（按完整路径段匹配）。
#: 按段匹配而不是"路径里含这个字串"，是为了避免把正片名里的字串当成目录名。
AUXILIARY_PATH_SEGMENTS = (
    "/trailers/", "/trailer/", "/backdrops/", "/backdrop/", "/extras/",
    "/featurettes/", "/shorts/", "/scenes/", "/interviews/",
    "/behind the scenes/", "/deleted scenes/", "/theme videos/",
    "/theme_videos/", "/theme-videos/", "/others/", "/specials/",
)


def is_junk_attachment_path(path: str, name: Optional[str] = None) -> bool:
    """判断一个路径是否为 Jellyfin 生成的附属文件（非正片）。

    与 scoring.is_junk_item 的判定保持一致，但仅基于"文件名/路径"、
    不需要 Item 对象——供只有文件路径的场景复用（如视频修复转码页）：
    theme / theme_video / backdrop / fanart / trailer 等是 Jellyfin 为生成
    缩略图或预告片而生成的，跟视频本体无关，不应作为"正片"参与分析与修复。

    Args:
        path: 文件完整路径（或目录路径）
        name: 文件名（不提供时取 path 的文件名），可含/不含后缀均可

    Returns:
        True = 附属文件（应排除），False = 可能是正片
    """
    if not (path or name):
        return False
    base = name or split_path_name(str(path))
    if not base:
        return False
    # 去掉扩展名，与 Jellyfin 附属命名（如 theme_video / theme / backdrop / fanart\d*）比对
    stem = re.sub(r"\.(mp4|mkv|avi|mov|wmv|flv|ts|rmvb|jpg|jpeg|png|webp|gif)$",
                  "", base, flags=re.I).strip().lower()
    if stem in AUXILIARY_STEMS:
        return True
    if re.fullmatch(r"fanart\d*", stem):
        return True
    if _AUX_THEME_VIDEO_RE.search(stem):
        return True
    if _AUX_SUFFIX_RE.search(stem):
        return True
    # 路径里含 Jellyfin 的附属目录（trailers/backdrops/extras…）
    pth = str(path).lower().replace(chr(92), "/")
    if any(seg in pth for seg in AUXILIARY_PATH_SEGMENTS):
        return True
    return False


def _dir_listing(directory: str, cache: Optional[dict] = None) -> Optional[list]:
    """列举目录内容，可选带缓存。

    为什么需要缓存：同一个目录里往往有几十上百个条目，逐个 ``os.listdir``
    就是重复劳动；而媒体库放在网络盘（NAS）上时每次 listdir 都是几十毫秒的
    网络往返——上千条目就是几十秒。缓存只活在**一次采集**内（由调用方传入
    一个 dict），所以不会出现"新增海报后刷新不出现"的陈旧问题。
    """
    if cache is not None and directory in cache:
        return cache[directory]
    try:
        files = os.listdir(directory)
    except OSError:
        files = None
    if cache is not None:
        cache[directory] = files
    return files


def find_cover(video_path: str, num: Optional[str] = None,
               cd_index: Optional[int] = None,
               cache: Optional[dict] = None) -> Optional[str]:
    """在视频所在目录中找本地封面图，返回路径或 None。

    匹配优先级（避免"文件夹里塞了一大堆 poster 却取错"）：
    1. 文件名含当前番号实体的 jpg/png（按番号核心匹配，如 MIAA-743、FC2-123456），
       若为 CD 分卷则优先 cd 后缀版本；
    2. 目录里唯一的封面候选（排除 fanart/backdrop 等干扰）；
    3. folder.jpg / poster.jpg / cover.jpg 等通用名。

    Args:
        video_path: 视频文件完整路径
        num: 番号核心（如 MIAA-743、FC2-123456），可为空
        cd_index: CD 分卷编号（1 起），可为空
        cache: 可选的目录列举缓存（同一批采集内传递同一个 dict 即可大幅减少 IO）

    Returns:
        本地图片路径或 None
    """
    if not video_path:
        return None
    d = os.path.dirname(video_path)
    if not d or not os.path.isdir(d):
        return None
    files = _dir_listing(d, cache)
    if files is None:
        return None
    cands = [f for f in files if f.lower().endswith(_COVER_EXT)]
    if not cands:
        return None
    # 排除干扰图
    keep = [f for f in cands if not any(k in f.lower() for k in _COVER_EXCLUDE)]
    if not keep:
        keep = cands

    # 1) 番号匹配
    if num:
        norm = re.sub(r"[\s._\-]+", "", num.upper())
        # 去掉文件名后缀里最常见的干扰（-poster、_破解、空白等）再比对番号前缀
        def _norm_base(f: str) -> str:
            return re.sub(r"[\s._\-]+", "", os.path.splitext(f)[0].upper())
        postered = []
        for f in keep:
            b = _norm_base(f)
            if norm in b or b.startswith(norm):
                postered.append((f, b))
        if postered:
            # 明确含 cd 后缀的单独一组
            def _is_cd(f: str) -> bool:
                return bool(re.search(r"[cC][dD]\s*\d{1,2}(?![0-9])", f))
            # 请求 cd_index 时优先精确对应 cd 编号；否则优先"无 cd 后缀"的基础封面
            if cd_index:
                cd_pick = [f for f, _ in postered if re.search(
                    rf"[cC][dD]\s*{cd_index}(?![0-9])", f)]
                if cd_pick:
                    return os.path.join(d, sorted(cd_pick)[0])
                plain = [f for f, _ in postered if not _is_cd(f)]
                if plain:
                    return os.path.join(d, sorted(plain)[0])
            plain = [f for f, _ in postered if not _is_cd(f)]
            if plain:
                return os.path.join(d, sorted(plain)[0])
            # 只有 cd 版 → 兜底取第一张
            return os.path.join(d, sorted(f for f, _ in postered)[0])

    # 2) 通用名
    for generic in ("folder.jpg", "poster.jpg", "cover.jpg", "fanart.jpg"):
        for f in keep:
            if f.lower() == generic:
                return os.path.join(d, f)

    # 3) 唯一候选兜底
    if len(keep) == 1:
        return os.path.join(d, keep[0])
    return None


# Jellyfin BaseItemImageInfos.ImageType 的语义（0=主封面Primary 2=fanart背景 5=截图缩略图）
_JF_IMAGE_PRIMARY = 0
_JF_IMAGE_SCREENSHOT = 5
_JF_IMAGE_FANART = 2


def load_primary_covers(db_path: str, log: Optional[Callable[[str], None]] = None) -> dict[str, str]:
    """从 jellyfin.db 的 BaseItemImageInfos 加载每部作品的封面路径。

    优先级：主封面(ImageType=0) > 视频帧截图(ImageType=5) > 背景图(ImageType=2)。
    不少视频没有刮削海报，但 Jellyfin 会生成视频帧截图；这些截图同样能帮助
    打标时辨认内容，因此作为主封面缺失时的兜底。只返回实际存在的本地文件路径。

    Args:
        db_path: jellyfin.db 路径
        log: 可选日志回调
    Returns:
        {item_id: 封面本地路径}，仅含文件确实存在的条目
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    result: dict[str, str] = {}
    db_path = Path(db_path)
    if not db_path.exists():
        return result
    try:
        conn = _readonly_conn(str(db_path))
    except Exception:  # noqa: BLE001
        return result
    c = conn.cursor()
    try:
        c.execute("PRAGMA query_only=ON")
        rows = c.execute(
            "SELECT ItemId, Path, ImageType FROM BaseItemImageInfos "
            "WHERE ImageType IN (?, ?, ?) AND Path IS NOT NULL AND Path != '' "
            "ORDER BY CASE ImageType WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END",
            (_JF_IMAGE_PRIMARY, _JF_IMAGE_SCREENSHOT, _JF_IMAGE_FANART,
             _JF_IMAGE_PRIMARY, _JF_IMAGE_SCREENSHOT, _JF_IMAGE_FANART),
        ).fetchall()
        _log(f"读取封面映射（主封面/截图/背景）：{len(rows)} 条")
        for item_id, path, image_type in rows:
            if path.startswith("http://") or path.startswith("https://"):
                continue
            if item_id in result:
                continue
            # 去掉 DB 里可能残留的引号/空白，再校验存在性
            p = str(path).strip().strip('"').strip("'")
            if p and os.path.isfile(p):
                result[item_id] = p
        _log(f"  -> 有效封面 {len(result)} 条")
    except Exception as e:  # noqa: BLE001
        _log(f"主封面读取失败：{e}")
    finally:
        conn.close()
    return result


def build_item(
    *,
    item_id: str,
    name: str,
    path: str,
    type_full: str = "",
    year=None,
    genres=None,
    tags=None,
    studios=None,
    actors=None,
    size=None,
    cover: Optional[str] = None,
    behavior: Optional[dict] = None,
    nfo_fallback: bool = True,
    listing_cache: Optional[dict] = None,
) -> Item:
    """把"各数据源给出的原始字段"归一成 ``Item``。

    三个数据源（Jellyfin EF 架构库 / 传统 TypedBaseItems 库 / REST API）的字段
    名字与结构都不一样，但**归一后的判定逻辑必须一致**——否则同一个库用不同方式
    读出来会得到不同的分桶、不同的番号、不同的评分，用户会以为程序算错了。

    所以番号提取、CD 分卷识别、大类分桶、裸番号补标题、无标签时用 ``.nfo`` 兜底
    这些规则**只在这里写一遍**，三个采集器都调它。
    """
    path = path or ""
    name = name or ""
    genres_list = [str(x).strip() for x in (genres or []) if str(x).strip()]
    tags_list = [str(x).strip() for x in (tags or []) if str(x).strip()]
    studios_list = [str(x).strip() for x in (studios or []) if str(x).strip()]

    num = extract_number(name or path)
    cd_idx, is_cd = detect_cd(name)
    bucket, is_unc = classify_bucket(name, path, num, type_full)

    display_name = name
    if is_bare_code_name(display_name):
        # 标题只是裸番号时，用同目录 .nfo 里的可读标题代替
        nfo_title = read_nfo_title(path)
        if nfo_title:
            display_name = nfo_title

    if nfo_fallback and not genres_list and not tags_list:
        # 库里没有任何标签时，用视频旁刮削 .nfo 兜底（防同步缺失导致"无特征"）
        n_tags, _n_genres, n_studios = read_nfo_features(path)
        if n_tags:
            tags_list = n_tags
        if n_studios and not studios_list:
            studios_list = n_studios

    if not cover:
        cover = find_cover(path, num, cd_idx, cache=listing_cache)

    item = Item(
        id=item_id,
        name=display_name,
        path=path,
        type_cn=type_full.split(".")[-1] if type_full else "",
        year=year,
        genres=genres_list,
        tags=tags_list,
        studios=studios_list,
        actors=list(actors or []),
        size=size,
        num=num,
        is_cd=is_cd,
        cd_index=cd_idx,
        bucket=bucket,
        is_uncensored=is_unc,
        unknown_actor="未知演员" in path or "未知女优" in path,
        cover=cover,
    )
    for key, value in (behavior or {}).items():
        if hasattr(item, key):
            setattr(item, key, value)
    return item


def _collect_ef(
    db_path: str,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_keywords: Optional[list] = None,
) -> list[Item]:
    """读取 Jellyfin 10.9+ 的 ``jellyfin.db``（EF Core 架构），构造全库 Item 列表。

    Args:
        db_path: jellyfin.db 路径
        log / progress / should_stop: 可选回调（进度 0-100）
        exclude_keywords: 路径含这些关键词的目录不参与分析（如动漫库），None 为不过滤
    Returns:
        list[Item]
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    conn = _readonly_conn(str(db_path))
    c = conn.cursor()
    c.execute("PRAGMA query_only=ON")
    try:
        # 1. 视频条目
        _log("读取视频条目...")
        c.execute(
            "SELECT Id, Name, Path, ProductionYear, Genres, Tags, Studios, Size, Type "
            "FROM BaseItems WHERE IsFolder=0 AND MediaType='Video'"
        )
        rows = c.fetchall()
        _log(f"  -> {len(rows)} 条视频")

        # 2. 演员映射（一次全量加载）
        _log("读取演员映射...")
        actors_map: dict[str, list[str]] = defaultdict(list)
        for item_id, name in c.execute(
            "SELECT m.ItemId, p.Name FROM PeopleBaseItemMap m "
            "JOIN Peoples p ON p.Id = m.PeopleId"
        ):
            actors_map[item_id].append(name)

        # 3. UserData 行为（单用户）
        _log("读取播放记录...")
        behavior_map: dict[str, dict] = {}
        for r in c.execute(
            "SELECT ItemId, IsFavorite, PlayCount, Played, PlaybackPositionTicks, "
            "LastPlayedDate, Rating FROM UserData"
        ):
            item_id, fav, pc, played, pos, last, rating = r
            behavior_map[item_id] = {
                "is_favorite": bool(fav),
                "play_count": pc or 0,
                "is_played": bool(played),
                "has_progress": bool(pos and pos > 0),
                "last_played": last,
                "rating": rating,
            }
    finally:
        conn.close()

    # 4. 主封面映射：Jellyfin ImageType=0 (Primary 海报) —— 复用纯函数，逻辑统一、可单测
    try:
        cover_map = load_primary_covers(str(db_path), log=_log)
    except Exception as e:  # noqa: BLE001
        _log(f"主封面读取失败：{e}")
        cover_map = {}

    exclude = [k.lower() for k in (exclude_keywords or []) if k]
    items: list[Item] = []
    total = len(rows)
    skipped = 0
    # 同一目录里的条目共用一份列举结果（库在网络盘上时这是扫描速度的关键）
    listing_cache: dict = {}
    for i, (item_id, name, path, year, genres, tags, studios, size, type_full) in enumerate(rows):
        if should_stop and should_stop():
            break
        path = path or ""
        if exclude and any(k in path.lower() for k in exclude):
            skipped += 1
            continue
        items.append(build_item(
            item_id=item_id, name=name, path=path, type_full=type_full,
            year=year,
            genres=_split_csv(genres), tags=_split_csv(tags),
            studios=_split_csv(studios),
            actors=actors_map.get(item_id, []),
            size=size,
            # 封面优先级：服务器主封面(ImageType=0 刮削海报) > 视频目录 sidecar 盲猜
            cover=cover_map.get(item_id),
            behavior=behavior_map.get(item_id),
            listing_cache=listing_cache,
        ))
        if progress and total:
            progress(int((i + 1) / total * 100))

    _log(f"✅ 采集完成：{len(items)} 部（按范围排除 {skipped} 条）")
    return items


# 传统架构里 type 列的取值形如 "MediaBrowser.Controller.Entities.Movies.Movie"，
# 只有含这些片段的行才是我们关心的视频类条目（排除音乐、相册、播放列表等）
_LEGACY_VIDEO_TOKENS = ("video", "movie", "episode", "trailer")


def _collect_legacy(
    db_path: str,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_keywords: Optional[list] = None,
) -> list[Item]:
    """读取传统架构库（``TypedBaseItems``）—— Emby 与 Jellyfin 10.8 及更早。

    与 EF 架构的关键差异：Genres / Tags / Studios / ProductionYear / Size 这些
    元数据不在一等列里，而是塞在 ``data`` 列的 JSON 中，且不同版本的键名不完全一致。

    因此这里**只按候选键名宽容取值**，取不到就留空——取不到标签的条目会自动走
    视频旁 ``.nfo`` 兜底（与 EF 架构共用同一套逻辑），不会静默产出错数据。
    实际从库里拿到了多少标签，会写进日志，便于排查。

    行为数据（收藏/播放次数）在 Jellyfin ≤10.8 的 library.db 里有 ``UserData`` 表；
    Emby 把它放在独立的 ``users.db`` 里，只给库文件是读不到的——这种情况会明确
    在日志里说明，而不是伪造成"没有播放记录"。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    conn = _readonly_conn(str(db_path))
    try:
        c = conn.cursor()
        c.execute("PRAGMA query_only=ON")
        cols = _column_names(conn, "TypedBaseItems")
        if not cols or "guid" not in cols:
            raise ValueError(
                f"该文件不是 Emby / Jellyfin≤10.8 的库（缺少 TypedBaseItems.guid）：{db_path}")

        wanted = ["guid", "type", "data", "path", "name", "isfolder"]
        sel = [w for w in wanted if w in cols]
        sql = f"SELECT {', '.join(sel)} FROM TypedBaseItems"
        if "isfolder" in cols:
            sql += " WHERE IsFolder=0"
        rows = c.execute(sql).fetchall()
        pos = {name: i for i, name in enumerate(sel)}
        _log(f"  -> 传统架构（TypedBaseItems）：{len(rows)} 条非目录条目")

        # 行为数据（可选）
        behavior_map: dict[str, dict] = {}
        tables = table_names(conn)
        ud_table = next((t for t in ("userdata", "userdatas") if t in tables), None)
        if ud_table:
            ucols = _column_names(conn, ud_table)
            if {"itemid", "isfavorite"} <= ucols:
                pick = [k for k in ("itemId", "IsFavorite", "PlayCount", "Played",
                                    "PlaybackPositionTicks", "LastPlayedDate", "Rating")
                        if k.lower() in ucols]
                for r in c.execute(f"SELECT {', '.join(pick)} FROM {ud_table}"):
                    rec = dict(zip(pick, r))
                    iid = _guid_str(rec.get("itemId"))
                    if not iid:
                        continue
                    behavior_map[iid] = {
                        "is_favorite": bool(rec.get("IsFavorite")),
                        "play_count": int(rec.get("PlayCount") or 0),
                        "is_played": bool(rec.get("Played")),
                        "has_progress": bool((rec.get("PlaybackPositionTicks") or 0) > 0),
                        "last_played": rec.get("LastPlayedDate"),
                        "rating": rec.get("Rating"),
                    }
                _log(f"  · 播放记录：{len(behavior_map)} 条（来自 {ud_table}）")
            else:
                _log(f"  · {ud_table} 表结构不含 itemId/IsFavorite，已跳过播放记录")
        else:
            _log("  · 未找到播放记录表——Emby 的行为数据在独立的 users.db 里，"
                 "仅凭库文件读不到（收藏/播放数将按 0 处理）")
    finally:
        conn.close()

    exclude = [k.lower() for k in (exclude_keywords or []) if k]
    items: list[Item] = []
    total = len(rows)
    skipped = 0
    no_tags = 0
    listing_cache: dict = {}
    for i, row in enumerate(rows):
        if should_stop and should_stop():
            break
        item_id = _guid_str(row[pos["guid"]])
        type_full = str(row[pos["type"]] or "") if "type" in pos else ""
        if not any(tok in type_full.lower() for tok in _LEGACY_VIDEO_TOKENS):
            continue
        path = str(row[pos["path"]] or "") if "path" in pos else ""
        if exclude and any(k in path.lower() for k in exclude):
            skipped += 1
            continue
        name = str(row[pos["name"]] or "") if "name" in pos else ""

        payload = None
        raw_data = row[pos["data"]] if "data" in pos else None
        if raw_data:
            try:
                payload = json.loads(raw_data.decode("utf-8", errors="ignore")
                                     if isinstance(raw_data, (bytes, bytearray))
                                     else str(raw_data))
            except (ValueError, TypeError):
                payload = None

        genres_list = _as_list(_json_field(payload, "Genres"))
        tags_list = _as_list(_json_field(payload, "Tags"))
        studios_list = _as_list(_json_field(payload, "Studios"))
        year = _json_field(payload, "ProductionYear")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        size = _json_field(payload, "Size")
        try:
            size = int(size) if size is not None else None
        except (TypeError, ValueError):
            size = None

        if not genres_list and not tags_list:
            no_tags += 1

        items.append(build_item(
            item_id=item_id, name=name, path=path, type_full=type_full,
            year=year, genres=genres_list, tags=tags_list, studios=studios_list,
            actors=[],          # 传统架构的演员在 People 关联表里，结构随版本变化，不做猜测
            size=size,
            behavior=behavior_map.get(item_id),
            listing_cache=listing_cache,
        ))
        if progress and total:
            progress(int((i + 1) / total * 100))

    if no_tags:
        _log(f"  · 其中 {no_tags} 条在库文件里没有标签，已尝试用同目录 .nfo 兜底")
    _log(f"✅ 采集完成：{len(items)} 部（按范围排除 {skipped} 条）")
    return items


def collect_library(
    db_path: str,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_keywords: Optional[list] = None,
) -> list[Item]:
    """按**数据库架构自动分派**，构造全库 Item 列表。

    保留原有签名（第一个参数是数据库路径），所以既有调用方与测试完全不受影响；
    新增的是对传统架构（Emby / Jellyfin ≤10.8）的支持。

    需要"本地数据库 or 服务器 API 二选一"的统一入口，请用 ``utils.library_source``。
    """
    schema = detect_db_schema(db_path)
    if schema == SCHEMA_LEGACY:
        return _collect_legacy(db_path, log=log, progress=progress,
                               should_stop=should_stop,
                               exclude_keywords=exclude_keywords)
    if schema == SCHEMA_UNKNOWN and Path(db_path).exists():
        raise ValueError(
            f"无法识别的数据库架构（既没有 BaseItems 也没有 TypedBaseItems）：{db_path}\n"
            "· Jellyfin 请指向数据目录下的 jellyfin.db（10.9+）或 library.db（10.8 及更早）；\n"
            "· Emby 的库文件结构随版本变化较大，建议在设置里把「库数据来源」改成「服务器 API」。")
    return _collect_ef(db_path, log=log, progress=progress,
                       should_stop=should_stop, exclude_keywords=exclude_keywords)


def summarize(items: list[Item]) -> dict:
    """从采集结果计算概览统计（仪表盘用，纯函数可测）。"""
    total = len(items)
    by_bucket = Counter(it.bucket for it in items)
    fav = sum(1 for it in items if it.is_favorite)
    played = sum(1 for it in items if it.is_played)
    ever_play = sum(1 for it in items if it.play_count > 0)
    progress = sum(1 for it in items if it.has_progress)
    unknown = sum(1 for it in items if it.unknown_actor)
    # 未看 = 待预测池（不是删除依据；真正判删要靠评分阶段的内容相似度预测）
    unwatched = sum(1 for it in items if not it.is_played and it.play_count == 0)
    unwatched_old = sum(
        1 for it in items
        if not it.is_played and it.play_count == 0 and it.year is not None and it.year <= 2022
    )

    years = Counter(it.year for it in items if it.year is not None)
    studios = Counter(" / ".join(it.studios) for it in items if it.studios)
    actors = Counter(a for it in items for a in it.actors if a)
    fav_actors = Counter(a for it in items if it.is_favorite for a in it.actors if a)

    # 重复（同番号 >= 2，排除 trailer/other）
    by_num = defaultdict(list)
    for it in items:
        if it.num and it.bucket not in (BUCKET_TRAILER, BUCKET_OTHER):
            by_num[it.num].append(it)
    dup_groups = {k: v for k, v in by_num.items() if len(v) >= 2}
    cd_groups = sum(1 for v in dup_groups.values() if any(i.is_cd for i in v))
    true_dup_candidates = sum(1 for v in dup_groups.values() if not any(i.is_cd for i in v))

    return {
        "total": total,
        "by_bucket": dict(by_bucket),
        "favorite": fav,
        "played": played,
        "ever_play": ever_play,
        "progress": progress,
        "unknown_actor": unknown,
        "unwatched": unwatched,
        "unwatched_old": unwatched_old,
        "years": sorted(years.items()),
        "top_studios": studios.most_common(15),
        "top_actors": actors.most_common(15),
        "top_fav_actors": fav_actors.most_common(12),
        "dup_groups": len(dup_groups),
        "dup_paths": sum(len(v) for v in dup_groups.values()),
        "dup_cd_groups": cd_groups,
        "dup_true": true_dup_candidates,
    }
