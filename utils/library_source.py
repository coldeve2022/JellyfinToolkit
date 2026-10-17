"""库数据来源的统一入口 —— 决定"从本地数据库读"还是"从服务器 API 读"。

配置项 ``library_source`` = ``auto`` / ``db`` / ``api``。

``auto`` 的判定顺序（**刻意保持 Jellyfin 老用户行为不变**）：

1. 配了 ``jellyfin_db_path``、文件存在、且架构可识别 → **本地数据库**
   （最快，封面/演员等信息也最全，不依赖服务器在线）；
2. 否则若同时配了服务器地址 + API Key → **服务器 API**
   （Emby 的推荐方式：无需在本机找到库文件，NAS/Docker 部署也能用）；
3. 都不满足 → 抛出 ``SourceUnavailable``，并把两种配法直接写在报错里。

这个模块是**所有采集调用方的唯一入口**。好处不只是"换来源改一处"：
10 个页面共用同一套解析逻辑，就不会出现"概览页读的是库文件、去重页读的是 API"
这种自相矛盾的情况。
"""

from __future__ import annotations

from pathlib import Path
import os
import re
import sqlite3
from typing import Callable, Optional

from utils.library import (
    SCHEMA_JELLYFIN_EF,
    SCHEMA_LEGACY,
    collect_library,
    detect_db_schema,
    LibraryCollectionCancelled,
)
from utils.library_api import LibraryApiError, collect_library_api
from utils.media_server import (
    KIND_AUTO,
    KIND_EMBY,
    KIND_JELLYFIN,
    find_local_databases,
    normalize_kind,
)

SOURCE_AUTO = "auto"
SOURCE_DB = "db"
SOURCE_API = "api"

ALL_SOURCES = (SOURCE_AUTO, SOURCE_DB, SOURCE_API)

#: 界面下拉用的 (值, 显示名, 说明)
SOURCE_CHOICES = (
    (SOURCE_AUTO, "自动（推荐）", "有可读的库文件就用库文件，否则走服务器 API"),
    (SOURCE_DB, "本地数据库", "直接读 jellyfin.db / library.db，不依赖服务器在线"),
    (SOURCE_API, "服务器 API", "走 Jellyfin / Emby 的 REST 接口，适合 Emby 或服务器在别的机器"),
)

_SCHEMA_CN = {
    SCHEMA_JELLYFIN_EF: "Jellyfin 10.11+ 架构",
    SCHEMA_LEGACY: "传统架构（Emby / Jellyfin ≤10.10）",
}


CollectionCancelled = LibraryCollectionCancelled


def map_media_path(path: str, mappings=None) -> str:
    """Explicit longest segment-prefix mapping; refuse traversal and substring matches."""
    source = str(path).replace("\\", "/")
    if ".." in source.split("/"):
        return path
    for mapping in sorted((m for m in (mappings or []) if isinstance(m, dict)),
                          key=lambda m: len(str(m.get("remote", ""))), reverse=True):
        remote = str(mapping.get("remote") or "").replace("\\", "/").rstrip("/")
        local = str(mapping.get("local") or "")
        if not remote or not local:
            continue
        windows = bool(re.match(r"^[A-Za-z]:", remote) or remote.startswith("//"))
        left, right = (source.casefold(), remote.casefold()) if windows else (source, remote)
        if left == right or left.startswith(right + "/"):
            tail = source[len(remote):].lstrip("/")
            return os.path.join(local, *tail.split("/")) if tail else local
    return path


def capabilities(cfg, items=None) -> dict:
    """Report reachable local media separately from metadata-only remote library access."""
    rows = list(items or [])
    local = sum(bool(it.path and os.path.isfile(it.path)) for it in rows)
    return {"source": normalize_source(_get(cfg, "library_source", "auto")),
            "items": len(rows), "local_items": local, "remote_items": len(rows) - local,
            "can_modify_local": bool(local), "has_path_mappings": bool(_get(cfg, "library_path_mappings", [])),
            "can_sync_server": bool(_get(cfg, "jellyfin_server") and _get(cfg, "jellyfin_api_key"))}


class SourceUnavailable(Exception):
    """当前配置无法确定数据来源——报错信息里会直接给出怎么配。"""


def normalize_source(value) -> str:
    """把任意输入收敛成合法来源；非法/空 → auto。"""
    v = str(value or "").strip().lower()
    return v if v in ALL_SOURCES else SOURCE_AUTO


def _get(value, name: str, default=""):
    """从配置对象 / 字典里取字段，两者都支持（测试里常传 dict）。"""
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _autodetect_db(log=None) -> Optional[dict]:
    """在本机常见安装位置里找库文件；找到就返回 ``{"path","schema"}``。"""
    try:
        found = find_local_databases()
    except Exception:  # noqa: BLE001  探测失败不该让整个采集流程挂掉
        return None
    if not found:
        return None
    best = found[0]
    if log:
        log(f"未指定数据库路径，已自动检测到：{best['path']}（{_SCHEMA_CN.get(best['schema'], '未知架构')}）")
        if len(found) > 1:
            log(f"  · 本机另有 {len(found) - 1} 个库文件，如需指定请到「设置 → 媒体库数据源」")
    return {"path": best["path"], "schema": best["schema"]}


def resolve(value, log: Optional[Callable[[str], None]] = None) -> tuple[str, dict]:
    """解析出 ``("db", {"path": Path})`` 或 ``("api", {"server","key","kind"})``。

    Args:
        value: ``ToolkitConfig`` / ``dict`` / 数据库路径字符串
        log: 可选日志回调（自动检测到路径时会说一声，避免用户不知道为什么能读到）
    Raises:
        SourceUnavailable: 配置不足以采集，或指定的来源不可用
    """
    # 兼容旧调用：直接给了数据库路径
    if isinstance(value, (str, Path)):
        p = Path(str(value))
        if not p.is_file():
            raise SourceUnavailable(f"数据库文件不存在：{p}")
        return SOURCE_DB, {"path": p}

    source = normalize_source(_get(value, "library_source", SOURCE_AUTO))
    db_raw = str(_get(value, "jellyfin_db_path", "") or "").strip()
    server = str(_get(value, "jellyfin_server", "") or "").strip()
    api_key = str(_get(value, "jellyfin_api_key", "") or "").strip()
    kind = normalize_kind(_get(value, "server_type", KIND_AUTO))

    if source == SOURCE_DB:
        if not db_raw:
            auto = _autodetect_db(log)
            if auto:
                return SOURCE_DB, auto
            raise SourceUnavailable(
                "数据来源被设为「本地数据库」，但既没填路径、本机也没自动检测到库文件。\n"
                "请到「设置 → 媒体库数据源」填写，或改成「自动」/「服务器 API」。")
        path = Path(db_raw)
        if not path.is_file():
            raise SourceUnavailable(f"数据库文件不存在：{path}")
        schema = detect_db_schema(path)
        if schema not in _SCHEMA_CN:
            raise SourceUnavailable(
                f"无法识别数据库架构：{path}\n"
                "Jellyfin 请指向 jellyfin.db（10.11+）或 library.db（10.10 及更早）；\n"
                "Emby 建议改用「服务器 API」。")
        return SOURCE_DB, {"path": path, "schema": schema}

    if source == SOURCE_API:
        if not server or not api_key:
            raise SourceUnavailable(
                "数据来源被设为「服务器 API」，但服务器地址或 API Key 没填全。\n"
                "请到「设置 → 服务器同步」补全。")
        return SOURCE_API, {"server": server, "key": api_key, "kind": kind}

    # ── auto ──
    if db_raw:
        path = Path(db_raw)
        schema = detect_db_schema(path)
        if path.is_file() and schema in _SCHEMA_CN:
            return SOURCE_DB, {"path": path, "schema": schema}
    else:
        auto = _autodetect_db(log)
        if auto:
            return SOURCE_DB, auto
    if server and api_key:
        return SOURCE_API, {"server": server, "key": api_key, "kind": kind}

    raise SourceUnavailable(
        "还没有配置媒体库数据源，无法读取库内容。二选一即可：\n"
        "· 方式 A（本地数据库）：在「设置 → 媒体库数据源」填写 jellyfin.db / library.db 的路径，"
        "或直接点「自动检测」；\n"
        "· 方式 B（服务器 API）：在「设置 → 服务器同步」填写服务器地址与 API Key。\n"
        "Emby 推荐方式 B。")


def describe(value) -> str:
    """给界面用的一句话说明：当前实际会从哪里取库，取不到时说明原因。

    **刻意不做任何网络请求** —— 它会在界面构造时被调用，探测服务器会卡住 UI；
    要验证连接是否可用，请用设置页的「测试连接」按钮。
    """
    try:
        kind, opts = resolve(value)
    except SourceUnavailable as e:
        return f"未就绪：{str(e).splitlines()[0]}"
    if kind == SOURCE_DB:
        schema = opts.get("schema") or detect_db_schema(opts["path"])
        label = _SCHEMA_CN.get(schema, "未知架构")
        return f"本地数据库 · {opts['path'].name}（{label}）"
    declared = normalize_kind(_get(value, "server_type", KIND_AUTO))
    label = {KIND_JELLYFIN: "Jellyfin", KIND_EMBY: "Emby"}.get(declared, "自动识别")
    return f"服务器 API · {opts['server']}（{label}，连接状态请点「测试连接」）"


def server_kind(value) -> str:
    """当前配置指向的服务类型（用于界面显示 Jellyfin / Emby）。不做网络请求。"""
    declared = normalize_kind(_get(value, "server_type", KIND_AUTO))
    if declared in (KIND_JELLYFIN, KIND_EMBY):
        return declared
    try:
        kind, opts = resolve(value)
    except SourceUnavailable:
        return KIND_AUTO
    if kind == SOURCE_API:
        return normalize_kind(opts.get("kind") or KIND_AUTO)
    from utils.media_server import guess_kind_from_path
    return guess_kind_from_path(opts.get("path", ""))


def discover_scopes(value, log=None, should_stop=None):
    """Read selectable library roots. Run on a worker; no catalog cache is trusted."""
    from utils.library import _check_cancel
    from utils.library_scope import discover_db_scopes, scope_id
    from utils.library_api import list_virtual_folders
    from utils.media_server import server_identity
    _check_cancel(should_stop)
    kind, opts = resolve(value, log=log)
    try:
        if kind == SOURCE_DB:
            return discover_db_scopes(opts['path'], _get(value, 'jellyfin_data_dir', ''), should_stop)
        rows = list_virtual_folders(opts['server'], opts['key'], should_stop=should_stop)
        source = 'server:' + server_identity(opts['server'])
        return [{'id': scope_id(source, row['id']), 'name': row['name'], 'paths': row['paths']} for row in rows]
    except (LibraryApiError, OSError, ValueError, sqlite3.Error) as exc:
        raise SourceUnavailable('无法读取媒体库范围，请检查数据源并重新选择：' + str(exc)) from exc


def collect(
    value,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    exclude_keywords: Optional[list] = None,
) -> list:
    """按配置采集全库，返回 ``list[Item]``。

    ``value`` 可以是 ``ToolkitConfig``、``dict``，或（兼容旧代码的）数据库路径字符串。
    """
    kind, opts = resolve(value, log=log)
    if kind == SOURCE_DB:
        items = collect_library(
            str(opts["path"]), log=log, progress=progress,
            should_stop=should_stop, exclude_keywords=exclude_keywords,
            user_id=str(_get(value, "jellyfin_user_id") or ""))
    else:
        try:
            items, _server_kind = collect_library_api(
            opts["server"], opts["key"], log=log, progress=progress,
            should_stop=should_stop, exclude_keywords=exclude_keywords,
            declared_kind=opts.get("kind", KIND_AUTO),
            user_id=str(_get(value, "jellyfin_user_id") or ""))
        except LibraryApiError as e:
            raise SourceUnavailable(str(e)) from e
    if should_stop and should_stop():
        raise CollectionCancelled("采集已取消；部分结果不会用于全库统计或删除建议")
    from utils.library_scope import filter_scopes, normalize_scope_path
    selected = _get(value, 'library_scope_ids', []) or []
    roots = _get(value, 'library_scope_paths', []) or []
    if (not isinstance(selected, list) or not isinstance(roots, list)
            or any(not isinstance(sid, str) or not sid.strip() for sid in selected)):
        raise SourceUnavailable('媒体库范围配置无效，请重新选择')
    if any(not isinstance(root, str) or not normalize_scope_path(root) for root in roots):
        raise SourceUnavailable('媒体库目录必须为绝对服务器路径且不能包含 ..，请重新选择')
    catalog = discover_scopes(value, log=log, should_stop=should_stop) if selected or (_get(value, 'library_scope_confirmed', False) and not roots) else []
    missing = set(selected) - {scope['id'] for scope in catalog}
    if missing:
        raise SourceUnavailable('已选择的媒体库不存在或当前数据源不可访问，请重新选择媒体库范围')
    items = filter_scopes(items, catalog, selected, roots, should_stop=should_stop)
    mappings = _get(value, "library_path_mappings", [])
    listing_cache = {}
    for it in items:
        original = it.path
        mapped = map_media_path(original, mappings)
        if mapped != original:
            it.source_path = original
            it.path = mapped
            if not it.cover or not os.path.isfile(it.cover):
                from utils.library import find_cover
                it.cover = find_cover(mapped, it.num, it.cd_index, cache=listing_cache)
    from utils.media_filter import filter_media
    return filter_media(items, log=log, should_stop=should_stop, listing_cache=listing_cache)


__all__ = [
    "SourceUnavailable",
    "SOURCE_AUTO", "SOURCE_DB", "SOURCE_API", "SOURCE_CHOICES",
    "collect", "discover_scopes", "describe", "resolve", "server_kind", "normalize_source",
]
