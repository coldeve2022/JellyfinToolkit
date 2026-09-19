"""CPU 亲和性与显存阈值的单元测试。

这两项都是"针对某台机器调出来的优化"，所以测试重点在于：
**判定不出来时要优雅退回、并且理由要能说清楚** —— 因为用户需要据此决定
要不要改成自定义值。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import cpu_affinity as ca  # noqa: E402
from utils import gpu  # noqa: E402


# ── 绑核模式 ────────────────────────────────────────────────

def test_off_mode_pins_nothing():
    info = ca.detect_affinity("off")
    assert info["mode"] == "off"
    assert info["cores"] == []
    assert "不绑" in ca.describe_affinity(info) or "没有绑定" in info["reason"]


def test_custom_mode_uses_given_cores():
    info = ca.detect_affinity("custom", "0-1")
    assert info["mode"] == "custom"
    assert info["cores"] == [0, 1]
    assert info["source"] == "自定义"


def test_custom_mode_falls_back_when_spec_is_garbage():
    """自定义写错了不能把任务卡住 —— 退回自动模式，并在说明里体现。"""
    info = ca.detect_affinity("custom", "这不是核心号")
    assert info["mode"] == "auto"
    assert info["cores"], "退回后必须给出可用的核心列表"


def test_unknown_mode_defaults_to_auto():
    assert ca.detect_affinity("乱填")["mode"] in ("auto", "off")


def test_auto_mode_always_returns_something():
    """自动模式必须永远给出一组可用的核心（否则绑核会失败）。"""
    info = ca.detect_affinity("auto")
    assert info["cores"], "至少要退回全部核心"
    assert info["source"], "必须说明判定来源，用户才知道能不能信"


def test_auto_source_reflects_how_it_was_decided():
    """来源字段要真实反映走的哪条路（界面会显示它）。"""
    info = ca.detect_affinity("auto")
    assert info["source"] in (ca._SRC_API, ca._SRC_HEURISTIC, ca._SRC_ALL)
    assert info["reason"]


def test_detect_p_core_threads_is_a_list_of_ints():
    cores = ca.detect_p_core_threads()
    assert isinstance(cores, list) and cores
    assert all(isinstance(c, int) and c >= 0 for c in cores)


def test_windows_api_detection_returns_none_when_unavailable(monkeypatch):
    """API 路径失败时必须返回 None（交给上层回退），而不是抛异常。"""
    monkeypatch.setattr(ca.sys, "platform", "linux")
    assert ca._detect_via_windows_api() is None


# ── 核心写法解析 ────────────────────────────────────────────

@pytest.mark.parametrize("spec,expect", [
    ("0-3", [0, 1, 2, 3]),
    ("0,2,4", [0, 2, 4]),
    ("0-1,4-5", [0, 1, 4, 5]),
    ("5-3", [3, 4, 5]),            # 反向也要接受
    ("1,1,2", [1, 2]),             # 去重
    ("", []),
    ("abc", []),
    ("-1", []),
])
def test_parse_core_spec(spec, expect):
    assert ca.parse_core_spec(spec) == expect


def test_parse_core_spec_clamps_to_cpu_count():
    got = ca.parse_core_spec("0-99999")
    assert got == list(range(len(ca._all_logical_threads())))


def test_format_roundtrips_and_compresses():
    assert ca.format_core_spec([0, 1, 2, 3, 8]) == "0-3,8"
    assert ca.format_core_spec([]) == ""
    cores = [0, 1, 2, 5]
    assert ca.parse_core_spec(ca.format_core_spec(cores)) == cores


# ── 显存阈值推算 ────────────────────────────────────────────

def test_recommend_scales_with_card_size():
    """核心诉求：**不能写死某个卡的值**，要按容量换算。"""
    h12, l12 = gpu.recommend_vram_thresholds(12)
    h24, l24 = gpu.recommend_vram_thresholds(24)
    assert h24 > h12 and l24 > l12
    # 12 GB 卡上的推算应接近原实现实测的 10.5 / 8.5
    assert h12 == pytest.approx(10.5, abs=0.1)
    assert l12 == pytest.approx(8.5, abs=0.1)


def test_recommend_keeps_a_gap_between_watermarks():
    """低水位必须明显低于高水位，否则会"刚暂停就恢复"来回抖。"""
    for total in (4, 8, 12, 24, 48, 80):
        high, low = gpu.recommend_vram_thresholds(total)
        assert high > low, total
        assert high - low >= 1.0, (total, high, low)
        assert high < total, (total, high)      # 总得留点余量


def test_recommend_handles_bad_input():
    for bad in (0, -1, None, "abc"):
        assert gpu.recommend_vram_thresholds(bad) == (0.0, 0.0)


def test_describe_thresholds_mentions_ratio():
    text = gpu.describe_thresholds(10.5, 8.5, 12)
    assert "10.5" in text and "8.5" in text and "12" in text
    assert gpu.describe_thresholds(0, 0) == "未设置阈值"


def test_vram_query_returns_none_without_nvidia_smi(monkeypatch):
    """读不到显存要返回 None（调用方据此放行），而不是抛异常或返回 0。"""
    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi 不存在")

    monkeypatch.setattr(gpu.subprocess, "run", boom)
    assert gpu.vram_query() is None
    assert gpu.vram_used_gb() == -1.0
    assert gpu.vram_total_gb() == -1.0
