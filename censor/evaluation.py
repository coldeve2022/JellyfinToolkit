"""Reproducible, content-independent synthetic benchmark (not field accuracy)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import platform
import subprocess
import time
from typing import Iterable

from censor.mosaic_cv import MosaicCvDetector, MosaicParams, analyze_frame
from censor.sampling import SampledFrame


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    split: str
    seed: int
    expected: bool | None
    frame: SampledFrame


def build_cases() -> list[EvaluationCase]:
    import numpy as np

    cases = []
    for split, seed in [("development", 17), ("validation", 104729)]:
        rng = np.random.default_rng(seed)
        height, width = 240, 320
        y, x = np.mgrid[:height, :width]
        texture = np.clip(
            110 + 35 * np.sin(x / 20) + 30 * np.cos(y / 17) + rng.normal(0, 8, (height, width)),
            0,
            255,
        ).astype(np.uint8)

        def add(category, image, expected, suffix=""):
            image = np.ascontiguousarray(image, dtype=np.uint8)
            frame = SampledFrame(0, image.shape[1], image.shape[0], image.tobytes(), 1)
            cases.append(
                EvaluationCase(
                    f"{split}/{category}{suffix}", category, split, seed, expected, frame
                )
            )

        add("texture", texture, False)
        add("gradient", np.tile(np.linspace(0, 255, width), (height, 1)), False)
        text = np.full((height, width), 220, dtype=np.uint8)
        for left in range(10, width - 20, 35):
            text[40:160, left : left + 4] = 20
            for top in (40, 98, 156):
                text[top : top + 4, left : left + 20] = 20
        add("text", text, False)
        add("stripes", ((x // 10) % 2) * 220, False)
        add("checkerboard", ((x // 16 + y // 16) % 2) * 220, None)
        bordered = texture.copy()
        bordered[:30] = 0
        bordered[-30:] = 0
        add("letterbox", bordered, False)
        low = texture[::8, ::8]
        add("low_resolution", np.repeat(np.repeat(low, 8, axis=0), 8, axis=1), None)
        for block in (8, 16, 24):
            for left, top, position in [(0, 0, "edge"), (80, 48, "center")]:
                image = texture.copy()
                size = block * (160 // block)
                region = image[top : top + size, left : left + size]
                means = region.reshape(size // block, block, size // block, block).mean((1, 3))
                image[top : top + size, left : left + size] = np.repeat(
                    np.repeat(means, block, axis=0), block, axis=1
                )
                add("local_pixelation", image, True, f"/{block}/{position}")
                # Non-integral linear resampling smears block boundaries, as common
                # video resize pipelines do. Keep misses visible in the benchmark.
                scaled_x = np.linspace(0, width - 1, 240)
                horizontal = np.array([np.interp(scaled_x, np.arange(width), row) for row in image])
                scaled_y = np.linspace(0, height - 1, 180)
                scaled = np.array(
                    [np.interp(scaled_y, np.arange(height), col) for col in horizontal.T]
                ).T
                add("local_pixelation", scaled, True, f"/{block}/{position}/linear075")
    return cases


def encode_cases(
    cases: Iterable[EvaluationCase], ffmpeg: str, ffprobe: str, directory: str | Path
) -> list[EvaluationCase]:
    """Optional real H.264 degradation; no network or user media involved."""
    from censor.sampling import FrameSampler, SamplingError

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    output = []
    for index, case in enumerate(cases):
        frame = case.frame
        path = root / f"benchmark-{index:03d}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-s",
                f"{frame.width}x{frame.height}",
                "-r",
                "1",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            input=frame.data,
            capture_output=True,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        decoded = FrameSampler(
            ffmpeg, ffprobe, width=frame.width, height=frame.height, pixel_format="gray"
        ).frame_at(path, 0.0)
        if decoded is None:
            raise SamplingError("合成编码样本解码失败")
        output.append(replace(case, case_id=case.case_id + "/h264-crf28", frame=decoded))
    return output


def evaluate_cases(cases: Iterable[EvaluationCase], params: MosaicParams) -> dict:
    rows = []
    confusion = dict(tp=0, tn=0, fp=0, fn=0)
    groups = {}
    ambiguous = 0
    for case in cases:
        start = time.perf_counter()
        hit = analyze_frame(case.frame, params)
        detected = hit.score >= params.strong_score
        row = dict(
            case_id=case.case_id,
            category=case.category,
            split=case.split,
            seed=case.seed,
            expected=case.expected,
            detected=detected,
            score=hit.score,
            block_size=hit.block_size,
            elapsed_seconds=time.perf_counter() - start,
        )
        rows.append(row)
        group = groups.setdefault(f"{case.split}/{case.category}", dict(count=0, detected=0))
        group["count"] += 1
        group["detected"] += int(detected)
        if case.expected is None:
            ambiguous += 1
        else:
            key = ("tp" if detected else "fn") if case.expected else ("fp" if detected else "tn")
            confusion[key] += 1
    return dict(
        scope="synthetic frame-level mechanism benchmark; not real-video accuracy",
        python_version=platform.python_version(),
        platform=platform.system(),
        algorithm_version=MosaicCvDetector.version,
        params=asdict(params),
        sample_count=len(rows),
        ambiguous_count=ambiguous,
        confusion=confusion,
        groups=groups,
        rows=rows,
    )
