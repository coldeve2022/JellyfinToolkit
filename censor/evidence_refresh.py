"""Supplemental local reference images, independent of detection and labels."""
from copy import deepcopy
import hashlib
from pathlib import Path
import time
import uuid

from censor.identity import identify_file
from censor.models import FrameCandidate, ProcessingStatus, VideoVerdict
from censor.sampling import FrameSampler, SamplingCancelled, SamplingError

REVIEW_RUN = 'review.frames'


def has_evidence(result):
    return any(usable_image(candidate.thumb_path) for candidate in result.candidates)


def usable_image(path):
    if not path:
        return False
    try:
        target = Path(path)
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


def next_round(previous):
    rounds = [0]
    if previous is not None:
        for run in previous.detector_runs.values():
            if isinstance(run, dict):
                try:
                    rounds.append(max(0, int(run.get('evidence_round', run.get('round', 0)))))
                except (TypeError, ValueError):
                    pass
    number = max(rounds) + 1
    return number, (number * .61803398875) % 1.


def _prior_images(*results):
    images, seen = [], set()
    for result in results:
        if result is None:
            continue
        for candidate in result.candidates:
            key = candidate.frame_hash or candidate.thumb_path
            if usable_image(candidate.thumb_path) and key not in seen:
                seen.add(key)
                images.append(candidate)
    return images


def refresh_evidence(service, path, *, count=24, base=None,
                     should_stop=lambda: False, log=lambda message: None, progress=None):
    """Capture bounded, distinct reference frames without rerunning classifiers.

    The original analysis status/error/signature and exact file-version manual
    authority are preserved. The operation has its own persisted outcome.
    """
    target = str(path)
    started = time.monotonic()
    count = max(1, min(60, int(count)))
    notify = progress or (lambda *args: None)
    result = VideoVerdict(path=target, status=ProcessingStatus.PARTIAL,
        error='尚无完整检测结果；补图不产生像素化结论')
    capture = dict(status='failed', requested=count, sampled=0, added=0,
                   duplicates=0, error='', round=1, sample_phase=0.)
    work = None
    durable = None
    display_base = False
    identity = ''
    fresh = []
    persist = False
    have_baseline = False
    identity_matches = True
    try:
        if should_stop():
            raise SamplingCancelled('补抓证据图已取消')
        info = identify_file(target)
        identity = info.serialize()
        durable = service.store.get(target, file_identity=identity)
        display_base = base is not None and base.file_identity == identity
        previous = base if display_base else durable
        if previous is not None:
            result = deepcopy(previous)
            have_baseline = True
        else:
            stat = Path(target).stat()
            result.size, result.mtime = stat.st_size, stat.st_mtime
            result.file_identity = identity
            result.verdict.needs_review = True
        number, phase = max((next_round(result), next_round(durable)), key=lambda item: item[0])
        capture.update(round=number, sample_phase=phase)
        result.candidates = _prior_images(result, durable)
        retained = result.candidates
        seen = {c.frame_hash for c in retained if c.frame_hash}
        ffmpeg, ffprobe = service.tools()
        sampler = FrameSampler(ffmpeg, ffprobe,
            width=int(service._cfg_get('censor_sample_width', 1280)),
            height=int(service._cfg_get('censor_sample_height', 720)),
            pixel_format='gray', timeout=float(service._cfg_get('censor_sampling_timeout', 900)))
        work = service._thumb_dir(target) / uuid.uuid4().hex
        service._active_work_dirs.add(str(work.resolve()))
        frames = sampler.iter_samples(target, count=count, duration=result.duration,
            should_stop=should_stop, log=log, sample_phase=phase,
            progress=lambda done, total: notify('补抓参考画面', done, total))
        try:
            for frame in frames:
                if should_stop():
                    raise SamplingCancelled('补抓证据图已取消')
                capture['sampled'] += 1
                digest = hashlib.sha256(frame.data).hexdigest()
                if digest in seen:
                    capture['duplicates'] += 1
                    continue
                out = work / f'reference-{len(fresh):03}.png'
                if not sampler.write_sampled_thumbnail(frame, out, should_stop=should_stop):
                    raise SamplingError('参考画面编码失败，旧证据保留')
                seen.add(digest)
                fresh.append(FrameCandidate(frame.timestamp, 0., thumb_path=str(out), frame_hash=digest))
            if should_stop():
                raise SamplingCancelled('补抓证据图已取消')
            capture['status'] = 'completed'
        finally:
            if hasattr(frames, 'close'):
                frames.close()
    except SamplingCancelled as exc:
        capture.update(status='cancelled', error=str(exc))
    except Exception as exc:  # noqa: BLE001 - one failed video must not stop a batch
        capture.update(status='partial' if fresh else 'failed', error=str(exc))
    finally:
        if identity:
            try:
                identity_matches = identify_file(target).serialize() == identity
            except OSError:
                identity_matches = False
        if not identity_matches:
            capture.update(status='failed', error='文件在补图期间已变化或不可读，未采用旧版本证据')
            result = VideoVerdict(path=target, file_identity=identity,
                status=ProcessingStatus.PARTIAL, error=capture['error'])
            result.verdict.needs_review = True
            fresh = []
        if fresh:
            retained = [c for c in result.candidates if usable_image(c.thumb_path)]
            # Bound repeated supplementation while retaining the strongest old
            # structural evidence and all the frames from this request.
            result.candidates = retained[:max(0, 60 - len(fresh))] + fresh
            persist = True
        capture['added'] = len(fresh)
        capture['elapsed'] = time.monotonic() - started
        persist = persist or bool(identity and have_baseline and identity_matches
                                  and capture['status'] != 'cancelled')
        result.detector_runs[REVIEW_RUN] = capture
        result.cache_hit = False
        result.from_history = False
        try:
            if durable is not None and display_base and persist:
                # A failed latest UI attempt is deliberately not a replacement
                # for the last successful analysis cache. Supplement both
                # views without converting either analysis status into the
                # outcome of this separate image operation.
                saved = deepcopy(durable)
                saved.candidates = deepcopy(result.candidates)
                saved.detector_runs[REVIEW_RUN] = capture
                service.store.put(saved, policy=service.policy)
                service.store.put(result, policy=service.policy, persist=False)
            else:
                service.store.put(result, policy=service.policy, persist=persist,
                                  allow_manual=identity_matches)
        except Exception as exc:  # noqa: BLE001
            capture.update(status='failed', error=f'补图记录保存失败：{exc}')
        if work is not None:
            service._active_work_dirs.discard(str(work.resolve()))
        if capture['error']:
            log(capture['error'])
        notify('补图完成' if capture['status'] == 'completed' else '补图未完整完成',
               capture['sampled'], count)
    return result
