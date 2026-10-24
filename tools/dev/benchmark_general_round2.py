"""Compare current generic analysis/history with the audit's Git baseline.

Uses synthetic pixels and private temporary databases; never scans user media.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BASE = '58f512d'


def baseline(path, name):
    source = subprocess.run(['git', 'show', f'{BASE}:{path}'], cwd=ROOT,
        capture_output=True, check=True).stdout.decode('utf-8-sig')
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
    sys.modules[name] = module
    exec(compile(source, path, 'exec'), module.__dict__)
    return module


def pixels(module, repeated):
    import numpy as np
    from censor.base import ProbeContext
    from censor.sampling import SampledFrame
    rng = np.random.default_rng(20261023)
    images = [rng.integers(0, 256, (180, 320), dtype=np.uint8).tobytes()
              for _ in range(4 if repeated else 60)]
    class Sampler:
        last_duration = 1200.
        last_sampled_count = 0
        def iter_samples(self, *args, **kwargs):
            for index in range(60):
                self.last_sampled_count = index + 1
                yield SampledFrame(index * 20., 320, 180, images[index % len(images)])
    calls = []
    original = module.analyze_frames
    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    module.analyze_frames = measured
    try:
        ctx = ProbeContext('generic.mp4', cfg={'censor_frame_samples': 60},
                           shared={'sampler': Sampler()})
        started = time.perf_counter()
        evidence = module.MosaicCvDetector().detect(ctx)[0]
        elapsed = time.perf_counter() - started
        return dict(seconds=round(elapsed, 4), cv_calls=len(calls),
                    score=ctx.shared['visual_score'], state=evidence.state.value,
                    review_required=evidence.review_required)
    finally:
        module.analyze_frames = original


def history(module, directory):
    from censor.models import VideoVerdict, Verdict
    store = module.VerdictStore(Path(directory) / 'history.db')
    for index in range(300):
        store.put(VideoVerdict(path=f'synthetic-{index:04}', verdict=Verdict(needs_review=True)))
    calls = []
    connect = store._connect
    @contextmanager
    def counted():
        calls.append(1)
        with connect() as conn:
            yield conn
    store._connect = counted
    started = time.perf_counter()
    results = store.review_queue(100)
    return dict(seconds=round(time.perf_counter() - started, 4),
                connections=len(calls), returned=len(results))


def main():
    from censor import mosaic_cv, store
    prior_cv = baseline('censor/mosaic_cv.py', 'round2_baseline_cv')
    prior_store = baseline('censor/store.py', 'round2_baseline_store')
    report = {'baseline': BASE, 'media': '60 synthetic 320x180 grayscale frames',
              'caveat': 'Single local run; CV stage only, excludes video decode and NAS I/O.'}
    for name, repeated in [('repeated', True), ('unique', False)]:
        report[name] = {'before': pixels(prior_cv, repeated), 'after': pixels(mosaic_cv, repeated)}
    ROOT.joinpath('.local').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='history-before-', dir=ROOT / '.local') as before:
        with tempfile.TemporaryDirectory(prefix='history-after-', dir=ROOT / '.local') as after:
            report['history_100_of_300'] = {'before': history(prior_store, before),
                                          'after': history(store, after)}
    target = ROOT / 'docs' / '通用审计第二轮-2026-10-23-performance.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
