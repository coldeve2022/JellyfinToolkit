"""The service must execute the requested analysis, not reuse stale conclusions."""

import pytest

from censor.base import BaseDetector
from censor.models import CensorState, Evidence, EvidenceSource
from censor.registry import detectors
from censor.service import CensorOptions, CensorService


class Visual(BaseDetector):
    name = "test.visual"
    capabilities = ("visual",)

    def detect(self, ctx):
        return [Evidence(EvidenceSource.VISUAL, CensorState.CENSORED, 0.85, "synthetic grid")]


@pytest.fixture
def setup_service(tmp_path):
    detectors.add(Visual.name, Visual, tags=("visual",), replace=True)
    path = tmp_path / "sample.bin"
    path.write_bytes(b"file version one")
    cfg = {"censor_detectors": [Visual.name]}
    svc = CensorService(cfg, data_dir=tmp_path / "data")
    yield svc, path, cfg
    detectors.discard(Visual.name)


def test_visual_mode_does_not_reuse_metadata_only_cache(setup_service):
    svc, path, cfg = setup_service
    assert svc.probe(path, CensorOptions(detect_visual=False)).state is CensorState.UNKNOWN
    assert svc.probe(path).state is CensorState.CENSORED
    assert svc.probe(path).elapsed == 0


def test_empty_allowlist_runs_nothing(setup_service):
    svc, path, _ = setup_service
    result = svc.probe(path, CensorOptions(detectors=[]))
    assert result.verdict.evidence == []


def test_explicit_recheck_refreshes_visual_without_losing_manual_verdict(setup_service):
    svc, path, _ = setup_service
    svc.label(path, CensorState.UNCENSORED, 'human review')
    result = svc.probe(path, CensorOptions(force=True, recheck_manual=True, frame_samples=180))
    assert result.verdict.of(EvidenceSource.VISUAL) is not None
    assert result.state is CensorState.UNCENSORED
    assert result.detector_runs[Visual.name]['status'] == 'completed'


def test_visual_capability_obeys_global_config(setup_service):
    svc, path, cfg = setup_service
    cfg["censor_visual_enabled"] = False
    assert svc.probe(path).verdict.of(EvidenceSource.VISUAL) is None


def test_changed_configuration_invalidates_signature(setup_service):
    svc, path, cfg = setup_service
    first = svc.probe(path)
    cfg["censor_window"] = 240
    assert svc.probe(path).analysis_signature != first.analysis_signature


def test_changed_tags_and_nfo_invalidate_signature(setup_service):
    svc, path, _ = setup_service
    first = svc.probe(path, server_tags=["one"])
    second = svc.probe(path, server_tags=["two"])
    assert second.analysis_signature != first.analysis_signature
    path.with_suffix(".nfo").write_text("<movie/>")
    assert svc.probe(path, server_tags=["two"]).analysis_signature != second.analysis_signature


def test_cancelled_result_is_not_cached(setup_service):
    svc, path, _ = setup_service
    result = svc.probe(path, should_stop=lambda: True)
    assert result.status.value == "cancelled"
    assert svc.store.get(str(path)) is None


def test_replacement_does_not_inherit_manual_confirmation(setup_service):
    svc, path, _ = setup_service
    svc.label(path, CensorState.UNCENSORED, "old")
    path.write_bytes(b"replacement version two")
    result = svc.probe(path)
    assert result.verdict.of(EvidenceSource.MANUAL) is None
    assert result.state is CensorState.CENSORED
    assert svc.store.label_history(str(path))


def test_unknown_explicit_plugin_reports_failure(setup_service):
    svc, path, _ = setup_service
    result = svc.probe(path, CensorOptions(detectors=["missing.plugin"]))
    assert result.status.value == "failed"
    assert "missing.plugin" in result.error
    assert svc.store.get(str(path)) is None


def test_disabling_cache_does_not_write(setup_service):
    svc, path, _ = setup_service
    assert svc.probe(path, CensorOptions(use_cache=False)).state is CensorState.CENSORED
    assert svc.store.get(str(path)) is None


def test_file_changed_during_scan_is_not_cached(setup_service):
    svc, path, _ = setup_service

    class ReplacingVisual(Visual):
        def detect(self, ctx):
            path.write_bytes(b"file changed while analyzing")
            return super().detect(ctx)

    detectors.add(Visual.name, ReplacingVisual, replace=True)
    result = svc.probe(path)
    assert result.status.value == "partial"
    assert result.needs_review
    assert svc.store.get(str(path)) is None


def test_removing_manual_label_runs_visual_analysis_again(setup_service):
    svc, path, _ = setup_service
    svc.label(path, CensorState.UNCENSORED)
    assert svc.probe(path).state is CensorState.UNCENSORED
    svc.store.clear_labels()
    assert svc.probe(path).state is CensorState.CENSORED


def test_multiple_visual_plugins_aggregate_processing_counts(setup_service):
    svc, path, cfg = setup_service

    class First(Visual):
        def detect(self, ctx):
            ctx.shared.update(sampled_frames=2, analyzed_frames=2)
            return super().detect(ctx)

    class Second(Visual):
        name = "test.second"

        def detect(self, ctx):
            ctx.shared.update(sampled_frames=3, analyzed_frames=1)
            return super().detect(ctx)

    detectors.add(First.name, First, replace=True)
    detectors.add(Second.name, Second, replace=True)
    cfg["censor_detectors"] = [First.name, Second.name]
    try:
        result = svc.probe(path)
        assert (result.sampled_frames, result.analyzed_frames) == (5, 3)
        assert result.detector_runs["test.second"]["analyzed_frames"] == 1
        assert svc.probe(path).detector_runs == result.detector_runs
    finally:
        detectors.discard(Second.name)


def test_multiple_plugins_do_not_duplicate_review_candidates(setup_service):
    from censor.models import FrameCandidate

    svc, path, cfg = setup_service

    class First(Visual):
        def detect(self, ctx):
            ctx.shared["candidates"] = [
                FrameCandidate(0, 40, frame_hash="one"),
                FrameCandidate(20, 39, frame_hash="two"),
            ]
            return super().detect(ctx)

    class Second(First):
        name = "test.second"

    detectors.add(First.name, First, replace=True)
    detectors.add(Second.name, Second, replace=True)
    cfg["censor_detectors"] = [First.name, Second.name]
    try:
        assert [c.timestamp for c in svc.probe(path).candidates] == [0, 20]
    finally:
        detectors.discard(Second.name)

def test_failed_rescan_cannot_overwrite_cached_evidence(setup_service):
    svc, path, _ = setup_service
    from censor.models import FrameCandidate

    class WritingVisual(Visual):
        fail = False

        def detect(self, ctx):
            ctx.work_dir.mkdir(parents=True, exist_ok=True)
            thumb = ctx.work_dir / 'candidate.jpg'
            thumb.write_bytes(b'new evidence' if self.fail else b'original evidence')
            ctx.shared['candidates'] = [FrameCandidate(10, 40, thumb_path=str(thumb))]
            if self.fail:
                raise RuntimeError('rescan interrupted')
            return super().detect(ctx)

    detectors.add(Visual.name, WritingVisual, replace=True)
    original = svc.probe(path)
    thumb = original.candidates[0].thumb_path
    WritingVisual.fail = True
    assert svc.probe(path, CensorOptions(force=True)).status.value != 'completed'
    from pathlib import Path
    assert Path(thumb).read_bytes() == b'original evidence'
    assert svc.probe(path).candidates[0].thumb_path == thumb


def test_signature_ignores_unrelated_config_changes(tmp_path):
    """改主题/改 AI 模型不该让全库分析缓存作废。"""
    from censor.service import CensorOptions
    from config import ToolkitConfig

    cfg = ToolkitConfig()
    service = CensorService(cfg, data_dir=tmp_path)
    active = service._active_detectors(CensorOptions(detect_visual=False))
    target = tmp_path / "sample.mp4"
    target.write_bytes(b"x" * 32)
    before = service._analysis_signature(str(target), CensorOptions(), active, [])

    cfg.theme = "light" if cfg.theme != "light" else "dark"
    cfg.ai_model = "some-other-model"
    after = service._analysis_signature(str(target), CensorOptions(), active, [])
    assert after == before

    cfg.censor_strong_score = float(cfg.censor_strong_score) + 1.0
    changed = service._analysis_signature(str(target), CensorOptions(), active, [])
    assert changed != before, "检码阈值变了必须重算"
