"""Release 正文生成的单元测试。

重点在**抽取 CHANGELOG 里本版本那一节**：这是 Release 页面唯一的内容来源，
抽错就会把上个版本或下个版本的内容贴到当前版本上 —— 比"只有链接"更糟。

纯文本处理，不依赖网络与 gh。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    """按文件路径加载 scripts/release_notes.py。

    不往 scripts/ 里放 __init__.py —— 那是**纯脚本目录**（build_release.py 等
    都是直接执行的），把它变成包会改变它的性质，也可能影响打包。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "release_notes_probe", ROOT / "scripts" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load()

SAMPLE = """# 变更日志

## [3.8.0] — 2026-09-19

两个新功能页。

### 新增

- 批量生成字幕
- 马赛克破解

## [3.7.0] — 2026-09-19

首个公开版本。

### 新增

- Emby 支持
"""


def test_extract_gets_the_right_version():
    sec = release_notes.extract_section(SAMPLE, "3.8.0")
    assert "批量生成字幕" in sec and "马赛克破解" in sec
    # 绝不能把别的版本带进来
    assert "Emby 支持" not in sec
    assert "3.7.0" not in sec


def test_extract_strips_version_heading_and_date():
    sec = release_notes.extract_section(SAMPLE, "3.8.0")
    assert not sec.startswith("##")
    assert "2026-09-19" not in sec


def test_extract_accepts_v_prefixed_version():
    """标签是 v3.8.0，version.py 是 3.8.0 —— 两种传法都得认。"""
    assert release_notes.extract_section(SAMPLE, "v3.8.0") == \
        release_notes.extract_section(SAMPLE, "3.8.0")


def test_extract_last_section_has_no_next_heading():
    sec = release_notes.extract_section(SAMPLE, "3.7.0")
    assert "Emby 支持" in sec


def test_extract_missing_version_returns_empty():
    assert release_notes.extract_section(SAMPLE, "9.9.9") == ""
    assert release_notes.extract_section("", "3.8.0") == ""


def test_body_embeds_update_and_install_steps():
    body = release_notes.build_body("3.8.0", SAMPLE,
                                    "https://example.invalid/repo")
    # 用户第一眼要能看到"这版改了什么"，而不是只有一个链接
    assert "本次更新（v3.8.0）" in body
    assert "批量生成字幕" in body
    # 安装步骤与数据位置仍然要有
    assert "## 安装" in body and "JellyfinToolkit.exe" in body
    assert "## 系统要求" in body and "## 数据位置" in body
    assert "https://example.invalid/repo/blob/main/CHANGELOG.md" in body


def test_body_falls_back_when_version_missing():
    """抽不到内容时也要能出一份可用的正文（不能抛异常让发布挂掉）。"""
    body = release_notes.build_body("9.9.9", SAMPLE, "https://example.invalid/repo")
    assert "未在 CHANGELOG.md 找到对应小节" in body
    assert "## 安装" in body


def test_head_regex_variants():
    for text in ("## [1.2.3] — 2026-01-01", "## [1.2.3]", "## 1.2.3"):
        assert release_notes.extract_section(text + "\n\n内容\n", "1.2.3").strip() == "内容"


def test_real_changelog_has_current_version():
    """仓库自己的 CHANGELOG 必须能抽到当前版本 —— 否则发版时正文会是空的。"""
    from version import __version__

    root = Path(__file__).resolve().parent.parent
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    sec = release_notes.extract_section(text, __version__)
    assert sec.strip(), f"CHANGELOG.md 里找不到 {__version__} 这一节"
    assert "新增" in sec or "修复" in sec
