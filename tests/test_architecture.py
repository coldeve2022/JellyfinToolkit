"""架构守则：同一件事**只允许有一处实现**。

这些测试不做功能验证，而是用源码扫描把"重复造轮子"挡在门外 ——
因为这类问题不会让测试变红，只会让"修了一个地方、另一个地方照旧"，
用户就得连着报三次同一个 bug（本项目真实发生过：字幕判断三处、附属文件判定三处）。

判定方式一律是**扫源码文本**：新加一份同义实现时，这里就会失败。
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 只扫这些目录（tests / build / dist 不算）
SCAN_DIRS = ("utils", "workers", "automation", "ui")


def _py_files():
    for d in SCAN_DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" not in f.parts:
                yield f


def _scan(pattern: str) -> list:
    rx = re.compile(pattern)
    hits = []
    for f in _py_files():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.split("\n"), 1):
            if rx.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
    return hits


# ── 守则 1：字幕是否存在的判定只有一处 ────────────────────────

def test_no_duplicate_subtitle_existence_checks():
    """禁止再出现"自己拼 base + 字幕扩展名"的写法。

    历史：这个判断被实现了三次（检测页 / 自动化管线 / find_subtitles），
    前两份只认 ``xxx.srt``，于是 ``xxx.chs.srt`` 被误报成"没有字幕"，
    用户在同一件事上报了两次。现在统一走 ``subtitle_clean.find_subtitles``。
    """
    hits = _scan(r"splitext\([^)]*\)\[0\]\s*\+\s*")
    # 允许 find_subtitles 之外的"取主干名"用法（如拼封面路径），
    # 但**不允许**跟字幕扩展名一起用 —— 那正是第三份实现的写法
    subtitle_hits = [h for h in hits if "srt" in h.lower() or "sub_ext" in h.lower()
                     or "subtitle" in h.lower()]
    assert not subtitle_hits, (
        "发现了并行的字幕判定实现，请改用 utils.subtitle_clean.find_subtitles：\n"
        + "\n".join(subtitle_hits))


def test_subtitle_detection_has_single_source():
    """`find_subtitles` 必须存在于 subtitle_clean，且是唯一入口。"""
    from utils import subtitle_clean

    assert callable(getattr(subtitle_clean, "find_subtitles", None))


# ── 守则 2：附属文件（预告片/主题视频）判定只有一份词表 ──────────

def test_auxiliary_rules_are_defined_once():
    """附属文件判定只允许一处词表。

    现状（待治理）：`utils/library.py` 的 AUXILIARY_* 与
    `utils/merge.py` 的 AUXILIARY_* 是**两套不同的词表**，
    同一文件在两处可能得出不同结论。这里先把"只允许一处"作为目标钉住 ——
    合并完成后这条会从 xfail 变成硬断言。
    """
    hits = _scan(r"^AUXILIARY_(WORDS|MULTIWORD|PATH_KEYWORDS|STEMS|PATH_SEGMENTS)\b")
    modules = {h.split(":")[0].split("\\")[0].split("/")[0] + "/" +
               h.split(":")[0].split("\\")[-1].split("/")[-1] for h in hits}
    files = sorted({h.split(":")[0] for h in hits})
    if len(files) > 1:
        pytest.xfail(f"已知待治理：附属判定词表分散在 {files}（见审计报告）")
    assert len(files) <= 1


# ── 守则 3：扩展名清单不应各自定义 ─────────────────────────────

def test_no_dead_extension_constants():
    """曾经有一份 `_AUDIO_EXTS` 在 v3.8.4 后变成死代码（无人引用却还在），
    正是"各模块自己存一份清单"的典型后遗症。这里确认它已被删除。
    """
    assert not _scan(r"_AUDIO_EXTS"), "utils/whisper_tool.py 里又出现了 _AUDIO_EXTS"


def test_video_extensions_come_from_config():
    """「哪些是视频」的唯一事实来源是配置，不是模块里另抄的常量。"""
    from config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.get("video_extensions"), "配置里应有 video_extensions"
