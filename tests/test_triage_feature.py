"""Synthetic-only coverage for configurable, conservative review triage."""
import importlib
import sys

import numpy as np
import pytest


@pytest.mark.parametrize('name', ['FC2PPV1234567.mp4', 'fc2ppv 1234567.mkv', 'FC21234567.mp4'])
def test_compact_fc2_identifiers_select_the_same_effort_group(name):
    from censor.triage import triage_plan
    assert triage_plan(name, {}, 60)['enabled']
    assert triage_plan(name, {}, 60)['group'] == 'FC2'

from censor.base import ProbeContext
from censor.models import CensorState
from censor.mosaic_cv import MosaicCvDetector
from censor.sampling import FrameSampler, SampledFrame, SamplingCancelled, SamplingError


def plan(path, cfg=None, count=60, **kwargs):
    assert importlib.util.find_spec('censor.triage') is not None, 'triage planner is missing'
    return importlib.import_module('censor.triage').triage_plan(path, cfg or {}, count, **kwargs)


@pytest.mark.parametrize('name,group', [('FC2-123.mp4','FC2'), ('[fc2]_123.mp4','FC2'),
    ('FC20-123.mp4',''), ('XFC2-123.mp4',''), ('FC2word.mp4',''),
    (r'D:\FC2\ordinary.mp4',''), ('ordinary.mp4','')])
def test_source_group_requires_filename_token_boundaries(name, group):
    result = plan(name)
    assert result['group'] == group
    assert result['enabled'] is bool(group)
    assert len(result['order']) == len(set(result['order'])) == 60
    if group:
        initial = result['initial_indices']
        assert len(initial) == 30
        assert (initial[0], initial[-1]) == (0, 59)
        assert max(np.diff(initial)) <= 3


def test_custom_group_disable_and_explicit_request():
    assert plan('LOCAL-12.mp4', {'censor_triage_prefixes':['LOCAL']})['group'] == 'LOCAL'
    assert plan('FC2-12.mp4', {'censor_triage_enabled':False})['enabled'] is False
    assert plan('FC2-12.mp4', explicit=True)['initial_indices'] == list(range(60))


class SyntheticSampler:
    """Decode known synthetic pixels using the real sampler's selected-point loop."""
    def __init__(self, *, repeat=False, incomplete=False, hits=()):
        self.tool = FrameSampler(sys.executable, width=80, height=80, pixel_format='gray')
        self.calls = []
        self.repeat, self.incomplete, self.hits = repeat, incomplete, set(hits)
        self.tool.frame_at = self.frame_at
    def __getattr__(self, key):
        return getattr(self.tool, key)
    def frame_at(self, path, timestamp, **kwargs):
        index = int(timestamp / 20)
        self.calls.append(index)
        if self.incomplete and index % 3:
            return None
        if index in self.hits:
            rng = np.random.default_rng(index)
            data = np.repeat(np.repeat(rng.integers(15, 235, (10,10), dtype=np.uint8), 8, axis=0), 8, axis=1)
        else:
            data = np.full((80,80), 20 + (index % 2 if self.repeat else index), dtype=np.uint8)
        return SampledFrame(timestamp, 80, 80, data.tobytes())


def context(path='FC2-123.mp4', **sampler_options):
    sampler = SyntheticSampler(**sampler_options)
    ctx = ProbeContext(path, duration=1200, cfg={'censor_window':80, 'censor_stride':80,
        'censor_min_block':6, 'censor_max_block':12, 'censor_candidate_gap_seconds':10},
        shared={'sampler':sampler})
    return ctx, sampler


def test_first_phase_low_risk_stops_after_uniform_half_and_keeps_end_overview():
    ctx, sampler = context()
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert len(sampler.calls) == 30
    assert sampler.calls[0] == 0 and sampler.calls[-1] == 59
    assert evidence.state is CensorState.UNKNOWN and evidence.review_required is False
    assert '快速分流' in evidence.detail and '30/60' in evidence.detail
    assert ctx.shared['triage']['phase'] == 'initial'
    assert ctx.shared['triage']['low_risk'] is True
    assert ctx.shared['triage']['sampled_samples'] == 30
    assert ctx.shared['triage']['coverage_start'] < 20
    assert ctx.shared['triage']['coverage_end'] > 1180
    assert max(c.timestamp for c in ctx.shared['candidates']) > 1100


def test_late_suspicion_refines_without_redecoding_original_points():
    ctx, sampler = context(hits=(58, 59))
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert sorted(sampler.calls) == list(range(60))
    assert ctx.shared['triage']['refined'] is True
    assert ctx.shared['sampled_frames'] == 60
    assert evidence.state is CensorState.CENSORED


@pytest.mark.parametrize('options', [{'repeat':True}, {'incomplete':True}])
def test_duplicate_or_insufficient_initial_frames_force_refinement(options):
    ctx, sampler = context(**options)
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert ctx.shared['triage']['refined'] is True
    assert evidence.review_required is True
    assert ctx.shared['sampled_frames'] <= 60
    assert ctx.shared['reused_frames'] > 0 if options.get('repeat') else True


@pytest.mark.parametrize('path, explicit', [('ordinary.mp4', False), ('FC2-123.mp4', True)])
def test_normal_group_and_explicit_deep_scan_keep_complete_plan(path, explicit):
    ctx, sampler = context(path)
    if explicit:
        ctx.shared['requested_samples'] = 60
    MosaicCvDetector().detect(ctx)
    assert sampler.calls == list(range(60))
    assert ctx.shared['triage']['enabled'] is False


def test_legacy_sampler_without_selected_indices_falls_back_to_full_plan():
    ctx, sampler = context()
    class Legacy:
        last_duration = 1200
        last_sampled_count = 0
        def iter_samples(self, path, count=60, **kwargs):
            yield from sampler.tool.iter_samples(path, count=count, **kwargs)
    ctx.shared['sampler'] = Legacy()
    MosaicCvDetector().detect(ctx)
    assert sampler.calls == list(range(60))
    assert ctx.shared['triage']['enabled'] is False


def test_cancel_during_initial_phase_preserves_real_sample_count():
    ctx, sampler = context()
    ctx.should_stop = lambda: len(sampler.calls) >= 5
    with pytest.raises(SamplingCancelled):
        MosaicCvDetector().detect(ctx)
    assert ctx.shared['sampled_frames'] == 5
    assert len(sampler.calls) == 5


def test_sampler_selected_indices_use_the_same_full_plan_timestamps():
    tool = FrameSampler(sys.executable, width=2, height=2)
    times = []
    def frame_at(path, timestamp, **kwargs):
        times.append(timestamp)
        return SampledFrame(timestamp, 2, 2, b'\x00'*12, 3)
    tool.frame_at = frame_at
    list(tool.iter_samples('unused', count=6, duration=120))
    full = list(times)
    times.clear()
    frames = list(tool.iter_samples('unused', count=6, duration=120, sample_indices=[0,3,5]))
    assert times == [full[0], full[3], full[5]]
    assert len(frames) == tool.last_sampled_count == 3
    with pytest.raises(SamplingError):
        list(tool.iter_samples('unused', count=6, duration=120, sample_indices=[0,0]))


def test_force_check_flag_bypasses_initial_triage():
    ctx, sampler = context()
    ctx.shared['explicit_visual'] = True
    MosaicCvDetector().detect(ctx)
    assert sampler.calls == list(range(60))
    assert ctx.shared['triage']['enabled'] is False


def test_cancel_during_refinement_keeps_cumulative_sample_count():
    ctx, sampler = context(hits=(59,))
    ctx.should_stop = lambda: len(sampler.calls) >= 35
    with pytest.raises(SamplingCancelled):
        MosaicCvDetector().detect(ctx)
    assert ctx.shared['sampled_frames'] == 35
    assert ctx.shared['triage']['refined'] is True


def test_refinement_shares_the_initial_sampling_timeout(monkeypatch):
    from censor.sampling import SamplingTimeout
    from censor import sampling
    ctx, sampler = context(hits=(59,))
    elapsed = [100.]
    sampler.tool.timeout = 1
    original = sampler.tool.frame_at
    def frame_at(*args, **kwargs):
        frame = original(*args, **kwargs)
        if len(sampler.calls) == 30:
            elapsed[0] += 2
        return frame
    sampler.tool.frame_at = frame_at
    monkeypatch.setattr(sampling.time, 'monotonic', lambda: elapsed[0])
    with pytest.raises(SamplingTimeout):
        MosaicCvDetector().detect(ctx)
    assert ctx.shared['sampled_frames'] == 30
    assert len(sampler.calls) == 30


def test_synthetic_ffmpeg_video_triage_preserves_real_presentation_times(tmp_path):
    import shutil
    import subprocess
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg unavailable')
    video = tmp_path / 'FC2-synthetic.mp4'
    # 240 independently varying solid gray frames are unambiguous synthetic input.
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray',
        '-s', '32x32', '-r', '2', '-i', 'pipe:0', '-c:v', 'libx264', '-qp', '0', str(video)],
        input=b''.join(bytes([i % 200 + 20])*1024 for i in range(240)),
        capture_output=True, check=True, timeout=20)
    sampler = FrameSampler(ffmpeg, ffprobe, width=32, height=32, pixel_format='gray')
    ctx = ProbeContext(str(video), shared={'sampler':sampler})
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert ctx.shared['sampled_frames'] == 30
    assert ctx.shared['triage']['coverage_start'] < 2
    assert ctx.shared['triage']['coverage_end'] > 118
    assert evidence.review_required is False
    for candidate in ctx.shared['candidates']:
        assert sampler.frame_at(video, candidate.timestamp).timestamp == candidate.timestamp
