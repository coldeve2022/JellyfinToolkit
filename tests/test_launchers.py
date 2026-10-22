"""启动脚本的守则 —— 这次 `0xc0000142` 的真正原因是 `.bat` 用了 LF 换行。

cmd.exe 读 LF-only 的批处理时会把多行合并成一条命令，于是满屏
"不是内部或外部命令"，中文也跟着乱码 —— 看起来像程序坏了，其实只是换行符。
这类问题不该靠肉眼发现，所以在测试里锁死：

1. `.bat` 必须是 **CRLF + 纯 ASCII**（ASCII 避免任何代码页歧义）；
2. 启动前必须验证 ``import PySide6``，否则会静默崩掉；
3. `main.pyw` 必须把启动期异常写日志并弹窗（.pyw 没有控制台）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BATCH_FILES = ("start.bat", "启动.bat")


@pytest.mark.parametrize("name", BATCH_FILES)
def test_batch_uses_crlf_line_endings(name):
    raw = (ROOT / name).read_bytes()
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    assert crlf > 0, f"{name} 没有 CRLF 换行"
    assert bare_lf == 0, (
        f"{name} 里有 {bare_lf} 处裸 LF —— cmd.exe 会把多行合并，"
        "表现为满屏『不是内部或外部命令』。请改用 CRLF 换行。")


@pytest.mark.parametrize("name", BATCH_FILES)
def test_batch_is_ascii_only(name):
    raw = (ROOT / name).read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"{name} 含非 ASCII 字符（{exc}）—— 批处理里不要写中文，"
            "否则不同代码页下会乱码并可能被误解析") from exc


@pytest.mark.parametrize("name", BATCH_FILES)
def test_batch_verifies_pyside6_before_starting(name):
    text = (ROOT / name).read_text(encoding="ascii")
    assert "PySide6" in text, f"{name} 启动前没有验证 PySide6"
    assert "import sys, PySide6" in text or "import PySide6" in text
    assert "main.py" in text


@pytest.mark.parametrize("name", BATCH_FILES)
def test_batch_does_not_trust_where_python(name):
    """解释器必须按固定顺序找，不能一上来就 where python。

    PATH 第一位可能是某个工具自带的隔离运行时（没有 PySide6）。
    """
    text = (ROOT / name).read_text(encoding="ascii")
    first_where = text.find("where python")
    if first_where < 0:
        return
    venv = text.find(".venv")
    assert venv >= 0 and venv < first_where, (
        f"{name} 里 .venv 的优先级必须高于 where python")


def test_main_pyw_reports_startup_errors():
    """.pyw 没有控制台，必须自己把错误写日志 + 弹窗。"""
    text = (ROOT / "main.pyw").read_text(encoding="utf-8")
    assert "startup-error.log" in text
    assert "MessageBoxW" in text
    assert "sys.executable" in text, "弹窗里要写出当前解释器路径，这是最常见的根因"
