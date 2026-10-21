"""注册表 / 缓存 / 编排层的用例 —— 全部不依赖 ffmpeg，可跑在任何 CI 上。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor.base import BaseDetector, DetectorError, ProbeContext  # noqa: E402
from censor.models import CensorState, Evidence, EvidenceSource  # noqa: E402
from censor.registry import Registry, detectors  # noqa: E402
from censor.service import CensorOptions, CensorService  # noqa: E402
from censor.store import VerdictStore  # noqa: E402

CFG = {
    "uncensored_keywords": ["无码", "流出", "fc2"],
    "censor_visual_enabled": False,
    "censor_detectors": ["series.rules", "filename.rules", "nfo.tags"],
    "censor_frame_samples": 6,
    "censor_candidates": 2,
    "censor_workers": 1,
}


# ── 注册表 ───────────────────────────────────────────────────

def test_registry_rejects_duplicate_without_replace():
    reg = Registry("demo")
    reg.add("a", lambda: object())
    with pytest.raises(KeyError):
        reg.add("a", lambda: object())
    reg.add("a", lambda: "new", replace=True)
    assert reg.get("a").create() == "new"


def test_registry_orders_by_priority_then_name():
    reg = Registry("demo")
    reg.add("late", lambda: "late", priority=90)
    reg.add("early", lambda: "early", priority=10)
    assert [s.name for s in reg.specs()] == ["early", "late"]


def test_registry_create_all_skips_broken_plugins():
    """一个插件构造失败不能拖垮整条链。"""
    def boom():
        raise RuntimeError("坏插件")

    reg = Registry("demo")
    reg.add("boom", boom, priority=1)
    reg.add("ok", lambda: "ok", priority=2)
    assert list(reg.create_all()) == ["ok"]


def test_registry_unknown_name_raises_helpful_error():
    reg = Registry("demo")
    with pytest.raises(KeyError, match="已注册"):
        reg.get("nope")


def test_builtin_detectors_are_registered():
    for name in ("series.rules", "filename.rules", "nfo.tags",
                 "server.tags", "visual.mosaic_cv"):
        assert detectors.has(name)


# ── 缓存 ────────────────────────────────────────────────────

def _verdict(path: str, state: CensorState = CensorState.CENSORED) -> object:
    from censor.fusion import fuse

    result = __import__("censor").VideoVerdict(path=path, size=10, mtime=1.0,
                                              number="ABC-123", visual_score=33.0)
    result.verdict = fuse([Evidence(EvidenceSource.VISUAL, state, 0.85, "画面命中")])
    return result


def test_store_roundtrip(tmp_path):
    store = VerdictStore(tmp_path / "c.db")
    item = _verdict("x.mp4")
    store.put(item)
    got = store.get("x.mp4", 10, 1.0)
    assert got is not None
    assert got.state is CensorState.CENSORED
    assert got.number == "ABC-123"
    assert got.verdict.evidence[0].detail == "画面命中"


def test_store_cache_invalidates_on_size_or_mtime(tmp_path):
    store = VerdictStore(tmp_path / "c.db")
    store.put(_verdict("x.mp4"))
    assert store.get("x.mp4", 11, 1.0) is None      # 大小变了
    assert store.get("x.mp4", 10, 2.0) is None      # 修改时间变了
    assert store.get("x.mp4", 10, 1.0) is not None  # 没变就命中


def test_store_labels_survive_probe_reset(tmp_path):
    """人工结论是语料，清自动结果时不能连带清掉。"""
    store = VerdictStore(tmp_path / "c.db")
    store.put(_verdict("x.mp4"))
    store.set_label("x.mp4", CensorState.UNCENSORED, "我看过了")
    store.clear_probes()
    assert store.get("x.mp4") is None
    assert store.get_label("x.mp4") == (CensorState.UNCENSORED, "我看过了")


def test_store_stats_and_review_queue_order(tmp_path):
    store = VerdictStore(tmp_path / "c.db")
    for index, score in enumerate((5.0, 40.0, 20.0)):
        item = _verdict(f"v{index}.mp4")
        item.visual_score = score
        store.put(item)
    stats = store.stats()
    assert stats.total == 3 and stats.censored == 3
    scores = [item.visual_score for item in store.review_queue(10)]
    assert scores == sorted(scores, reverse=True)


# ── 编排层 ──────────────────────────────────────────────────

def _make_file(tmp_path: Path, name: str, payload: bytes = b"x" * 32) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_service_decides_uncensored_for_provider_series(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    result = svc.probe(_make_file(tmp_path, "heyzo-1234.mp4"),
                       CensorOptions(detect_visual=False))
    assert result.state is CensorState.UNCENSORED
    assert result.verdict.confidence >= 0.6
    assert not result.verdict.needs_review
    assert result.number == "HEYZO-1234"


def test_service_unknown_for_plain_censored_tag(tmp_path):
    """只有"有码"标签、画面又没跑 → 未定 + 待复核，绝不能自动判有码。"""
    svc = CensorService(CFG, data_dir=tmp_path)
    path = _make_file(tmp_path, "ABCD-123.mp4")
    result = svc.probe(path, CensorOptions(detect_visual=False))
    assert result.state is CensorState.UNKNOWN
    assert result.verdict.needs_review


def test_service_cache_hit_is_instant(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    path = _make_file(tmp_path, "heyzo-4321.mp4")
    first = svc.probe(path, CensorOptions(detect_visual=False))
    second = svc.probe(path, CensorOptions(detect_visual=False))
    assert second.state is first.state
    assert second.elapsed == 0.0


def test_service_manual_label_wins_and_persists(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    path = _make_file(tmp_path, "ABCD-999.mp4")
    svc.probe(path, CensorOptions(detect_visual=False))
    svc.label(path, CensorState.CENSORED, "我亲眼看的")
    again = svc.probe(path, CensorOptions(detect_visual=False))
    assert again.state is CensorState.CENSORED
    assert again.verdict.of(EvidenceSource.MANUAL) is not None


def test_service_survives_broken_detector(tmp_path):
    class Boom(BaseDetector):
        name = "test.boom"
        title = "故意炸的检测器"

        def detect(self, ctx: ProbeContext):
            raise DetectorError("我就是坏的")

    detectors.add(Boom.name, Boom, priority=5, replace=True)
    try:
        svc = CensorService(CFG, data_dir=tmp_path)
        path = _make_file(tmp_path, "heyzo-1111.mp4")
        result = svc.probe(path, CensorOptions(detect_visual=False))
        assert result.state is CensorState.UNCENSORED     # 其它检测器照常工作
        assert "test.boom" in result.error
    finally:
        detectors.discard(Boom.name)


def test_service_missing_file_returns_error_not_exception(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    result = svc.probe(tmp_path / "不存在.mp4", CensorOptions(detect_visual=False))
    assert result.error
    assert result.state is CensorState.UNKNOWN


def test_service_probe_many_reports_progress(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    paths = [_make_file(tmp_path, f"heyzo-{i}123.mp4") for i in range(1, 5)]
    seen = []
    results = list(svc.probe_many(
        paths, CensorOptions(detect_visual=False),
        progress=lambda done, total, item: seen.append((done, total))))
    assert len(results) == 4
    assert seen[-1] == (4, 4)


def test_service_stops_early(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    paths = [_make_file(tmp_path, f"heyzo-{i}999.mp4") for i in range(1, 6)]
    counter = {"n": 0}

    def stop() -> bool:
        counter["n"] += 1
        return counter["n"] > 2

    results = list(svc.probe_many(paths, CensorOptions(detect_visual=False),
                                  should_stop=stop))
    assert len(results) < len(paths)


def test_elapsed_is_recorded(tmp_path):
    svc = CensorService(CFG, data_dir=tmp_path)
    path = _make_file(tmp_path, "heyzo-2222.mp4")
    started = time.perf_counter()
    result = svc.probe(path, CensorOptions(detect_visual=False))
    assert result.elapsed >= 0.0
    assert time.perf_counter() - started < 30
