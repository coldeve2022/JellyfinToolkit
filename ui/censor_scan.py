"""Owned scan worker with throttled structured progress; no QWidget or GUI I/O."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import os
from pathlib import Path
from threading import RLock
import time
from copy import copy, deepcopy

from PySide6.QtCore import QThread, Signal
from censor.jobs import ScanJobs, scan_priority
from censor.models import VideoVerdict, ProcessingStatus
from censor.review_route import category


class CensorScanWorker(QThread):
    item_done = Signal(object)
    progressed = Signal(int, int)  # Backwards compatible file counts.
    logged = Signal(str)
    discovered = Signal(list)
    error = Signal(str)
    all_done = Signal(int)
    task_status = Signal(object)

    def __init__(self, service, paths, options, workers=4, parent=None, *, manual_deferred=False):
        super().__init__(parent)
        self._service, self._paths, self._options = copy(service), list(paths), deepcopy(options)
        if hasattr(service, 'cfg'):
            self._service.cfg = deepcopy(service.cfg)
        self._workers = max(1, min(int(workers or 1), 16, max(1, (os.cpu_count() or 2) // 2)))
        if options.detect_visual:
            pixels = int(service._cfg_get('censor_sample_width', 960)) * int(service._cfg_get('censor_sample_height', 540))
            retained = min(20, int(service._cfg_get('censor_candidates', 18))) + 26
            per_file = max(1, pixels * retained)
            budget = int(service._cfg_get('censor_memory_mb', 512)) * 1024 * 1024
            self._workers = min(self._workers, max(1, budget // per_file))
        self._stopped = False
        self.jobs = ScanJobs(service.store.db_path)
        self._manual_deferred = manual_deferred
        self._lock = RLock()
        self._last_emit = 0.
        self._clock = time.monotonic
        self._active_files = {}
        self._completed = 0
        self._counts = dict(discovered=0,total=0,completed=0,cached=0,failed=0,
                            deferred=0,review=0,manual=0,low_risk=0)
        self._phase, self._preparation = '发现文件', ''
        self.stats = {}

    def stop(self):
        self._stopped = True

    def _should_stop(self):
        return self._stopped

    def _publish(self, *, force=False):
        with self._lock:
            now = self._clock()
            if not force and now - self._last_emit < .25:
                return
            self._last_emit = now
            counts = dict(self._counts)
            active = [dict(path=p,stage=s,done=d,total=t) for p,(s,d,t) in self._active_files.items()]
            preparing = self._phase in ('发现文件','读取线索')
            percent = None if preparing else min(100, int(100 * counts['completed'] / max(1,counts['total'])))
            if self._phase == '完成':
                percent = 100
            detail = (f'{self._phase} · 已发现 {counts["discovered"]} · '
                      f'完成 {counts["completed"]}/{counts["total"]} · '
                      f'复用 {counts["cached"]} · 失败 {counts["failed"]} · 延后 {counts["deferred"]}')
            if self._preparation and preparing:
                detail += ' · ' + self._preparation
            if active:
                detail += '\n' + ' | '.join(f'{Path(a["path"]).name} · {a["stage"]}' +
                    (f' {a["done"]}/{a["total"]}' if a['total'] else '') for a in active)
            self.task_status.emit(dict(counts,phase=self._phase,progress=percent,detail=detail,active=active))

    def _file_progress(self, path, stage, done, total):
        with self._lock:
            self._active_files[path] = (stage, done, total)
            self._publish()

    def run(self):
        try:
            self._publish(force=True)
            self._run_scan()
        except Exception as exc:
            self.stats['error'] = str(exc)
            self.error.emit(str(exc))
        finally:
            self._phase = '已停止' if self._stopped else '部分失败' if self.stats.get('error') else '完成'
            self._publish(force=True)
            self.all_done.emit(self._completed)

    def _run_scan(self):
        from utils.file_scan import iter_media_files
        from utils.media_types import is_video
        self.jobs.enqueue_sources(self._paths, manual=self._manual_deferred)
        errors = []
        def note(message):
            errors.append(message)
            self.logged.emit(message)
        files, batch = [], []
        for path in iter_media_files(self._paths,
                video_exts=self._service._cfg_get('video_extensions', None),
                exclude_keywords=self._service._cfg_get('exclude_path_keywords', []),
                should_stop=self._should_stop, log=note):
            files.append(path)
            batch.append(path)
            self._counts['discovered'] = len(files)
            self._preparation = path
            self._publish()
            if len(batch) >= 100:
                self.jobs.enqueue(batch)
                self.discovered.emit(list(batch))
                batch.clear()
        self.jobs.enqueue(batch)
        if batch:
            self.discovered.emit(list(batch))
        if self._stopped:
            return
        for source in self._paths:
            if not os.path.exists(source) and is_video(source):
                self.jobs.enqueue([source])
                self.jobs.finished(VideoVerdict(path=source,status=ProcessingStatus.FAILED,
                                               error='文件已不存在或不可访问'))
                self.jobs.finish_sources([source])
                self._counts['failed'] += 1
        if not errors:
            self.jobs.finish_sources(self._paths)
        else:
            self.stats['error'] = '部分来源不可访问，保留来源供续跑'
            self.error.emit(self.stats['error'])
        self._paths = files
        self._phase = '读取线索'
        self._counts['total'] = len(files)
        self._publish(force=True)
        ordered = []
        for index,path in enumerate(files):
            if self._stopped:
                return
            priority,reason = scan_priority(path)
            self.jobs.prioritize(path,priority,reason)
            if self._manual_deferred:
                self.jobs.authorize(path)
            if priority == 2 and not self.jobs.authorized(path):
                self.jobs.defer(path)
                self._counts['deferred'] += 1
            else:
                ordered.append((priority,path,reason))
            self._preparation = f'{index+1}/{len(files)} · {Path(path).name}'
            self._publish()
        self._counts['total'] = len(ordered)
        self._phase = '分析视频'
        self._publish(force=True)
        paths = iter(sorted(ordered))
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {}
            def submit_next():
                item = next(paths,None)
                if item is None or self._stopped:
                    return
                _,path,reason = item
                self.jobs.started(path)
                self._file_progress(path,'校验文件与缓存',0,0)
                future = pool.submit(self._service.probe,path,self._options,
                    should_stop=self._should_stop,log=self.logged.emit,
                    progress=lambda stage,done,total,p=path:self._file_progress(p,stage,done,total))
                futures[future] = (path,reason)
            for _ in range(self._workers):
                submit_next()
            while futures:
                ready,_ = wait(futures,timeout=.1,return_when=FIRST_COMPLETED)
                for future in ready:
                    path,reason = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = VideoVerdict(path=path,error=str(exc),status=ProcessingStatus.FAILED)
                        result.verdict.needs_review = True
                    result.scheduling_note = reason
                    self.jobs.finished(result)
                    with self._lock:
                        self._active_files.pop(path,None)
                        self._completed += 1
                        self._counts['completed'] = self._completed
                        self._counts['cached'] += int(result.cache_hit)
                        route = category(result)
                        key = 'failed' if result.status not in (ProcessingStatus.COMPLETED,ProcessingStatus.CANCELLED) else route
                        if key in ('review','manual','low_risk','failed'):
                            self._counts[key] += 1
                        self._publish()
                    self.item_done.emit(result)
                    self.progressed.emit(self._completed,len(ordered))
                    if not self._stopped:
                        submit_next()
        if self._counts['failed']:
            self.stats['error'] = f'{self._counts["failed"]}个视频失败，可重试；其他结果已保存'
