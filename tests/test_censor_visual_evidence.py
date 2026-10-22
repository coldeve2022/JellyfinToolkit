import pytest
import numpy as np

from censor.mosaic_cv import MosaicParams, analyze_frame, analyze_frames
from censor.sampling import SampledFrame, SamplingCancelled
from tests.test_censor_mosaic import _textured, _apply_mosaic, _frame


def test_tail_window_is_scanned():
    image = _textured(width=319, height=319)
    image = _apply_mosaic(image, (159, 159, 319, 319), 16)
    hit = analyze_frame(_frame(image), MosaicParams(stride=160))
    assert hit.score >= 32
    assert hit.box[2] == 319
    assert hit.box[3] == 319


def test_small_frame_uses_configured_small_scale():
    image = _apply_mosaic(_textured(width=96, height=96), (0, 0, 96, 96), 12)
    assert analyze_frame(_frame(image), MosaicParams(windows=(80, 160))).score > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stride": 0},
        {"min_block": 20, "max_block": 10},
        {"min_ratio": float("nan")},
        {"windows": (0,)},
        {"row_step": 0},
    ],
)
def test_bad_detector_parameters_fail_early(kwargs):
    with pytest.raises(ValueError):
        MosaicParams(**kwargs)


def test_invalid_frame_fails_with_useful_error():
    frame = SampledFrame(0, 10, 10, b"bad", channels=2)
    with pytest.raises(ValueError, match="通道|字节"):
        analyze_frame(frame)


def test_cancellation_checks_inside_single_frame():
    with pytest.raises(SamplingCancelled):
        analyze_frame(_frame(_textured()), should_stop=lambda: True)


def test_review_threshold_filters_weak_candidates():
    image = _apply_mosaic(_textured(), (0, 0, 640, 360), 16)
    candidates, maximum = analyze_frames([_frame(image)], MosaicParams(review_score=100))
    assert maximum > 0
    assert candidates == []


def test_candidates_are_spread_over_time():
    frames = [
        _frame(_apply_mosaic(_textured(index), (0, 0, 640, 360), 16), ts)
        for index, ts in enumerate((0, 1, 2, 20, 21, 40))
    ]
    candidates, _ = analyze_frames(frames, MosaicParams(candidate_gap_seconds=10), top=3)
    times = sorted(c.timestamp for c in candidates)
    assert times == [0, 20, 40]


def test_color_does_not_add_skin_ranking_signal():
    # Uniform RGB channels have precisely the same grayscale structure.
    image = _apply_mosaic(_textured(), (0, 0, 640, 360), 16).astype(np.uint8)
    rgb = np.stack([np.minimum(image.astype(int) + 100, 255), image, image // 2], axis=2).astype(
        np.uint8
    )
    frame = SampledFrame(0, 640, 360, rgb.tobytes(), channels=3)
    assert analyze_frame(frame).skin_ratio == 0


@pytest.mark.parametrize("duplicate,expected", [(True, "unknown"), (False, "censored")])
def test_repeated_source_frame_is_not_independent_visual_evidence(duplicate, expected):
    from censor.base import ProbeContext
    from censor.mosaic_cv import MosaicCvDetector

    image1 = _apply_mosaic(_textured(1), (0, 0, 640, 360), 16)
    image2 = image1 if duplicate else _apply_mosaic(_textured(2), (0, 0, 640, 360), 16)

    class Sampler:
        last_sampled_count = 2
        last_duration = 30.0

        def sample(self, *args, **kwargs):
            return [_frame(image1, 0), _frame(image2, 20)]

    ctx = ProbeContext(path="synthetic", shared={"sampler": Sampler()})
    evidence = MosaicCvDetector().detect(ctx)
    assert evidence[0].state.value == expected
