"""Reproducible synthetic sampler A/B against the committed baseline."""
from pathlib import Path
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from censor.sampling import FrameSampler  # noqa: E402


def run():
    scratch = ROOT / '.local' / 'optimization-benchmark'
    scratch.mkdir(parents=True, exist_ok=True)
    baseline = scratch / 'baseline_sampling.py'
    baseline.write_bytes(subprocess.check_output(['git', 'show', 'a4e2b3b:censor/sampling.py'], cwd=ROOT))
    spec = importlib.util.spec_from_file_location('baseline_sampling', baseline)
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    spec.loader.exec_module(old)
    video = ROOT / '.local' / 'audit-20261023' / 'synthetic-performance.mp4'
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    assert video.exists() and ffmpeg and ffprobe
    result = dict(scope='8s synthetic clip; 24 identical timestamp samples at 1280x720 gray; not real-library throughput',
                  baseline='a4e2b3b', runs={})
    hashes = {}
    for name, cls in (('before', old.FrameSampler), ('after', FrameSampler)):
        sampler = cls(ffmpeg, ffprobe, width=1280, height=720, pixel_format='gray')
        calls = []
        original = sampler._run_command
        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        sampler._run_command = counted
        start = time.perf_counter()
        frames = sampler.sample(video, count=24)
        elapsed = time.perf_counter() - start
        hashes[name] = [(f.timestamp, hashlib.sha256(f.data).hexdigest()) for f in frames]
        sampling_calls = len(calls)
        calls.clear()
        start = time.perf_counter()
        suffix = 'jpg' if name == 'before' else 'png'
        for index, frame in enumerate(frames[:6]):
            assert sampler.write_sampled_thumbnail(frame, scratch / f'{name}-{index}.{suffix}')
        result['runs'][name] = dict(sampling_seconds=elapsed, sampling_subprocesses=sampling_calls,
            evidence_seconds=time.perf_counter() - start, evidence_subprocesses=len(calls),
            samples=len(frames), retained_batch_pixel_bytes=sum(len(f.data) for f in frames))
    result['timestamp_and_pixel_hashes_identical'] = hashes['before'] == hashes['after']
    # The public list wrapper above deliberately retains frames for compatibility.
    # Production detector consumes iter_samples and spools evidence to disk.
    result['production_raw_frame_retention'] = dict(before='all sampled frames', after='current decoded frame plus selected output evidence; temporary disk spool')
    out = ROOT / 'docs' / '系统优化性能-2026-10-23.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
