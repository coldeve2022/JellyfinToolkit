"""无码版本检索的架构守则 —— 防"各处自己发请求""各处自己下结论"。

和 `test_architecture.py` 一样：扫源码文本，不跑功能。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCAN_DIRS = ("censor", "ui", "utils", "workers", "automation")


def _scan(pattern: str, dirs=SCAN_DIRS) -> list[str]:
    rx = re.compile(pattern)
    hits: list[str] = []
    for folder in dirs:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {line.strip()[:90]}")
    return hits


def test_only_transport_talks_http():
    """``urllib.request`` 只允许出现在传输层 —— 否则限速/代理/离线开关都会被绕过。"""
    # 只约束检码/检索域与界面层；``utils/*_api.py`` 是 Jellyfin 客户端，本来就该自己发请求
    hits = [h for h in _scan(r"^\s*(import urllib\.request|from urllib import request|import requests)",
                             dirs=("censor", "ui"))
            if "censor/releases/transport.py" not in h]
    assert not hits, "请统一走 censor.releases.transport：\n" + "\n".join(hits)


def test_availability_only_decided_in_service():
    """``Availability.CONFIRMED/LIKELY`` 的判定只允许在 service 里发生。"""
    hits = [h for h in _scan(r"Availability\.(CONFIRMED|LIKELY)\b")
            if "censor/releases/service.py" not in h
            and "censor/releases/models.py" not in h
            and not h.startswith("tests/")]
    assert not hits, "结论判定被绕过了 decide_availability：\n" + "\n".join(hits)


def test_offline_switch_is_enforced_in_one_place():
    text = (ROOT / "censor" / "releases" / "transport.py").read_text(encoding="utf-8")
    assert "JELLYFIN_TOOLKIT_OFFLINE" in text
    assert "def offline(" in text


def test_builtin_sources_registered_and_configured():
    import censor  # noqa: F401 - 导入即注册
    from censor.registry import sources
    from config import DEFAULT_CONFIG

    names = set(sources.names())
    assert {"javdb", "javbus", "supjav"} <= names
    for name in DEFAULT_CONFIG["release_sources"]:
        assert name in names, f"默认配置里的检索源 {name} 没有注册"


def test_sources_declare_version_and_capabilities():
    from censor.registry import sources

    for spec in sources.specs():
        instance = spec.create(None, None)
        assert instance.name == spec.name
        assert instance.version, f"{spec.name} 必须声明 version（缓存靠它失效）"
        assert callable(instance.search)


def test_release_config_keys_have_defaults():
    from config import DEFAULT_CONFIG, ToolkitConfig

    for key in ("release_enabled", "release_sources", "release_proxy",
                "release_cf_bypass_url", "release_cache_days", "release_auto_lookup"):
        assert key in DEFAULT_CONFIG
        assert hasattr(ToolkitConfig(), key)
