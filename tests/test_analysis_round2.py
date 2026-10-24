"""Sampling coverage and repeated-pixel work for generic synthetic videos."""
import numpy as np
import pytest

from censor.base import ProbeContext
from censor.models import CensorState
from censor.mosaic_cv import MosaicCvDetector
from censor.sampling import SampledFrame


def context(count=30, *, candidates=18, repeat=False):
    class Sampler:
        last_duration = float(count * 20)
        last_sampled_count = 0
        def iter_samples(self, *args, **kwargs):
            for index in range(count):
                self.last_sampled_count = index + 1
                value = index % 2 if repeat else index
                yield SampledFrame(index * 20., 80, 80, bytes([30 + value]) * 6400)
    return ProbeContext('synthetic.mp4', cfg={'censor_frame_samples': count,
        'censor_candidates': candidates}, shared={'sampler': Sampler()})


def test_repeated_exact_pixels_do_not_repeat_cv_analysis(monkeypatch):
    from censor import mosaic_cv
    calls = []
    original = mosaic_cv.analyze_frames
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(mosaic_cv, 'analyze_frames', counted)
    ctx = context(repeat=True)
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert len(calls) == 2
    assert ctx.shared['sampled_frames'] == ctx.shared['analyzed_frames'] == 30
    assert ctx.shared['reused_frames'] == 28
    assert evidence.state is CensorState.UNKNOWN and evidence.review_required is True


def test_overview_covers_end_and_middle_instead_of_first_six_samples():
    ctx = context()
    MosaicCvDetector().detect(ctx)
    times = sorted(c.timestamp for c in ctx.shared['candidates'])
    assert len(times) == 6
    assert times[-1] >= 540
    assert any(240 <= t <= 340 for t in times)
    assert min(np.diff(times)) >= 80


@pytest.mark.parametrize('limit', [1, 2, 4])
def test_overview_obeys_the_requested_candidate_limit(limit):
    ctx = context(candidates=limit)
    MosaicCvDetector().detect(ctx)
    assert len(ctx.shared['candidates']) <= limit


def test_uniform_black_or_solid_samples_never_become_content_confirmation():
    ctx = context(repeat=True)
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert evidence.state is CensorState.UNKNOWN
    assert evidence.review_required is True
