"""Export a reproducible synthetic frame benchmark without media-library access."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from censor.evaluation import build_cases, encode_cases, evaluate_cases
from censor.mosaic_cv import MosaicParams


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--single-scale", action="store_true")
    parser.add_argument("--ffmpeg", help="可选：同时评估真实 H.264 CRF28 编码后的合成样本")
    parser.add_argument("--ffprobe", default="")
    args = parser.parse_args()
    params = MosaicParams(windows=() if args.single_scale else (80, 160, 240))
    cases = build_cases()
    if args.ffmpeg:
        with tempfile.TemporaryDirectory() as tmp:
            cases += encode_cases(cases, args.ffmpeg, args.ffprobe, tmp)
    report = evaluate_cases(cases, params)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: report[key] for key in ("scope", "sample_count", "ambiguous_count", "confusion")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
