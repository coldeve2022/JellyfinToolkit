import json

from censor.mosaic_cv import MosaicParams


def test_benchmark_has_separate_splits_and_ambiguous_patterns():
    from censor.evaluation import build_cases, evaluate_cases

    cases = build_cases()
    development = {c.seed for c in cases if c.split == "development"}
    validation = {c.seed for c in cases if c.split == "validation"}
    assert development and validation and development.isdisjoint(validation)
    assert {
        "texture",
        "gradient",
        "text",
        "stripes",
        "checkerboard",
        "letterbox",
        "low_resolution",
        "local_pixelation",
    } <= {c.category for c in cases}
    assert all(c.expected is None for c in cases if c.category == "checkerboard")
    assert any(c.frame.width != 320 for c in cases if c.expected is True)
    report = evaluate_cases(cases[:2], MosaicParams(window=80))
    assert json.loads(json.dumps(report))["sample_count"] == 2
    assert report["algorithm_version"]
    assert report["rows"][0]["case_id"] == cases[0].case_id
    assert report["rows"][0]["elapsed_seconds"] >= 0
    assert "synthetic" in report["scope"]


def test_confusion_counts_do_not_include_ambiguous_cases():
    from censor.evaluation import build_cases, evaluate_cases

    cases = [c for c in build_cases() if c.category == "checkerboard"]
    report = evaluate_cases(cases, MosaicParams(window=80))
    assert report["ambiguous_count"] == len(cases)
    assert sum(report["confusion"].values()) == 0


def test_optional_h264_benchmark_decodes_real_encoded_pixels(tmp_path):
    import shutil
    import pytest
    from censor.evaluation import build_cases, encode_cases

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg unavailable")
    case = next(c for c in build_cases() if c.category == "local_pixelation")
    encoded = encode_cases([case], ffmpeg, ffprobe, tmp_path)
    assert len(encoded) == 1
    assert encoded[0].case_id.endswith("/h264-crf28")
    assert encoded[0].frame.width == case.frame.width
    assert encoded[0].frame.data != case.frame.data
