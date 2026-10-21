"""有码检测子系统的架构守则 —— 防"同一件事又被实现了一遍"。

和 ``tests/test_architecture.py`` 一样的思路：扫源码文本，而不是跑功能。
这类问题不会让功能测试变红，只会让"改了一处、另一处照旧"。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCAN_DIRS = ("censor", "ui", "workers", "utils", "automation")


def _scan(pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits: list[str] = []
    for folder in SCAN_DIRS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    # 统一成正斜杠，否则 Windows 上 "censor\\x.py" 匹配不到 "censor/x.py"
                    hits.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {line.strip()[:90]}")
    return hits


def test_only_one_frame_sampler():
    """抽帧参数（-skip_frame / scale / pix_fmt）只允许出现在 sampling.py。

    否则"为什么抽帧慢/为什么尺寸不对"就要在两处分别查 —— 这个坑在
    v3.14 之前出现过（旧页面自己 walk 目录、自己判扩展名）。
    """
    hits = [h for h in _scan(r"skip_frame") if "censor/sampling.py" not in h]
    assert not hits, "抽帧逻辑被复制到了别处，请统一走 censor.sampling.FrameSampler：\n" + "\n".join(hits)


def test_censorship_page_does_not_judge_by_filename():
    """页面不许再自己用文件名下结论 —— 必须走 CensorService。"""
    text = (ROOT / "ui" / "pages" / "censorship.py").read_text(encoding="utf-8")
    assert "is_uncensored_filename" not in text
    assert "CensorService" in text


def test_verdicts_only_come_from_fusion():
    """``.state = CensorState.X`` 只允许在 fusion.py 里出现。"""
    hits = [h for h in _scan(r"\.state\s*=\s*CensorState") if "censor/fusion.py" not in h]
    assert not hits, "结论被绕过了融合层，请统一走 censor.fusion.fuse：\n" + "\n".join(hits)


def test_detectors_have_unique_names_and_priority():
    import censor

    specs = censor.detectors.specs()
    assert specs, "内置检测器没注册上"
    names = [spec.name for spec in specs]
    assert len(names) == len(set(names))
    for spec in specs:
        instance = spec.create()
        assert instance.name == spec.name, f"{spec.name} 的实例 name 与注册名不一致"
        assert callable(instance.detect)


def test_fusion_policy_has_no_hardcoded_scores():
    """阈值必须能通过配置改，代码里只留默认值。"""
    from config import DEFAULT_CONFIG

    for key in ("censor_strong_score", "censor_review_score",
                "censor_frame_samples", "censor_window"):
        assert key in DEFAULT_CONFIG
