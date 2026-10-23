"""Common file/work handoff for GUI, automation and scripts. No Qt dependency."""
from censor.releases.models import normalize_number


def confirmed_work_items(censor_service, paths=None):
    selected = None if paths is None else {str(path) for path in paths}
    grouped = {}
    for item in censor_service.store.confirmed_censored():
        if selected is not None and item.path not in selected:
            continue
        key = normalize_number(item.number)
        if not key:
            continue
        row = grouped.setdefault(key, {"number": item.number, "paths": []})
        row["paths"].append(item.path)
    return grouped


def queue_confirmed(censor_service, release_service, paths=None):
    numbers = [row["number"] for row in confirmed_work_items(censor_service, paths).values()]
    release_service.queue(numbers, note="人工确认有码")
    return numbers


def run_censor_workflow(cfg, paths, *, scan=True, lookup=True, should_stop=None,
                        log=print, progress=None, censor_service=None, release_service=None):
    """Run the same file services in headless workflows; never invent manual labels."""
    from copy import deepcopy
    from censor import CensorService, CensorOptions
    from censor.models import ProcessingStatus
    from censor.releases import ReleaseService
    from utils.file_scan import iter_media_files
    cfg = deepcopy(cfg)
    stopped = should_stop or (lambda: False)
    report = dict(status="succeeded", scan=dict(completed=0, failed=0, cached=0),
                  lookup=dict(completed=0, failed=0, cached=0), error="")
    if stopped():
        report["status"] = "cancelled"
        return report
    censor = censor_service or CensorService(cfg)
    release = release_service or ReleaseService(cfg)
    files = list(iter_media_files(paths, cfg.video_extensions, cfg.exclude_path_keywords,
                                 stopped, log))
    if scan:
        opts = CensorOptions(detect_visual=cfg.censor_visual_enabled)
        for index, result in enumerate(censor.probe_many(files, opts, should_stop=stopped, log=log), 1):
            if result.status is ProcessingStatus.CANCELLED:
                report["status"] = "cancelled"
                return report
            key = "completed" if result.status is ProcessingStatus.COMPLETED else "failed"
            report["scan"][key] += 1
            report["scan"]["cached"] += int(result.cache_hit)
            if progress:
                progress(int(index * 100 / max(1, len(files))))
    if stopped():
        report["status"] = "cancelled"
        return report
    if lookup:
        numbers = queue_confirmed(censor, release, files)
        for result in release.lookup_many(numbers, should_stop=stopped, log=log):
            if result.status is ProcessingStatus.CANCELLED:
                report["status"] = "cancelled"
                return report
            key = "completed" if result.status is ProcessingStatus.COMPLETED else "failed"
            report["lookup"][key] += 1
            report["lookup"]["cached"] += int(result.cache_hit)
    if stopped():
        report["status"] = "cancelled"
    elif report["scan"]["failed"] or report["lookup"]["failed"]:
        report.update(status="failed", error="检测或检索存在未完成项，可在对应工作区重试")
    return report
