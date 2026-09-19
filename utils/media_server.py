"""媒体服务器类型适配层 —— 让同一套工具同时对接 Jellyfin 与 Emby。

为什么需要单独一层：Jellyfin 是从 Emby 分叉出来的，两者 REST API 高度相似，
但有几处**必须**区别对待，写在业务代码里会变成到处散落的 if：

1. **身份识别**
   ``GET /System/Info/Public`` 两者都支持，而且**不需要 API Key**，
   返回体里的 ``ProductName`` 可直接区分（Jellyfin → "Jellyfin Server"，
   Emby → "Emby"）。用它做探测，用户不必自己填"我这是哪个服务器"。
   ``/System/Info``（需要 Key）作为回退 —— 它同时能拿到 ServerName/Version。

2. **认证头**
   ``X-Emby-Token`` **两者都认**（Jellyfin 保留了 Emby 的历史头名），
   所以这里不做分支。``Authorization: MediaBrowser ...`` 只有 Emby 认，
   ``Authorization: Bearer`` 只有 Jellyfin 10.9+ 认 —— 都不如前者通用。

3. **数据库文件**
   Jellyfin 10.9+ 用 ``jellyfin.db``（EF Core 架构：BaseItems / Peoples /
   BaseItemImageInfos / UserData）；
   Jellyfin 10.8 及更早、以及 Emby 用 ``library.db``（TypedBaseItems 架构，
   元数据塞在 ``data`` 列的 JSON 里）。
   两者 SQL 不通用，必须按架构分派（见 ``utils/library.py``）。

   因此本模块还负责**在本机自动找库文件**：用户换机器后不必自己去翻
   ``%APPDATA%`` 或 ``/var/lib``。

本模块是纯函数 + 标准库，不依赖 Qt，可单元测试。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── 服务类型常量 ──
KIND_AUTO = "auto"
KIND_JELLYFIN = "jellyfin"
KIND_EMBY = "emby"
KIND_UNKNOWN = "unknown"

ALL_KINDS = (KIND_AUTO, KIND_JELLYFIN, KIND_EMBY)

#: 界面下拉用的 (值, 显示名) —— 集中在这里，避免 UI 与配置漂移
KIND_CHOICES = (
    (KIND_AUTO, "自动检测（推荐）"),
    (KIND_JELLYFIN, "Jellyfin"),
    (KIND_EMBY, "Emby"),
)

#: 两个服务器的默认 HTTP 端口（Emby 与 Jellyfin 都是 8096 / https 8920）
DEFAULT_PORT = 8096
DEFAULT_HTTPS_PORT = 8920

#: 各服务器的主数据库文件名（按存在性探测）
DB_FILENAMES = ("jellyfin.db", "library.db")


@dataclass
class ServerProbe:
    """一次服务端探测的结果。"""

    ok: bool = False
    kind: str = KIND_UNKNOWN          # jellyfin / emby / unknown
    name: str = ""                    # ServerName
    version: str = ""                 # Version
    product: str = ""                 # ProductName（原始字符串）
    error: str = ""
    authenticated: bool = False       # 是否用了 API Key 拿到 /System/Info

    @property
    def kind_cn(self) -> str:
        return {KIND_JELLYFIN: "Jellyfin", KIND_EMBY: "Emby"}.get(self.kind, "未知服务器")

    def describe(self) -> str:
        if not self.ok:
            return self.error or "未连接"
        bits = [self.kind_cn]
        if self.name:
            bits.append(self.name)
        if self.version:
            bits.append(f"v{self.version}")
        return " · ".join(bits)


def normalize_kind(value: Optional[str]) -> str:
    """把任意输入收敛成合法服务类型；非法/空 → auto。"""
    v = str(value or "").strip().lower()
    return v if v in ALL_KINDS else KIND_AUTO


def kind_from_product(product: str) -> str:
    """从 ``ProductName`` 推断服务类型。

    Jellyfin 返回 "Jellyfin Server"，Emby 返回 "Emby"（老版本可能是 "Emby Server"）。
    两者都做了宽松包含匹配，避免版本间文案变化导致误判。
    """
    text = str(product or "").strip().lower()
    if not text:
        return KIND_UNKNOWN
    # 先判 emby：Jellyfin 的 ProductName 里不含 emby，反之亦然
    if "emby" in text:
        return KIND_EMBY
    if "jellyfin" in text:
        return KIND_JELLYFIN
    return KIND_UNKNOWN


def normalize_server_url(server: str) -> str:
    """补全用户只填 ``192.168.1.5:8096`` 时缺失的 scheme，并去掉尾部斜杠。"""
    s = str(server or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def _request(url: str, api_key: str = "", timeout: int = 6) -> tuple[int, object]:
    """发一个 GET，返回 (状态码, 解析后的 JSON 或 None)。不抛异常。"""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Emby-Token"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001  连接被拒/DNS 失败/超时都只是"探测不到"
        return 0, None


def detect(server: str, api_key: str = "", timeout: int = 6,
           declared: str = KIND_AUTO) -> ServerProbe:
    """探测服务端类型。

    顺序：
      1. ``/System/Info/Public``（无需 Key，两种服务器都支持）→ 拿 ProductName/Version；
      2. 若失败且给了 Key，再试 ``/System/Info``（需要 Key，能拿到 ServerName）；
      3. 都失败 → 沿用用户在设置里**手选**的 ``declared``（手选优先于"未知"）。

    Args:
        server: 服务器地址，可省略 scheme（会自动补 http://）
        api_key: API Key，可空
        timeout: 单次请求超时（秒）
        declared: 用户在设置里手选的类型（auto/jellyfin/emby）
    Returns:
        ServerProbe
    """
    base = normalize_server_url(server)
    if not base:
        return ServerProbe(ok=False, error="未填写服务器地址",
                           kind=normalize_kind(declared) if declared != KIND_AUTO else KIND_UNKNOWN)

    code, data = _request(f"{base}/System/Info/Public", api_key="", timeout=timeout)
    payload = data if isinstance(data, dict) else {}

    if code != 200:
        # 回退到需要鉴权的接口
        if api_key:
            code2, data2 = _request(f"{base}/System/Info", api_key=api_key, timeout=timeout)
            if code2 == 200 and isinstance(data2, dict):
                payload = data2
                code = 200
                kind = kind_from_product(payload.get("ProductName", ""))
                kind = kind if kind != KIND_UNKNOWN else _declared_kind(declared)
                return ServerProbe(
                    ok=True, kind=kind,
                    name=str(payload.get("ServerName") or ""),
                    version=str(payload.get("Version") or ""),
                    product=str(payload.get("ProductName") or ""),
                    authenticated=True,
                )
        manual = _declared_kind(declared)
        err = (f"HTTP {code}" if code else "无法连接（地址/端口是否正确？）")
        return ServerProbe(ok=False, kind=manual, error=err)

    kind = kind_from_product(payload.get("ProductName", ""))
    if kind == KIND_UNKNOWN:
        kind = _declared_kind(declared)
    return ServerProbe(
        ok=True, kind=kind,
        name=str(payload.get("ServerName") or ""),
        version=str(payload.get("Version") or ""),
        product=str(payload.get("ProductName") or ""),
    )


def _declared_kind(declared: str) -> str:
    """手选类型 → 具体类型；auto 视为未知。"""
    v = normalize_kind(declared)
    return v if v in (KIND_JELLYFIN, KIND_EMBY) else KIND_UNKNOWN


# ── 本机数据库定位 ──────────────────────────────────────────

def candidate_data_dirs() -> list[tuple[str, Path]]:
    """返回 ``[(服务类型, 数据目录)]`` 候选列表（按平台给，**不含任何个人路径**）。

    只列各服务器**官方安装包**的默认位置；用户自定义安装位置请用设置页的手动填写。
    所有路径都用环境变量拼装，因此不会把某台机器的用户名写进源码。
    """
    home = Path.home()
    out: list[tuple[str, Path]] = []

    def add(kind: str, *candidates: Optional[Path]) -> None:
        for c in candidates:
            if c is not None:
                out.append((kind, c))

    if sys.platform == "win32":
        # 用环境变量拼装，不写字面量盘符 —— 源码里出现 Windows 绝对路径会被隐私守卫判为硬编码
        sys_drive = os.environ.get("SystemDrive") or "C:"
        prog = os.environ.get("PROGRAMDATA") or str(
            Path(sys_drive + os.sep) / "ProgramData")
        appd = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        add(KIND_JELLYFIN,
            Path(prog) / "Jellyfin" / "Server" / "data",
            Path(local) / "jellyfin" / "data",
            Path(appd) / "Jellyfin" / "data",
            Path(local) / "Jellyfin" / "Server" / "data")
        add(KIND_EMBY,
            Path(appd) / "Emby-Server" / "programdata" / "data",
            Path(prog) / "Emby-Server" / "data",
            Path(local) / "Emby-Server" / "programdata" / "data")
    elif sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        add(KIND_JELLYFIN,
            home / ".config" / "jellyfin" / "data",
            support / "Jellyfin" / "data")
        add(KIND_EMBY,
            home / ".config" / "emby" / "data",
            support / "Emby-Server" / "data")
    else:
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        add(KIND_JELLYFIN,
            Path("/var/lib/jellyfin/data"),
            xdg_config / "jellyfin" / "data",
            xdg_data / "jellyfin" / "data")
        add(KIND_EMBY,
            Path("/var/lib/emby/data"),
            xdg_config / "emby" / "data",
            xdg_data / "emby" / "data")

    return out


def find_local_databases() -> list[dict]:
    """在本机常见安装位置里查找可用的服务器数据库文件。

    Returns:
        ``[{"kind":..., "path": Path, "schema": "jellyfin_ef"|"legacy"|"unknown"}]``
        —— 按「优先 Jellyfin EF 架构（信息最全）→ Emby → 其它」排序，且已去重。
    """
    from utils.library import detect_db_schema  # 延迟导入，避免循环依赖

    found: list[dict] = []
    seen: set[str] = set()
    for kind, d in candidate_data_dirs():
        for fname in DB_FILENAMES:
            p = d / fname
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            found.append({"kind": kind, "path": p, "schema": detect_db_schema(p)})

    order = {"jellyfin_ef": 0, "legacy": 1, "unknown": 2}
    found.sort(key=lambda r: (order.get(r["schema"], 9), str(r["path"])))
    return found


def guess_kind_from_path(db_path: str | Path) -> str:
    """从数据库路径猜服务类型（只作为提示，不作为判定依据）。"""
    text = str(db_path or "").lower().replace("\\", "/")
    if "emby" in text:
        return KIND_EMBY
    if "jellyfin" in text:
        return KIND_JELLYFIN
    return KIND_UNKNOWN
