"""版本管理工具的单元测试。

重点在**一致性校验**：它是唯一能提前拦住"版本号写在四个地方、有一处没跟上"
这种问题的东西。而这类问题的代价很实在 ——
release workflow 会因为「标签与 version.py 不一致」直接红，
CHANGELOG 缺当前版本则会让 Release 正文变成空的。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    """按文件路径加载 scripts/version_manage.py。

    scripts/ 是**纯脚本目录**（都以 ``python scripts/xxx.py`` 方式运行），
    不往里面放 __init__.py，所以这里按路径加载。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "version_manage_probe", ROOT / "scripts" / "version_manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vm = _load()


# ── 读取 ────────────────────────────────────────────────────

def test_read_version_py():
    assert vm.read_version_py('__version__ = "3.8.1"') == "3.8.1"
    assert vm.read_version_py("__version__ = '1.0'") == "1.0"
    assert vm.read_version_py("没有版本号") == ""


def test_read_pyproject_version():
    text = '[project]\nname = "x"\nversion = "3.8.1"\n'
    assert vm.read_pyproject_version(text) == "3.8.1"
    assert vm.read_pyproject_version("") == ""


def test_read_changelog_versions_keeps_order():
    text = "# 变更日志\n\n## [3.8.1] — 2026-09-19\n\n## [3.8.0] — 2026-09-19\n"
    assert vm.read_changelog_versions(text) == ["3.8.1", "3.8.0"]


@pytest.mark.parametrize("text,expect", [
    ("3.8.1", (3, 8, 1)), ("v3.8", (3, 8)), ("", ()), ("abc", ()),
])
def test_parse_version(text, expect):
    assert vm.parse_version(text) == expect


# ── 一致性校验 ──────────────────────────────────────────────

GOOD_CL = "## [3.8.1] — 2026-09-19\n\n### 改进\n\n- x\n"


def test_consistency_passes_when_all_match():
    errors, warnings = vm.check_consistency("3.8.1", "3.8.1", GOOD_CL)
    assert errors == [] and warnings == []


def test_consistency_catches_version_mismatch():
    """两处版本号不一致 → release workflow 的一致性检查会红。"""
    errors, _ = vm.check_consistency("3.8.1", "3.8.0", GOOD_CL)
    assert any("不一致" in e for e in errors)


def test_consistency_catches_missing_changelog_section():
    """CHANGELOG 缺当前版本 → Release 正文会是空的（我们真踩过）。"""
    errors, _ = vm.check_consistency("3.8.2", "3.8.2", GOOD_CL)
    assert any("CHANGELOG" in e for e in errors)


def test_consistency_catches_unreadable_values():
    """读不到版本号时，能报的错都要报出来。

    注意这里只会有 2 条：CHANGELOG 那一项需要先知道版本号才能查
    （不能拿一个空版本号去说"CHANGELOG 里没有它"）。
    """
    errors, _ = vm.check_consistency("", "", "")
    assert len(errors) == 2
    assert any("version.py" in e for e in errors)
    assert any("pyproject" in e for e in errors)


def test_consistency_warns_when_changelog_is_ahead():
    """CHANGELOG 已经写了下一个版本，但 version.py 还没跟 —— 只警告，不算错。

    （发版前这属于"正在准备中"的正常状态。）
    """
    cl = ("## [3.9.0] — 2026-09-27\n\n- 还没发\n\n"
          "## [3.8.1] — 2026-09-19\n\n- 已发\n")
    errors, warnings = vm.check_consistency("3.8.1", "3.8.1", cl)
    assert errors == [], f"不该报错，只该警告：{errors}"
    assert any("还新" in w for w in warnings)


def test_consistency_checks_git_tag():
    errors, _ = vm.check_consistency("3.8.1", "3.8.1", GOOD_CL, git_tag="v3.8.0")
    assert any("标签" in e for e in errors)
    errors, _ = vm.check_consistency("3.8.1", "3.8.1", GOOD_CL, git_tag="v3.8.1")
    assert errors == []


# ── 提升版本号 ──────────────────────────────────────────────

def test_bump_only_touches_the_version_line():
    text = '"""说明。"""\n__version__ = "3.8.1"\nAPP_ID = "jellyfin-toolkit"\n'
    out = vm.bump_version_py(text, "3.8.2")
    assert '__version__ = "3.8.2"' in out
    assert 'APP_ID = "jellyfin-toolkit"' in out
    assert '"3.8.1"' not in out


def test_bump_pyproject():
    text = '[project]\nname = "x"\nversion = "3.8.1"\ndescription = "y"\n'
    out = vm.bump_pyproject(text, "3.8.2")
    assert 'version = "3.8.2"' in out and 'description = "y"' in out


def test_insert_changelog_entry_goes_to_top():
    text = "# 变更日志\n\n说明文字。\n\n## [3.8.1] — 2026-09-19\n\n- 旧内容\n"
    out = vm.insert_changelog_entry(text, "3.8.2", "2026-09-20", "一句话说明")
    # 新版本必须排在旧版本前面，且原有内容与开头说明都不丢
    assert out.index("## [3.8.2]") < out.index("## [3.8.1]")
    assert "说明文字。" in out and "- 旧内容" in out
    assert "一句话说明" in out


def test_insert_changelog_entry_on_empty_file():
    out = vm.insert_changelog_entry("", "1.0.0", "2026-09-20")
    assert "## [1.0.0]" in out


def test_inserted_entry_is_parseable_by_the_checker():
    """提升完版本号之后，校验必须立刻能通过（否则流程自己就断了）。"""
    text = "# 变更日志\n\n## [3.8.1] — 2026-09-19\n\n- x\n"
    out = vm.insert_changelog_entry(text, "3.8.2", "2026-09-20", "一句话")
    errors, _ = vm.check_consistency("3.8.2", "3.8.2", out)
    assert errors == []


# ── 对仓库自身的守则 ────────────────────────────────────────

def test_repo_itself_is_consistent():
    """仓库当前状态必须自洽 —— 否则说明有人改了版本号没同步别处。"""
    st = vm.gather_status(ROOT)
    assert st["version"], "读不到 version.py 的版本号"
    assert not st["errors"], f"仓库版本状态不一致：{st['errors']}"


def test_status_reports_git_facts():
    st = vm.gather_status(ROOT)
    assert st["head"], "应该在 git 仓库里运行"
    assert st["branch"]
