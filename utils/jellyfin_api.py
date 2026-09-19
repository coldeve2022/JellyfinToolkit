"""媒体服务器 REST API 客户端 - 纯函数层，标准库 urllib，零第三方依赖。

**同时支持 Jellyfin 与 Emby**：两者 API 高度同源，认证头统一用
``X-Emby-Token``（Jellyfin 保留了 Emby 的历史头名），因此这里的调用逻辑
不需要按服务器类型分支；类型识别与库文件差异交给 ``utils.media_server``。

用途：通过官方 API 与服务器联动——
- ping 测试连接、识别是 Jellyfin 还是 Emby、取服务器名
- clean_prefixed_tags：批量摘除 Tags/Genres 里的脏前缀标签（发行:/片商:/系列: 等），
  并从侧栏筛选里消失（解决"发行:xxx"这种刮削污染）
- refresh_library：触发全库刷新（POST /Library/Refresh），重建筛选/侧栏索引

注意：GET 条目需带 userId（``/Users/{uid}/Items/{id}``）；
``POST /Items/{id}`` 只改 Tags/Genres，提交前剔除复杂只读字段
（Jellyfin 会反序列化 500，Emby 同样不接受只读字段回填）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

# 提交时剔除的复杂/只读字段（避免 Jellyfin 反序列化 500）
DROP_FIELDS = ("MediaSources", "MediaStreams", "GenreItems", "ImageBlurHashes",
               "Trickplay", "UserData", "ImageTags", "BackdropImageTags",
               "ScreenshotImageTags", "ChapterImages")

# 刮削污染的脏前缀（写进了 genres/tags）
DIRTY_PREFIXES = ("发行:", "片商:", "系列:", "レーベル:", "メーカー:", "発行:")


class JellyfinError(Exception):
    pass


def _req(method: str, url: str, api_key: str, body=None,
         timeout: int = 20) -> tuple[int, dict | list | None]:
    headers = {"X-Emby-Token": api_key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        raise JellyfinError(str(e)[:120]) from e


def _base(server: str) -> str:
    return server.rstrip("/")


def _get_user_id(server: str, api_key: str) -> Optional[str]:
    code, data = _req("GET", f"{_base(server)}/Users", api_key)
    if code != 200 or not isinstance(data, list) or not data:
        return None
    for user in data:
        if isinstance(user, dict) and (user.get("Policy") or {}).get("IsAdministrator"):
            return user.get("Id")
    return data[0].get("Id") if isinstance(data[0], dict) else None


def server_probe(server: str, api_key: str = "", timeout: int = 8):
    """探测并识别服务端类型（Jellyfin / Emby）。

    Returns:
        ``utils.media_server.ServerProbe``（.ok / .kind / .name / .version / .error）
    """
    from utils.media_server import detect, normalize_kind

    declared = "auto"
    try:
        from config import ToolkitConfig
        declared = normalize_kind(getattr(ToolkitConfig.load(), "server_type", "auto"))
    except Exception:  # noqa: BLE001  读不到配置不影响探测本身
        pass
    return detect(server, api_key, timeout=timeout, declared=declared)


def ping(server: str, api_key: str) -> tuple[bool, str]:
    """测试连接：返回 (是否成功, 「类型 · 服务器名 · 版本」或错误原因)。

    会顺带识别服务端是 Jellyfin 还是 Emby，因此同一套配置两边都能验证。
    """
    if not server or not api_key:
        return False, "未配置服务器地址或 API Key"
    probe = server_probe(server, api_key)
    if probe.ok:
        return True, probe.describe()
    return False, probe.error


def _iter_items(server: str, api_key: str, log: Optional[Callable[[str], None]] = None):
    """分页遍历全部 Movie/Video 条目，产出 item dict（带 Path/Tags/Genres）。"""
    start = 0
    step = 500
    while True:
        params = urllib.parse.urlencode({
            "recursive": "true", "includeItemTypes": "Movie,Video",
            "startIndex": str(start), "limit": str(step),
            "fields": "Path,Tags,Genres",
        })
        code, data = _req("GET", f"{_base(server)}/Items?{params}", api_key, timeout=30)
        if code != 200 or not isinstance(data, dict):
            raise JellyfinError(f"读取条目失败 HTTP {code}")
        items = data.get("Items", []) or []
        total = data.get("TotalRecordCount", start + len(items))
        yield items
        if log:
            log(f"扫描条目 {min(start + step, total)}/{total}")
        start += step
        if start >= total or not items:
            break


def get_clean_tags_genres(item: dict) -> tuple[list[str], list[str], list[str]]:
    """从条目提取 Tags/Genres，摘除脏前缀 token。

    Returns: (清洗后的 tags, 清洗后的 genres, 被摘除的 token 列表)。两者都干净时第二个列表为空。
    """

    def _clean(vals) -> tuple[list[str], list[str]]:
        keep, removed = [], []
        for v in vals or []:
            s = str(v).strip()
            if not s:
                continue
            if any(s.startswith(p) for p in DIRTY_PREFIXES):
                removed.append(s)
            else:
                keep.append(s)
        return keep, removed

    tags, tags_rm = _clean(item.get("Tags"))
    genres, genres_rm = _clean(item.get("Genres"))
    return tags, genres, tags_rm + genres_rm


def update_tags_genres(server: str, api_key: str, item_id: str,
                       tags: list[str], genres: list[str]) -> bool:
    """更新条目 Tags/Genres（POST /Items/{id}，剔除复杂只读字段）。"""
    uid = _get_user_id(server, api_key)
    if not uid:
        return False
    code, item = _req("GET", f"{_base(server)}/Users/{uid}/Items/{item_id}",
                      api_key, timeout=20)
    if code != 200 or not isinstance(item, dict):
        return False
    name = item.get("Name")
    if not name:
        path = item.get("Path") or ""
        name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] or "Item"
    item["Name"] = name
    item["Tags"] = list(tags)
    item["Genres"] = list(genres)
    for k in DROP_FIELDS:
        item.pop(k, None)
    code2, _ = _req("POST", f"{_base(server)}/Items/{item_id}", api_key,
                    body=item, timeout=25)
    return code2 in (200, 204)


def scan_dirty(server: str, api_key: str,
               log: Optional[Callable[[str], None]] = None) -> list[dict]:
    """只读扫描全库：找出含脏前缀标签的条目，计算清洗方案。不修改任何数据。

    Returns: [{item_id, name, path, before_tags, before_genres,
               after_tags, after_genres, removed}]
    """
    dirty: list[dict] = []
    for items in _iter_items(server, api_key, log):
        for it in items:
            tags, genres, removed = get_clean_tags_genres(it)
            if not removed:
                continue
            dirty.append({
                "item_id": it.get("Id"),
                "name": it.get("Name") or "",
                "path": it.get("Path") or "",
                "before_tags": list(it.get("Tags") or []),
                "before_genres": list(it.get("Genres") or []),
                "after_tags": tags,
                "after_genres": genres,
                "removed": removed,
            })
    return dirty


def apply_clean(server: str, api_key: str, dirty: list[dict],
                log: Optional[Callable[[str], None]] = None) -> int:
    """应用清洗方案（逐条 POST 更新 Tags/Genres）。返回成功条数。"""
    ok = 0
    for it in dirty:
        if update_tags_genres(server, api_key, it["item_id"],
                              it["after_tags"], it["after_genres"]):
            ok += 1
            if log:
                log(f"  已清洗 {it['name'][:30] or it['path'][:30]}  <- 移除 "
                    f"{'、'.join(it['removed'][:3])}")
        elif log:
            log(f"  ⚠️ 更新失败: {(it['name'] or it['path'])[:30]}")
    return ok


def save_dirty_backup(dirty: list[dict], backup_path: str) -> int:
    """把将要清洗的原始数据落盘为备份 JSON（供回滚）。返回条数。"""
    payload = {
        "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "count": len(dirty),
        "items": dirty,
    }
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(dirty)


def load_dirty_backup(backup_path: str) -> list[dict]:
    """读取清洗备份 JSON，返回条目列表。"""
    with open(backup_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items", [])


def restore_backup(server: str, api_key: str, dirty: list[dict],
                   log: Optional[Callable[[str], None]] = None) -> int:
    """把备份里的 before_tags/before_genres 恢复回 Jellyfin。返回成功条数。"""
    ok = 0
    for it in dirty:
        if update_tags_genres(server, api_key, it["item_id"],
                              list(it.get("before_tags") or []),
                              list(it.get("before_genres") or [])):
            ok += 1
            if log:
                log(f"  已恢复 {it['name'][:30] or it['path'][:30]}")
    return ok


def refresh_library(server: str, api_key: str) -> tuple[bool, str]:
    """触发全库元数据刷新（清残留、重建侧栏筛选索引）。

    Jellyfin 与 Emby 的接口路径与语义一致（``POST /Library/Refresh``），
    因此不需要按类型分支。
    """
    try:
        code, _ = _req("POST", f"{_base(server)}/Library/Refresh", api_key, timeout=20)
        if code in (200, 202, 204):
            return True, "已触发全库刷新，服务器重新扫描后标签会自动更新"
        return False, f"HTTP {code}"
    except JellyfinError as e:
        return False, str(e)
