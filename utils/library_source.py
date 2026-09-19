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
from typing import Callable, Optional

from utils.library import (
    SCHEMA_JELLYFIN_EF,
    SCHEMA_LEGACY,
    collect_library,
    detect_db_schema,
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
    SCHEMA_JELLYFIN_EF: "Jellyfin 10.9+ 架构",
    SCHEMA_LEGACY: "传统架构（Emby / Jellyfin ≤10.8）",
}


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
                "Jellyfin 请指向 jellyfin.db（10.9+）或 library.db（10.8 及更早）；\n"
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
        return collect_library(
            str(opts["path"]), log=log, progress=progress,
            should_stop=should_stop, exclude_keywords=exclude_keywords)
    try:
        items, _server_kind = collect_library_api(
            opts["server"], opts["key"], log=log, progress=progress,
            should_stop=should_stop, exclude_keywords=exclude_keywords,
            declared_kind=opts.get("kind", KIND_AUTO))
        return items
    except LibraryApiError as e:
        raise SourceUnavailable(str(e)) from e


__all__ = [
    "SourceUnavailable",
    "SOURCE_AUTO", "SOURCE_DB", "SOURCE_API", "SOURCE_CHOICES",
    "collect", "describe", "resolve", "server_kind", "normalize_source",
]
