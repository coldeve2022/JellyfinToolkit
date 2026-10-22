import numpy as np
from censor.mosaic_cv import MosaicParams, _best_grid, analyze_frame
from tests.test_censor_mosaic import _frame


def test_incoherent_row_phases_do_not_make_a_grid():
    matrix = np.zeros((80, 159), np.float32)
    for row in range(80):
        matrix[row, row % 16::16] = 30
    assert _best_grid(matrix, MosaicParams(row_step=1), np)[0] == 0


def test_two_color_checkerboard_is_ambiguous_not_strong_detection():
    y, x = np.mgrid[:320, :320]
    hit = analyze_frame(_frame(((x // 16 + y // 16) % 2) * 220))
    assert hit.score < MosaicParams().strong_score
    assert analyze_frame(_frame(((x // 16 + y // 16) % 2) * 220),
                         MosaicParams(review_score=100)).score < 32


def test_low_risk_is_not_automatic_absence_confirmation():
    from censor.models import Evidence, EvidenceSource, CensorState
    from censor.fusion import fuse
    result = fuse([Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0.,
                            '抽样低风险，未确证全片', review_required=False)])
    assert result.state is CensorState.UNKNOWN
    assert result.needs_review is False


def test_low_risk_plugin_does_not_hide_another_plugins_uncertainty():
    from censor.models import Evidence, EvidenceSource, CensorState
    from censor.fusion import fuse
    result = fuse([Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0., review_required=False),
                   Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0., 'ambiguous')])
    assert result.needs_review


def test_low_risk_requires_sufficient_unique_coverage_and_retains_overview():
    from censor.models import CensorState
    from censor.base import ProbeContext
    from censor.mosaic_cv import MosaicCvDetector

    class Sampler:
        duplicate = False

        def sample(self, *args, **kwargs):
            return [_frame(np.full((80, 80), 30 if self.duplicate else 30 + i), i * 20)
                    for i in range(30)]

    sampler = Sampler()
    ctx = ProbeContext('generic', cfg={'censor_frame_samples': 30}, shared={'sampler': sampler})
    evidence = MosaicCvDetector().detect(ctx)[0]
    assert evidence.state is CensorState.UNKNOWN
    assert evidence.review_required is False
    assert len(ctx.shared['candidates']) == 6
    assert all(c.score == 0 for c in ctx.shared['candidates'])
    sampler.duplicate = True
    ctx = ProbeContext('generic', cfg={'censor_frame_samples': 30}, shared={'sampler': sampler})
    assert MosaicCvDetector().detect(ctx)[0].review_required is True


def test_seek_sampling_does_not_repeat_a_long_gop_keyframe(tmp_path):
    import shutil
    import subprocess
    import pytest
    from censor.sampling import FrameSampler
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg unavailable')
    w, h = 160, 160
    first = np.full((h, w), 220, np.uint8)
    rest = np.full((h, w), 30, np.uint8)
    video = tmp_path / 'longgop.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'gray',
                    '-s', '160x160', '-r', '1', '-i', 'pipe:0', '-c:v', 'libx264',
                    '-qp', '0', '-g', '100', '-sc_threshold', '0', str(video)],
                   input=first.tobytes() + rest.tobytes() * 9, capture_output=True,
                   check=True, timeout=20)
    frames = FrameSampler(ffmpeg, ffprobe, width=w, height=h, pixel_format='gray').sample(video, count=4)
    assert len(frames) == 4
    assert all(np.frombuffer(frame.data, np.uint8).mean() < 100 for frame in frames)
