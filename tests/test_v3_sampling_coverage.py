"""Synthetic regressions for coverage gates and atomic exact-pixel evidence."""
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

from censor.base import ProbeContext
from censor.mosaic_cv import MosaicCvDetector
from censor.sampling import FrameSampler, SampledFrame, SamplingCancelled, SamplingError, SamplingTimeout


@pytest.mark.parametrize('triage', [False, True])
@pytest.mark.parametrize('missing_end', [False, True])
def test_incomplete_temporal_coverage_requires_review_in_full_and_refined_plan(triage, missing_end):
    sampler = FrameSampler(sys.executable, width=32, height=32, pixel_format='gray')
    def frame_at(path, timestamp, **kwargs):
        # Exactly 80% of the plan succeeds, with one end uncovered.
        if (missing_end and timestamp >= 80) or (not missing_end and timestamp < 20):
            return None
        return SampledFrame(timestamp, 32, 32, bytes([20 + int(timestamp)]) * 1024, 1)
    sampler.frame_at = frame_at
    ctx = ProbeContext('DEMO-synthetic.mp4', duration=100, cfg={
        'censor_triage_prefixes': ['DEMO'], 'censor_triage_enabled': triage,
    }, shared={'sampler': sampler})
    evidence = MosaicCvDetector().detect(ctx)[0]
    plan = ctx.shared['triage']
    assert plan['sampled_samples'] == 48
    assert plan['phase'] == ('refined' if triage else 'full')
    assert plan['low_risk'] is False
    assert evidence.review_required is True


@pytest.mark.parametrize('duration', [0, None])
def test_unknown_duration_cannot_prove_low_risk_coverage(duration):
    class Sampler:
        def sample(self, *args, **kwargs):
            return [SampledFrame(index * 20, 32, 32, bytes([20 + index]) * 1024, 1)
                    for index in range(30)]
    ctx = ProbeContext('synthetic.mp4', duration=duration,
                       cfg={'censor_frame_samples': 30}, shared={'sampler': Sampler()})
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert ctx.shared['triage']['low_risk'] is False
    assert evidence.review_required is True


def test_audio_overhang_does_not_count_as_decoded_video_coverage(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg unavailable')
    source = tmp_path / 'DEMO-synthetic.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray',
        '-s', '32x32', '-r', '10', '-i', 'pipe:0', '-f', 'lavfi', '-i',
        'anullsrc=r=8000:cl=mono', '-t', '10', '-c:v', 'libx264', '-qp', '0',
        '-c:a', 'aac', str(source)],
        input=b''.join(bytes([20 + index]) * 1024 for index in range(80)),
        capture_output=True, check=True, timeout=20)
    sampler = FrameSampler(ffmpeg, ffprobe, width=32, height=32, pixel_format='gray')
    ctx = ProbeContext(str(source), cfg={'censor_triage_prefixes': ['DEMO']},
                       shared={'sampler': sampler})
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert ctx.duration == pytest.approx(10)
    assert ctx.shared['triage']['phase'] == 'refined'
    assert ctx.shared['triage']['sampled_samples'] == 48
    assert ctx.shared['triage']['coverage_end'] < .85 * ctx.duration
    assert evidence.review_required is True


@pytest.mark.parametrize('suffix', ['.png', '.jpg'])
@pytest.mark.parametrize('frame', [SampledFrame(0, 2, 2, b'', 1),
    SampledFrame(0, 0, 2, b'', 1), SampledFrame(0, 2, 2, b'x' * 8, 2)])
def test_sampled_evidence_rejects_invalid_pixels_before_touching_old_output(tmp_path, suffix, frame):
    output = tmp_path / ('evidence' + suffix)
    output.write_bytes(b'previous evidence')
    sampler = FrameSampler(sys.executable, width=2, height=2)
    with pytest.raises(SamplingError):
        sampler.write_sampled_thumbnail(frame, output)
    assert output.read_bytes() == b'previous evidence'
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize('outcome', ['eof', 'error', 'cancel', 'timeout'])
def test_sampled_jpeg_never_accepts_or_overwrites_old_evidence_on_incomplete_encode(tmp_path, monkeypatch, outcome):
    output = tmp_path / 'evidence.jpg'
    output.write_bytes(b'previous evidence')
    sampler = FrameSampler(sys.executable, width=2, height=2, pixel_format='gray')
    def run(command, **kwargs):
        if outcome != 'eof':
            Path(command[-1]).write_bytes(b'partial new image')
        if outcome == 'cancel':
            raise SamplingCancelled('cancelled during encoder')
        if outcome == 'timeout':
            raise SamplingTimeout('encoder timeout')
        return subprocess.CompletedProcess(command, 1 if outcome == 'error' else 0, b'', b'')
    monkeypatch.setattr(sampler, '_run_command', run)
    frame = SampledFrame(0, 2, 2, b'1234', 1)
    if outcome in ('cancel', 'timeout'):
        with pytest.raises(SamplingError):
            sampler.write_sampled_thumbnail(frame, output)
    else:
        assert sampler.write_sampled_thumbnail(frame, output) is False
    assert output.read_bytes() == b'previous evidence'
    assert list(tmp_path.iterdir()) == [output]


def test_sampled_png_cancellation_after_encode_preserves_previous_evidence(tmp_path, monkeypatch):
    output = tmp_path / 'evidence.png'
    output.write_bytes(b'previous evidence')
    sampler = FrameSampler(sys.executable, width=2, height=2, pixel_format='gray')
    cancelled = [False]
    write_png = sampler._write_png
    def encode(frame, path, **kwargs):
        result = write_png(frame, path, **kwargs)
        cancelled[0] = True
        return result
    monkeypatch.setattr(sampler, '_write_png', encode)
    with pytest.raises(SamplingCancelled):
        sampler.write_sampled_thumbnail(SampledFrame(0, 2, 2, b'1234', 1), output,
                                        should_stop=lambda: cancelled[0])
    assert output.read_bytes() == b'previous evidence'
    assert list(tmp_path.iterdir()) == [output]


def test_sampled_png_encoding_error_preserves_previous_evidence(tmp_path, monkeypatch):
    output = tmp_path / 'evidence.png'
    output.write_bytes(b'previous evidence')
    sampler = FrameSampler(sys.executable, width=2, height=2, pixel_format='gray')
    def fail(frame, path, **kwargs):
        path.write_bytes(b'partial png')
        raise OSError('synthetic write failure')
    monkeypatch.setattr(sampler, '_write_png', fail)
    try:
        assert sampler.write_sampled_thumbnail(SampledFrame(0, 2, 2, b'1234', 1), output) is False
    except OSError:
        pass  # Both surfaced and boolean I/O errors must preserve prior pixels.
    assert output.read_bytes() == b'previous evidence'
    assert list(tmp_path.iterdir()) == [output]
