"""编排层 —— 把"一个路径"变成"一条带证据的结论"，并负责缓存与取消。

这一层是**唯一**知道"检测器有哪些、按什么顺序跑、什么时候读缓存"的地方。
UI 只调 :meth:`CensorService.probe` / :meth:`probe_many`，不认识任何具体检测器；
新增一种检测方式时，改的是 ``censor/__init__.py`` 的注册表，不是这里。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from censor.base import BaseDetector, ProbeContext
from censor.fusion import FusionPolicy, fuse
from censor.models import CensorState, Evidence, EvidenceSource, VideoVerdict
from censor.models import ProcessingStatus
from censor.identity import identify_file
from censor.candidates import select_candidates
from censor.sampling import SamplingCancelled
from censor.numbering import parse_from_path
from censor.registry import detectors as detector_registry
from censor.store import StoreStats, VerdictStore

__all__ = ["CensorService", "CensorOptions"]


def _never_stop() -> bool:
    return False


def _noop(_msg: str) -> None:
    return None


@dataclass
class CensorOptions:
    """一次检测的开关。默认值即"最稳妥"。"""

    use_cache: bool = True
    force: bool = False  # 忽略缓存重算
    detect_visual: bool = True  # 是否抽帧看画面
    detectors: Optional[list[str]] = None  # None = 全部启用的检测器
    frame_samples: Optional[int] = None  # Explicit deeper inspection, without global config mutation.
    recheck_manual: bool = False


class CensorService:
    """有码检测服务（无 UI 依赖，可在脚本 / 测试里直接用）。"""
    _active_work_dirs = set()

    def __init__(
        self,
        cfg: Any = None,
        store: Optional[VerdictStore] = None,
        *,
        data_dir: str | Path | None = None,
        policy: Optional[FusionPolicy] = None,
    ) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir) if data_dir else None
        self.store = store or VerdictStore(self._default_db_path())
        self.policy = policy or FusionPolicy()
        self._tools: tuple[str, str] | None = None
        self._tools_config = None

    # ── 路径 ──
    def _default_db_path(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir / "censor.db"
        if self.cfg is not None and hasattr(self.cfg, "data_dir"):
            return Path(self.cfg.data_dir) / "censor.db"
        from config import DATA_DIR

        return Path(DATA_DIR) / "censor.db"

    @property
    def work_dir(self) -> Path:
        root = self.data_dir or self.store.db_path.parent
        return root / "censor_thumbs"

    def _thumb_dir(self, path: str) -> Path:
        digest = hashlib.md5(str(path).encode("utf-8", "replace")).hexdigest()[:16]
        return self.work_dir / digest

    # ── 工具解析 ──
    def tools(self) -> tuple[str, str]:
        """(ffmpeg, ffprobe)；解析不到就是空串，检测器会自己报告不可用。"""
        tool_config = self._cfg_get("merge_ffmpeg_path", "")
        if self._tools is not None and self._tools_config == tool_config:
            return self._tools
        ffmpeg = ffprobe = ""
        try:
            from utils.tools import resolve_tool

            ffmpeg = resolve_tool("ffmpeg", configured_path=tool_config) or ""
            ffprobe = resolve_tool("ffprobe", configured_path=tool_config) or ""
        except Exception:  # noqa: BLE001 - 独立使用时退化为直接找 PATH
            import shutil

            ffmpeg = shutil.which("ffmpeg") or ""
            ffprobe = shutil.which("ffprobe") or ""
        if not ffmpeg and self.cfg is not None:
            ffmpeg = str(getattr(self.cfg, "ffmpeg_path", "") or "")
        self._tools = (ffmpeg, ffprobe)
        self._tools_config = tool_config
        return self._tools

    def refresh_tools(self):
        self._tools = None
        self._tools_config = None

    # ── 缓存查询（给界面"不重复检查"用）──
    def cached_result(
        self,
        path: str | Path,
        options: Optional[CensorOptions] = None,
        *,
        server_tags: Optional[list[str]] = None,
    ) -> Optional[VideoVerdict]:
        """返回**仍然有效**的缓存结果；没有就返回 None。不抽帧、不写盘。

        界面在"导入一批文件"时先问一遍：能用的直接铺到列表里，只把真正要算的
        送进扫描队列。这样重复导入同一批片子不会从头再算一遍，
        但又不会因为"有缓存"而漏掉配置/文件变化导致的过期结果。
        """
        opts = options or CensorOptions()
        if opts.force or not opts.use_cache:
            return None
        target = str(path)
        try:
            stat = Path(target).stat()
            size, mtime = int(stat.st_size), float(stat.st_mtime)
            identity = identify_file(target).serialize()
            active = self._active_detectors(opts)
            label = self.store.get_label(target, file_identity=identity)
            signature = self._analysis_signature(
                target, opts, active, server_tags or [], label)
            cached = self.store.get(
                target, size, mtime, file_identity=identity, analysis_signature=signature)
        except Exception:  # noqa: BLE001 - 查询缓存失败就当没有缓存
            return None
        if cached is None:
            return None
        evidence = [ev for ev in cached.verdict.evidence
                    if ev.source is not EvidenceSource.MANUAL]
        if label is not None:
            evidence.append(Evidence(EvidenceSource.MANUAL, label[0], 1.0, label[1]))
        cached.verdict = fuse(evidence, self.policy)
        cached.elapsed = 0.0
        cached.cache_hit = True
        return cached

    # ── 单部 ──
    def probe(
        self,
        path: str | Path,
        options: Optional[CensorOptions] = None,
        *,
        should_stop: Callable[[], bool] = _never_stop,
        log: Callable[[str], None] = _noop,
        server_tags: Optional[list[str]] = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> VideoVerdict:
        """检测一部片。任何异常都不外抛，转为 ``error`` 字段返回。"""
        opts = options or CensorOptions()
        target = str(path)
        start = time.perf_counter()
        notify = progress or (lambda stage, done, total: None)
        notify('校验文件与缓存', 0, 0)
        try:
            stat = Path(target).stat()
            size, mtime = int(stat.st_size), float(stat.st_mtime)
        except OSError as exc:
            result = VideoVerdict(
                path=target, error=f"读不到文件: {exc}", status=ProcessingStatus.FAILED
            )
            result.verdict.needs_review = True
            return result

        if should_stop():
            result = VideoVerdict(path=target, status=ProcessingStatus.CANCELLED)
            result.verdict.needs_review = True
            return result
        try:
            identity = identify_file(target).serialize()
            active = self._active_detectors(opts)
            label = self.store.get_label(target, file_identity=identity)
            signature = self._analysis_signature(target, opts, active, server_tags or [], label)
            if not opts.force and opts.use_cache:
                cached = self.store.get(
                    target, size, mtime, file_identity=identity, analysis_signature=signature
                )
                if cached is not None:
                    # Re-fuse to reflect a changed/removed manual label without decoding.
                    evidence = [
                        ev
                        for ev in cached.verdict.evidence
                        if ev.source is not EvidenceSource.MANUAL
                    ]
                    if label is not None:
                        evidence.append(Evidence(EvidenceSource.MANUAL, label[0], 1.0, label[1]))
                    cached.verdict = fuse(evidence, self.policy)
                    cached.elapsed = 0.0
                    cached.cache_hit = True
                    notify('复用有效分析', 1, 1)
                    return cached
        except Exception as exc:
            result = VideoVerdict(path=target, error=str(exc), status=ProcessingStatus.FAILED)
            result.verdict.needs_review = True
            return result

        ctx = ProbeContext(
            path=target,
            cfg=self.cfg,
            size=size,
            mtime=mtime,
            should_stop=should_stop,
            log=log,
            progress=notify,
            server_tags=list(server_tags or []),
            # A cancelled rescan must never overwrite evidence referenced by
            # an older successful cache entry.
            work_dir=self._thumb_dir(target) / uuid.uuid4().hex,
        )
        ctx.number = parse_from_path(target)
        self._active_work_dirs.add(str(ctx.work_dir.resolve()))
        if opts.frame_samples is not None:
            ctx.shared['requested_samples'] = max(6, min(600, int(opts.frame_samples)))
        # 只有真要抽帧时才去解析 ffmpeg —— 纯元数据模式不该依赖外部工具
        if any("ffmpeg" in d.capabilities for d in active):
            ctx.ffmpeg, ctx.ffprobe = self.tools()

        evidence: list[Evidence] = []
        if label is not None:
            evidence.append(Evidence(EvidenceSource.MANUAL, label[0], 1.0, label[1] or "人工确认"))

        error = ""
        status = ProcessingStatus.COMPLETED
        finished = 0
        runs = {}
        all_candidates = []
        maximum_score = 0.0
        for detector in active:
            if should_stop():
                status = ProcessingStatus.CANCELLED
                break
            visual_started = False
            run_status = "completed"
            try:
                if not detector.supports(ctx):
                    continue
                # Only a matching manual confirmation can skip visual analysis.
                if "visual" in detector.capabilities and label is not None and not opts.recheck_manual:
                    continue
                if "visual" in detector.capabilities:
                    visual_started = True
                    ctx.shared.update(
                        sampled_frames=0, analyzed_frames=0, candidates=[], visual_score=0.0
                    )
                detector.require_available()
                evidence.extend(detector.detect(ctx))
                finished += 1
            except SamplingCancelled:
                run_status = "cancelled"
                status = ProcessingStatus.CANCELLED
                break
            except Exception as exc:  # noqa: BLE001 - 单个检测器失败不拖垮整条链
                run_status = "failed"
                error += ("；" if error else "") + f"{detector.name}: {exc}"
                log(f"⚠️ 检测器 {detector.name} 失败：{exc}")
            finally:
                if visual_started:
                    runs[detector.name] = {
                        "status": run_status,
                        "sampled_frames": int(ctx.shared.get("sampled_frames", 0)),
                        "analyzed_frames": int(ctx.shared.get("analyzed_frames", 0)),
                    }
                    all_candidates.extend(ctx.shared.get("candidates") or [])
                    maximum_score = max(maximum_score, float(ctx.shared.get("visual_score") or 0.0))

        if should_stop():
            status = ProcessingStatus.CANCELLED
        elif error:
            status = ProcessingStatus.PARTIAL if finished else ProcessingStatus.FAILED
        try:
            if identify_file(target).serialize() != identity:
                status = ProcessingStatus.PARTIAL
                error += "；文件在扫描期间发生变化，结果未缓存"
        except OSError as exc:
            status = ProcessingStatus.PARTIAL
            error += f"；文件已不可读取: {exc}"

        verdict = fuse(evidence, self.policy)
        if status is not ProcessingStatus.COMPLETED:
            # Partial evidence must not be presented as a confirmed file verdict.
            verdict = fuse([], self.policy)
            verdict.evidence = evidence
            verdict.reason = error or status.label
            verdict.needs_review = True
        result = VideoVerdict(
            path=target,
            size=size,
            mtime=mtime,
            duration=ctx.duration,
            number=ctx.number.number,
            provider=ctx.number.provider,
            verdict=verdict,
            candidates=select_candidates(
                all_candidates,
                gap_seconds=float(self._cfg_get("censor_candidate_gap_seconds", 10.0)),
                top=int(self._cfg_get("censor_candidates", 18)),
            ),
            visual_score=maximum_score,
            elapsed=time.perf_counter() - start,
            error=error,
            status=status,
            file_identity=identity,
            analysis_signature=signature,
            sampled_frames=sum(r["sampled_frames"] for r in runs.values()),
            analyzed_frames=sum(r["analyzed_frames"] for r in runs.values()),
            detector_runs=runs,
        )
        try:
            if opts.use_cache and status is ProcessingStatus.COMPLETED:
                self.store.put(result)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不该影响结果
            log(f"⚠️ 结果缓存写入失败：{exc}")
        self._active_work_dirs.discard(str(ctx.work_dir.resolve()))
        return result

    def _active_detectors(self, opts: CensorOptions) -> list[BaseDetector]:
        """按注册表优先级实例化检测器，按配置/选项过滤。"""
        allowed = opts.detectors
        if allowed is None:
            allowed = self._cfg_detector_names()
        if allowed is not None:
            missing = set(allowed) - set(detector_registry.names())
            if missing:
                raise ValueError(f"未注册的检测器: {sorted(missing)}")
        out: list[BaseDetector] = []
        for spec in detector_registry.specs():
            if allowed is not None and spec.name not in allowed:
                continue
            try:
                instance = spec.create()
                instance.capabilities = tuple(set(instance.capabilities) | set(spec.tags))
                if "visual" in instance.capabilities and (
                    not opts.detect_visual or not self._cfg_get("censor_visual_enabled", True)
                ):
                    continue
                out.append(instance)
            except Exception as exc:
                raise ValueError(f"构造检测器 {spec.name} 失败: {exc}") from exc
        return out

    def _cfg_detector_names(self) -> list[str]:
        if self.cfg is None:
            return list(detector_registry.names())
        names = self._cfg_get("censor_detectors", None)
        if isinstance(names, (list, tuple)):
            return [str(n) for n in names]
        return list(detector_registry.names())

    def _cfg_get(self, key, default=None):
        if isinstance(self.cfg, dict):
            return self.cfg.get(key, default)
        return getattr(self.cfg, key, default)

    #: 只有这些配置会影响像素化分析结果。
    #: **不能把整份配置塞进签名** —— 否则改个主题、改个 AI 模型名，
    #: 库里几百部片的分析缓存全部作废，白扫一遍。
    _ANALYSIS_CONFIG_KEYS = ("uncensored_keywords",)

    def _analysis_config(self) -> dict:
        raw = self.cfg if isinstance(self.cfg, dict) else vars(self.cfg) if self.cfg else {}
        return {
            key: value
            for key, value in raw.items()
            if (key.startswith("censor_") and key not in {"censor_workers", "censor_memory_mb", "censor_verify_replacements"})
            or key in self._ANALYSIS_CONFIG_KEYS
        }

    def _analysis_signature(self, target, opts, active, server_tags, label=None):
        from censor.mosaic_cv import MosaicParams

        cfg = self._analysis_config()
        nfo = []
        for path in (
            Path(target).with_suffix(".nfo"),
            Path(target).parent / "movie.nfo",
            Path(target).parent / "tvshow.nfo",
        ):
            if path.exists():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                nfo.append((path.name, digest.hexdigest()))
                break
        analysis_options = asdict(opts)
        for control in ("force", "use_cache"):
            analysis_options.pop(control)
        payload = {
            "schema": 2,
            "options": analysis_options,
            "policy": asdict(self.policy),
            "config": cfg,
            "visual_params": asdict(MosaicParams.from_config(self.cfg)),
            "plugins": [(d.name, d.version, sorted(d.capabilities)) for d in active],
            "tags": sorted(server_tags),
            "nfo": nfo,
            "manual_label": label,
        }
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ── 批量 ──
    def probe_many(
        self,
        paths: Iterable[str | Path],
        options: Optional[CensorOptions] = None,
        *,
        should_stop: Callable[[], bool] = _never_stop,
        log: Callable[[str], None] = _noop,
        progress: Optional[Callable[[int, int, VideoVerdict], None]] = None,
    ) -> Iterator[VideoVerdict]:
        """逐部检测并 yield —— 调用方可以边扫边显示，不用等全部跑完。"""
        items = [str(p) for p in paths]
        total = len(items)
        for index, path in enumerate(items, 1):
            if should_stop():
                return
            result = self.probe(path, options, should_stop=should_stop, log=log)
            if progress is not None:
                progress(index, total, result)
            yield result

    # ── 人工标注 ──
    def label(
        self,
        path: str | Path,
        state: CensorState,
        note: str = "",
        *,
        base: Optional[VideoVerdict] = None,
    ) -> VideoVerdict:
        """记下人工结论，并立刻把缓存里的结论改成它（不必等重算）。

        ``base`` 用于"这条结果还没写进缓存"的场景（例如界面刚拿到实时结果），
        传进来就能保住原有证据，不会只剩一条"人工确认"。
        """
        target = str(path)
        identity = identify_file(target).serialize()
        if base is not None and base.file_identity and base.file_identity != identity:
            raise ValueError("文件已替换，请重新扫描后确认")
        self.store.set_label(target, state, note, file_identity=identity)
        cached = self.store.get(target) or base
        if cached is None or cached.file_identity != identity:
            cached = VideoVerdict(path=target)
        cached.file_identity = identity
        cached.status = ProcessingStatus.COMPLETED
        others = [ev for ev in cached.verdict.evidence if ev.source is not EvidenceSource.MANUAL]
        evidence = [Evidence(EvidenceSource.MANUAL, state, 1.0, note or "人工确认")]
        cached.verdict = fuse(evidence + others, self.policy)
        self.store.put(cached)
        return cached

    def stats(self) -> StoreStats:
        return self.store.stats()

    def remove_label(self, path: str | Path) -> VideoVerdict | None:
        target = str(path)
        self.store.remove_label(target, identify_file(target).serialize())
        cached = self.store.get(target)
        if cached is not None:
            cached.verdict = fuse([e for e in cached.verdict.evidence
                                   if e.source is not EvidenceSource.MANUAL], self.policy)
            cached.analysis_signature = ""  # A prior manual result may have skipped visuals.
            self.store.put(cached)
        return cached

    def review_queue(self, limit: int = 500) -> list[VideoVerdict]:
        return self.store.review_queue(limit)

    def cleanup_thumbnails(self, max_bytes=512 * 1024 * 1024, min_age_seconds=86400):
        """Only unlink old unreferenced evidence inside this service's work root."""
        root = self.work_dir.resolve()
        live = self.store.referenced_thumbnails()
        active = tuple(Path(path) for path in self._active_work_dirs)
        files, total, removed = [], 0, 0
        if not root.exists():
            return dict(removed_bytes=0, remaining_bytes=0)
        for path in root.rglob('*'):
            try:
                resolved = path.resolve()
                if not path.is_file() or root not in resolved.parents:
                    continue
                stat = path.stat()
                total += stat.st_size
                if (str(resolved) not in live and not any(run in resolved.parents for run in active)
                        and time.time() - stat.st_mtime >= min_age_seconds):
                    files.append((stat.st_mtime, path, stat.st_size))
            except OSError:
                continue
        for _, path, size in sorted(files):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += size
            except OSError:
                continue
        return dict(removed_bytes=removed, remaining_bytes=total)
