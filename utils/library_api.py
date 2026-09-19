"""通过媒体服务器 REST API 采集库数据 —— Jellyfin 与 Emby 共用一套实现。

为什么要有这条路：

1. **Emby 的库文件靠不住**。它的 ``library.db`` 结构随版本变化，而且 Emby 的常见
   部署是 NAS / Docker / 另一台机器——本机根本拿不到那个文件。相比之下 REST API
   是官方接口，只要地址和 API Key 对，部署在哪都能用。
2. **一次实现两边通吃**。Jellyfin 10.9+ 与 Emby 的 ``/Items`` 系列接口高度一致，
   差异集中在身份识别与库文件上（由 ``utils/media_server`` 处理）。
3. **行为数据更全**。收藏 / 播放次数 / 观看进度在 Emby 里分散在 ``users.db``，
   只读库文件读不到；走 API 则和 Jellyfin 一样直接拿到。

认证统一用 ``X-Emby-Token`` 请求头——两个服务器都认，因此这里不需要按类型分支。
本模块只读（GET），不写用户服务器的任何数据。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from utils.library import Item, build_item
from utils.media_server import KIND_AUTO, KIND_UNKNOWN, detect, normalize_server_url

#: 与本地数据库口径对齐：``BaseItems.MediaType='Video'`` 覆盖的条目类型
VIDEO_ITEM_TYPES = "Movie,Video,Episode,Trailer,MusicVideo"

#: 请求这些字段：Path/Tags/Genres/Studios 是分析必需，MediaSources 提供文件大小，
#: People 提供演员。ProductionYear / Name / Id / UserData 是 BaseItemDto 的标准字段。
ITEM_FIELDS = "Path,Tags,Genres,Studios,MediaSources,People"

_PAGE_SIZE = 500


class LibraryApiError(Exception):
    """API 采集过程中的可读错误（会直接展示给用户）。"""


def _req(method: str, url: str, api_key: str, timeout: int = 30):
    """发请求并解析 JSON；网络/HTTP 错误统一抛 ``LibraryApiError``。"""
    headers = {"X-Emby-Token": api_key, "Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raise LibraryApiError(f"HTTP {e.code}（{url.split('?')[0]}）") from e
    except Exception as e:  # noqa: BLE001
        raise LibraryApiError(str(e)[:140]) from e


def pick_user_id(server: str, api_key: str, log: Optional[Callable[[str], None]] = None):
    """挑一个用户 ID——优先管理员。

    行为数据（收藏/播放）是**按用户**存放的，不指定 userId 的话
    ``/Items`` 返回体里根本没有 ``UserData`` 字段，收藏数会全是 0。
    """
    base = normalize_server_url(server)
    _code, data = _req("GET", f"{base}/Users", api_key, timeout=15)
    if not isinstance(data, list) or not data:
        if log:
            log("  · 未取到用户列表，收藏/播放数据将不可用")
        return None
    for user in data:
        if isinstance(user, dict) and (user.get("Policy") or {}).get("IsAdministrator"):
            return user.get("Id")
    first = data[0]
    return first.get("Id") if isinstance(first, dict) else None


def _iter_pages(server: str, api_key: str, user_id: Optional[str],
                log: Optional[Callable[[str], None]] = None,
                should_stop: Optional[Callable[[], bool]] = None):
    """分页产出条目列表。"""
    base = normalize_server_url(server)
    path = f"/Users/{user_id}/Items" if user_id else "/Items"
    start = 0
    while True:
        if should_stop and should_stop():
            return
        params = urllib.parse.urlencode({
            "recursive": "true",
            "includeItemTypes": VIDEO_ITEM_TYPES,
            "startIndex": str(start),
            "limit": str(_PAGE_SIZE),
            "fields": ITEM_FIELDS,
            "enableImages": "false",
            "enableTotalRecordCount": "true",
        })
        _code, data = _req("GET", f"{base}{path}?{params}", api_key, timeout=45)
        if not isinstance(data, dict):
            raise LibraryApiError("服务器返回的不是预期的条目列表")
        items = data.get("Items") or []
        total = data.get("TotalRecordCount") or (start + len(items))
        yield items
        start += _PAGE_SIZE
        if log:
            log(f"  已获取 {min(start, total)}/{total}")
        if start >= total or not items:
            return


def _size_of(raw: dict) -> Optional[int]:
    """从 MediaSources 里取文件大小；多个版本取最大的那个（更接近"正片"）。"""
    sizes = []
    for src in raw.get("MediaSources") or []:
        if not isinstance(src, dict):
            continue
        val = src.get("Size")
        try:
            if val is not None:
                sizes.append(int(val))
        except (TypeError, ValueError):
            continue
    return max(sizes) if sizes else None


def _actors_of(raw: dict) -> list:
    """取演员名（People 里 Type=Actor）。"""
    out = []
    for p in raw.get("People") or []:
        if isinstance(p, dict) and str(p.get("Type") or "").lower() == "actor":
            name = str(p.get("Name") or "").strip()
            if name:
                out.append(name)
    return out


def _behavior_of(raw: dict) -> dict:
    """把 API 的 UserData 映射成 Item 的行为字段（缺失时什么都不给）。"""
    ud = raw.get("UserData")
    if not isinstance(ud, dict):
        return {}
    try:
        pos = int(ud.get("PlaybackPositionTicks") or 0)
    except (TypeError, ValueError):
        pos = 0
    return {
        "is_favorite": bool(ud.get("IsFavorite")),
        "play_count": int(ud.get("PlayCount") or 0),
        "is_played": bool(ud.get("Played")),
        "has_progress": pos > 0,
        "last_played": ud.get("LastPlayedDate"),
        "rating": ud.get("Rating"),
    }


def collect_library_api(
    server: str,
    api_key: str,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_keywords: Optional[list] = None,
    declared_kind: str = KIND_AUTO,
) -> tuple[list[Item], str]:
    """通过 REST API 采集全库条目。

    Returns:
        ``(items, kind)`` —— kind 是探测到的服务类型（jellyfin / emby / unknown），
        方便界面把"连的是哪个服务器"如实显示出来。

    Raises:
        LibraryApiError: 地址/Key 不可用，或服务器拒绝请求
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    base = normalize_server_url(server)
    if not base or not api_key:
        raise LibraryApiError("未配置服务器地址或 API Key")

    probe = detect(base, api_key, timeout=8, declared=declared_kind)
    if not probe.ok:
        raise LibraryApiError(f"连接失败：{probe.error}")
    kind = probe.kind
    _log(f"已连接：{probe.describe()}")

    user_id = pick_user_id(base, api_key, log=_log)
    if user_id:
        _log("  · 已定位用户，收藏/播放数据可用")
    else:
        _log("  · 未定位到用户，收藏/播放数据不可用（其它功能不受影响）")

    exclude = [k.lower() for k in (exclude_keywords or []) if k]
    items: list[Item] = []
    skipped = 0
    seen = 0
    listing_cache: dict = {}
    for page in _iter_pages(base, api_key, user_id, log=_log, should_stop=should_stop):
        if should_stop and should_stop():
            break
        for raw in page:
            if not isinstance(raw, dict):
                continue
            seen += 1
            path = str(raw.get("Path") or "")
            if exclude and any(k in path.lower() for k in exclude):
                skipped += 1
                continue
            name = str(raw.get("Name") or "")
            type_full = str(raw.get("Type") or "")
            year = raw.get("ProductionYear")
            try:
                year = int(year) if year is not None else None
            except (TypeError, ValueError):
                year = None

            # 归一化逻辑与数据库模式共用同一个 build_item，
            # 保证同一部作品无论怎么读进来，番号/分桶/评分都一致
            items.append(build_item(
                item_id=str(raw.get("Id") or ""),
                name=name, path=path, type_full=type_full, year=year,
                genres=raw.get("Genres"), tags=raw.get("Tags"),
                studios=raw.get("Studios"),
                actors=_actors_of(raw),
                size=_size_of(raw),
                # API 不返回封面在本机的路径，build_item 会退回视频目录 sidecar 图
                behavior=_behavior_of(raw),
                listing_cache=listing_cache,
            ))
            if progress and seen:
                progress(min(99, seen))
            if should_stop and should_stop():
                break

    _log(f"✅ API 采集完成：{len(items)} 部（按范围排除 {skipped} 条）")
    if progress:
        progress(100)
    return items, kind


def describe_source(server: str, api_key: str = "", declared_kind: str = KIND_AUTO) -> str:
    """返回一句可读的来源描述（供界面显示"当前会从哪里读库"）。"""
    base = normalize_server_url(server)
    if not base:
        return "未配置服务器地址"
    if not api_key:
        return f"{base}（缺 API Key，无法通过 API 采集）"
    probe = detect(base, api_key, timeout=5, declared=declared_kind)
    if not probe.ok:
        return f"{base}（{probe.error}）"
    return f"{probe.describe()} @ {base}"


__all__ = [
    "LibraryApiError",
    "collect_library_api",
    "describe_source",
    "pick_user_id",
    "VIDEO_ITEM_TYPES",
    "ITEM_FIELDS",
    "KIND_UNKNOWN",
]
