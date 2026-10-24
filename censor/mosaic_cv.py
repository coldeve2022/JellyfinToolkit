"""Content-independent local pixel-grid analysis with NumPy.

Periodic step energy in two directions produces structural candidates. Multiple
scales and spatial edge coverage improve candidate recall. Neither a structural
score nor repeated grid structure proves the purpose of the image processing.
Identical sampled pixels do not count as independent temporal evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import math
import hashlib
from pathlib import Path
from typing import Any, Optional

from censor.base import BaseDetector, DetectorError, ProbeContext
from censor.candidates import select_candidates
from censor.models import CensorState, Evidence, EvidenceSource, FrameCandidate
from censor.sampling import SampledFrame, SamplingCancelled, SamplingError
from censor.triage import triage_plan

__all__ = [
    "MosaicParams",
    "MosaicHit",
    "analyze_frame",
    "analyze_frames",
    "MosaicCvDetector",
    "numpy_available",
]


def numpy_available() -> tuple[bool, str]:
    """画面检测依赖 numpy；没装就老实报告，别在跑的时候炸。"""
    try:
        import numpy  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"缺少 numpy（pip install numpy）：{exc}"
    return True, ""


@dataclass(frozen=True)
class MosaicParams:
    """检测参数。全部可从配置覆盖，不写死在代码里。"""

    window: int = 160  # 滑窗边长（像素）
    stride: int = 120  # 滑窗步长
    min_block: int = 6  # 最小马赛克块边长
    max_block: int = 56  # 最大马赛克块边长
    edge_threshold: float = 1.5  # 只统计超过此强度的阶跃（压掉传感器噪声与压缩噪点）
    min_step: float = 2.0  # 边界平均台阶下限，低于此不算"有台阶"
    row_step: int = 2  # 每隔几行采样一次（2 = 减半算力，精度几乎不变）
    min_chunks: int = 5  # 一个窗口至少要能排下几个块才可信（防大周期假命中）
    min_coverage: float = 0.60  # 边界位置里"真有台阶"的比例下限
    min_ratio: float = 3.0  # 边界/块内 能量比下限
    ratio_cap: float = 40.0  # 计分时的比值上限（防单点爆表）
    strong_score: float = 32.0  # 达到此分才自动判"有码"
    review_score: float = 6.0  # 达到此分就进人工复核队列
    windows: tuple[int, ...] = ()  # 空表示兼容单尺度 window；服务默认使用多尺度
    candidate_gap_seconds: float = 10.0
    min_phase_consistency: float = 0.70

    def __post_init__(self):
        for name in ("window", "stride", "min_block", "max_block", "row_step", "min_chunks"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")
        if self.window < 16 or self.min_block < 2 or self.min_block > self.max_block:
            raise ValueError("窗口或块边长范围无效")
        if any(not isinstance(w, int) or w < 16 for w in self.windows):
            raise ValueError("windows 必须为至少 16 像素的窗口列表")
        for name in (
            "edge_threshold",
            "min_step",
            "min_coverage",
            "min_ratio",
            "ratio_cap",
            "strong_score",
            "review_score",
            "candidate_gap_seconds",
            "min_phase_consistency",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须为有限非负数")
        if self.min_coverage > 1 or self.min_phase_consistency > 1 or self.ratio_cap <= 0:
            raise ValueError("coverage 或 ratio_cap 超出范围")

    @classmethod
    def from_config(cls, cfg: Any) -> "MosaicParams":
        get = cfg.get if isinstance(cfg, dict) else (lambda k, d=None: getattr(cfg, k, d))
        defaults = cls()
        values = {}
        for spec in fields(cls):
            value = get("censor_" + spec.name, getattr(defaults, spec.name))
            if spec.name == "windows":
                value = tuple(value)
            elif isinstance(getattr(defaults, spec.name), int):
                value = int(value)
            else:
                value = float(value)
            values[spec.name] = value
        return cls(**values)


@dataclass
class MosaicHit:
    """单帧检测结果。"""

    score: float = 0.0
    block_size: int = 0
    box: Optional[tuple[int, int, int, int]] = None
    ratio: float = 0.0
    coverage: float = 0.0
    skin_ratio: float = 0.0

    def __bool__(self) -> bool:
        return self.score > 0.0


# ── 图像基础操作（全部对 numpy 数组）──────────────────────────────


def _to_gray(frame: SampledFrame, np) -> Any:
    """RGB / 灰度统一成 float32 灰度图。"""
    raw = np.frombuffer(frame.data, np.uint8)
    if frame.channels == 1:
        return raw.reshape(frame.height, frame.width).astype(np.float32)
    rgb = raw.reshape(frame.height, frame.width, 3).astype(np.float32)
    return rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _content_bounds(gray, np) -> tuple[int, int, int, int]:
    """裁掉黑边（``scale+pad`` 补出来的纯色区域）。

    黑边会造成两种误判：一是整窗接近常数导致比值爆炸，二是把"画面边界"
    当成强台阶。所以先按行/列标准差找出真正有内容的区域。
    """
    h, w = gray.shape
    row_std = gray.std(axis=1)
    col_std = gray.std(axis=0)
    rows = np.nonzero(row_std > 2.0)[0]
    cols = np.nonzero(col_std > 2.0)[0]
    if rows.size == 0 or cols.size == 0:
        return 0, h, 0, w
    return int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def _step_matrix(win, axis: int, threshold: float, np):
    """窗口内的阶跃能量矩阵 ``(行数, 位置数)`` —— 每行是一条一维阶跃曲线。

    保留"逐行"而不是直接压成一维，是因为马赛克区域往往只占窗口的一部分：
    压平会让 30% 的正常纹理把 70% 的网格信号稀释掉（实测分数会从 10⁶ 掉到 10）。
    """
    diff = np.abs(np.diff(win, axis=axis))
    if threshold > 0:
        diff = np.clip(diff - threshold, 0, None)
    return diff if axis == 1 else diff.T


def _best_grid(mat, p: MosaicParams, np) -> tuple[float, int, float, float]:
    """在阶跃矩阵里找最像"等宽阶梯"的周期。

    返回 ``(得分, 周期, 对齐率, 边界/块内比)``；找不到返回全 0。
    逐行判定、再取中位数 —— 半覆盖的窗口不会被整体拖死。
    """
    if p.row_step > 1:
        mat = mat[:: p.row_step]
    rows, positions = mat.shape
    best = (0.0, 0, 0.0, 0.0)
    for n in range(max(p.min_block, 2), p.max_block + 1):
        m = positions // n
        if m < max(4, p.min_chunks):
            continue
        folded = mat[:, : m * n].reshape(rows, m, n).sum(axis=1)  # (rows, n)
        peak = folded.argmax(axis=1)
        peak_energy = folded[np.arange(rows), peak]
        total = folded.sum(axis=1) + 1e-6
        boundary = peak_energy / m
        inner = (total - peak_energy) / (m * (n - 1))
        ratio = boundary / (inner + 1e-6)
        cover = peak_energy / total  # 对齐类占全部阶跃能量的比例
        good = (boundary >= p.min_step) & (cover >= p.min_coverage) & (ratio >= p.min_ratio)
        if int(good.sum()) < max(8, rows // 8):
            continue
        # A real grid shares its boundary phase across rows. Independent row
        # maxima otherwise turn diagonal/repeated natural textures into grids.
        phase_counts = np.bincount(peak[good], minlength=n)
        phase = int(phase_counts.argmax())
        if phase_counts[phase] / int(good.sum()) < p.min_phase_consistency:
            continue
        good &= peak == phase
        if int(good.sum()) < max(8, rows // 8):
            continue
        median_ratio = float(np.median(ratio[good]))
        median_cover = float(np.median(cover[good]))
        score = min(median_ratio, p.ratio_cap) * median_cover
        # 周期的"约数"也会完美对齐（真周期 16 时，8 同样对齐），
        # 所以分数接近时取**更大的周期** —— 真周期一定是能对齐的最大那个。
        if score > best[0] * 1.02 or (score >= best[0] * 0.98 and n > best[1]):
            best = (score, int(n), median_cover, median_ratio)
    return best


def _window_starts(length: int, window: int, stride: int) -> list[int]:
    if length < window:
        return []
    starts = list(range(0, length - window + 1, min(stride, window)))
    if starts[-1] != length - window:
        starts.append(length - window)
    return starts


def analyze_frame(
    frame: SampledFrame, params: Optional[MosaicParams] = None, *, should_stop=None
) -> MosaicHit:
    """单帧检测。返回最强网格证据（没有就返回空 hit）。"""
    ok, why = numpy_available()
    if not ok:
        raise DetectorError(why)
    import numpy as np

    p = params or MosaicParams()
    if frame.channels not in (1, 3):
        raise ValueError("画面通道只支持灰度或 RGB")
    if (
        frame.width <= 0
        or frame.height <= 0
        or len(frame.data) != (frame.width * frame.height * frame.channels)
    ):
        raise ValueError("画面尺寸或字节数不匹配")
    if should_stop is not None and should_stop():
        raise SamplingCancelled("画面分析已取消")
    gray = _to_gray(frame, np)
    y0, y1, x0, x1 = _content_bounds(gray, np)
    gray = gray[y0:y1, x0:x1]
    h, w = gray.shape
    hit = MosaicHit()
    for window, wy, wx in (
        (window, y, x)
        for window in sorted(set(p.windows or (p.window,)))
        for y in _window_starts(h, window, p.stride)
        for x in _window_starts(w, window, p.stride)
    ):
        if should_stop is not None and should_stop():
            raise SamplingCancelled("画面分析已取消")
        win = gray[wy : wy + window, wx : wx + window]
        if float(win.std()) < 4.0:  # 纯色/近纯色窗，不可能有网格
            continue
        score_x, n_x, cov_x, ratio_x = _best_grid(_step_matrix(win, 1, p.edge_threshold, np), p, np)
        if score_x <= 0:
            continue
        score_y, n_y, cov_y, ratio_y = _best_grid(_step_matrix(win, 0, p.edge_threshold, np), p, np)
        if score_y <= 0:
            continue
        # 两个方向的块必须基本一样大（正方形网格）
        if abs(n_x - n_y) > max(2, 0.15 * max(n_x, n_y)):
            continue
        score = min(score_x, score_y)
        # A tiny repeated palette (e.g. checkerboard artwork) is ambiguous.
        # Keep it available for review without treating it as strong evidence.
        if score >= p.strong_score and np.unique((win / 8).astype(np.uint8)).size < 6:
            score = min(score, p.review_score, p.strong_score * .5)
        if score > hit.score:
            hit = MosaicHit(
                score=score,
                block_size=int(round((n_x + n_y) / 2)),
                box=(wx + x0, wy + y0, wx + x0 + window, wy + y0 + window),
                ratio=min(ratio_x, ratio_y),
                coverage=min(cov_x, cov_y),
            )
    return hit


def analyze_frames(
    frames: list[SampledFrame],
    params: Optional[MosaicParams] = None,
    *,
    top: int = 5,
    should_stop=None,
) -> tuple[list[FrameCandidate], float]:
    """批量分析，返回 (按分数排好的候选帧, 最高分)。"""
    p = params or MosaicParams()
    if top < 0:
        raise ValueError("候选帧数量不能为负数")
    hits: list[FrameCandidate] = []
    for frame in frames:
        if should_stop is not None and should_stop():
            break
        hit = analyze_frame(frame, p, should_stop=should_stop)
        if hit.score > 0:
            hits.append(
                FrameCandidate(
                    timestamp=frame.timestamp,
                    score=hit.score,
                    block_size=hit.block_size,
                    box=hit.box,
                    frame_hash=hashlib.sha256(frame.data).hexdigest(),
                )
            )
    hits.sort(key=lambda c: (-c.score, c.timestamp))
    max_score = hits[0].score if hits else 0.0
    selected = select_candidates(
        hits, gap_seconds=p.candidate_gap_seconds, minimum_score=p.review_score, top=top
    )
    return selected, max_score


class MosaicCvDetector(BaseDetector):
    """把 :func:`analyze_frame` 接到检测器框架上。"""

    name = "visual.mosaic_cv"
    version = "5"
    capabilities = ("visual", "ffmpeg")
    title = "画面网格检测（CV）"

    def available(self) -> tuple[bool, str]:
        return numpy_available()

    def supports(self, ctx: ProbeContext) -> bool:
        if not bool(ctx.cfg_get("censor_visual_enabled", True)):
            return False
        return True

    def detect(self, ctx: ProbeContext) -> list[Evidence]:
        ok, why = self.available()
        if not ok:
            return [Evidence(EvidenceSource.VISUAL, CensorState.UNKNOWN, 0.0, why)]

        p = MosaicParams.from_config(ctx.cfg)
        sampler = self._sampler(ctx)
        requested = int(ctx.shared.get('requested_samples', ctx.cfg_get("censor_frame_samples", 60)))
        plan = triage_plan(ctx.path, ctx.cfg, requested,
                           explicit=('requested_samples' in ctx.shared
                                     or bool(ctx.shared.get('explicit_visual'))
                                     or bool(ctx.shared.get('deep_recheck'))))
        ctx.shared['triage'] = plan
        candidates, max_score, sampled, unique_frames, independent_strong = self._analyze_stream(ctx, sampler, p, requested)
        ctx.duration = ctx.duration or getattr(sampler, "last_duration", 0.0)
        low_risk = (plan['low_risk'] if plan['enabled'] and plan['phase'] == 'initial' else
                    (max_score < p.review_score and sampled >= 24
                     and sampled >= .8 * requested and unique_frames >= 4))
        plan['low_risk'] = bool(low_risk)
        prefix = (f"快速分流（{sampled}/{requested}计划帧，实际覆盖"
                  f"{plan['coverage_start']:.1f}–{plan['coverage_end']:.1f}s/"
                  f"{ctx.duration:.1f}s）"
                  if plan['enabled'] and plan['phase'] == 'initial' else '')
        ctx.shared["candidates"] = candidates
        ctx.shared["visual_score"] = max_score

        if independent_strong >= 2 and candidates:
            best = candidates[0]
            return [
                Evidence(
                    EvidenceSource.VISUAL,
                    CensorState.CENSORED,
                    0.85,
                    f"抽样多时点检出像素化网格（{independent_strong}处）："
                    f"{best.hms} 块边长≈{best.block_size}px 结构分{best.score:.1f}；非校准概率",
                )
            ]
        return [
            Evidence(
                EvidenceSource.VISUAL,
                CensorState.UNKNOWN,
                0.0,
                (
                    f"抽样疑似像素化，独立强证据不足（最高结构分 {max_score:.1f}）"
                    if max_score >= p.review_score
                    else prefix + f"{'抽样低风险' if low_risk else '抽样证据不足'}：{sampled}帧、"
                         f"{unique_frames}种画面，最高结构分 {max_score:.1f}；不代表全片不存在"
                ),
                review_required=not low_risk,
            )
        ]

    def _analyze_stream(self, ctx, sampler, p, requested):
        import inspect
        import tempfile
        import time
        method = getattr(sampler, 'iter_samples', None) or sampler.sample
        plan = ctx.shared['triage']
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        if plan['enabled'] and 'sample_indices' not in parameters:
            plan.update(enabled=False, phase='full', fallback='sampler_without_sample_indices',
                        initial_indices=list(range(requested)), order=list(range(requested)),
                        initial_samples=requested)
        scored, overview, records, hashes = [], [], {}, set()
        timestamps = []
        # Cache compact analysis only, never decoded pixel buffers. Dimensions
        # are part of the key so equal byte strings of different shapes differ.
        analyses, pixel_records = {}, {}
        limit = max(1, min(20, int(ctx.cfg_get('censor_candidates', 18))))
        first_count = plan['initial_samples']
        overview_count = min(6, limit, max(1, first_count))
        overview_positions = {round(i * (first_count - 1) / max(1, overview_count - 1))
                              for i in range(overview_count)}
        maximum, sampled = 0., 0
        ctx.shared['analyzed_frames'] = 0
        ctx.shared['reused_frames'] = 0
        def initial_is_low_risk():
            duration = ctx.duration or getattr(sampler, 'last_duration', 0.)
            enough_coverage = (duration > 0 and timestamps and
                               min(timestamps) <= .15 * duration and max(timestamps) >= .85 * duration)
            return bool(maximum < p.review_score and sampled >= 24
                        and sampled >= .8 * first_count and len(hashes) >= 4 and enough_coverage)

        def staged_frames():
            # A named parameter is required: old plugins accepting **kwargs may
            # silently ignore selected indices and must get the complete plan.
            stages = [plan['initial_indices']]
            if plan['enabled']:
                stages.append(plan['order'][first_count:])
            timeout = getattr(sampler, 'timeout', 900.)
            deadline = time.monotonic() + float(timeout)
            for phase, indices in enumerate(stages):
                if phase:
                    if initial_is_low_risk():
                        plan['low_risk'] = True
                        return
                    plan.update(phase='refined', refined=True)
                kwargs = dict(count=requested, duration=ctx.duration or getattr(sampler, 'last_duration', 0.),
                              should_stop=ctx.should_stop, log=ctx.log,
                              progress=lambda done, total: ctx.progress('精确取样', done, total))
                if plan['enabled']:
                    kwargs['sample_indices'] = indices
                    if 'deadline' in parameters:
                        kwargs['deadline'] = deadline
                stream = method(ctx.path, **kwargs)
                previous = sampled
                try:
                    yield from stream
                except SamplingError as exc:
                    if not plan['enabled'] or str(exc) != '抽帧未输出任何完整帧':
                        raise
                finally:
                    if hasattr(stream, 'close'):
                        stream.close()
                    count = max(sampled, previous + getattr(sampler, 'last_sampled_count', 0))
                    ctx.shared['sampled_frames'] = count
            if plan['enabled'] and plan['phase'] == 'initial':
                plan['low_risk'] = initial_is_low_risk()

        frames = staged_frames()
        # Only candidate/overview pixels are spooled, never an entire frame list
        # in memory. The temporary stream is closed on success, failure or stop.
        with tempfile.TemporaryFile() as spool:
            try:
                for frame in frames:
                    if ctx.should_stop():
                        raise SamplingCancelled('画面分析已取消')
                    sampled += 1
                    timestamps.append(frame.timestamp)
                    ctx.shared['sampled_frames'] = sampled
                    digest = hashlib.sha256(frame.data).hexdigest()
                    key = (frame.width, frame.height, frame.channels, digest)
                    if key in analyses:
                        original_hits, score = analyses[key]
                        hits = [replace(hit, timestamp=frame.timestamp) for hit in original_hits]
                        ctx.shared['reused_frames'] += 1
                    else:
                        hits, score = analyze_frames([frame], p, top=1, should_stop=ctx.should_stop)
                        analyses[key] = (hits, score)
                    scored.extend(hits)
                    maximum = max(maximum, score)
                    hashes.add(key)
                    is_overview = sampled - 1 in overview_positions
                    if hits or is_overview:
                        if key not in pixel_records:
                            pixel_records[key] = (spool.tell(), frame.width, frame.height, frame.channels)
                            spool.write(frame.data)
                        records[frame.timestamp] = pixel_records[key]
                    if is_overview:
                        overview.append(FrameCandidate(frame.timestamp, 0., frame_hash=digest))
                    ctx.shared['analyzed_frames'] = sampled
                    ctx.progress('分析画面', sampled, requested)
            finally:
                if hasattr(frames, 'close'):
                    frames.close()
                ctx.shared['sampled_frames'] = max(sampled, ctx.shared.get('sampled_frames', 0))
            if not sampled:
                raise DetectorError('没有可分析的画面帧')
            plan.update(sampled_samples=sampled, coverage_start=min(timestamps),
                        coverage_end=max(timestamps))
            candidates = select_candidates(scored, gap_seconds=p.candidate_gap_seconds,
                                           minimum_score=p.review_score)
            strong = sum(c.score >= p.strong_score for c in candidates)
            candidates = candidates[:limit]
            if len(candidates) < min(6, limit):
                occupied = {c.timestamp for c in candidates}
                available = [c for c in overview if c.timestamp not in occupied]
                slots = min(min(6, limit) - len(candidates), len(available))
                # Preserve both ends even when strong candidates consume slots.
                positions = {round(i * (len(available) - 1) / max(1, slots - 1))
                             for i in range(slots)}
                candidates.extend(available[i] for i in sorted(positions))
            pixels = []
            for candidate in candidates:
                offset, width, height, channels = records[candidate.timestamp]
                spool.seek(offset)
                pixels.append(SampledFrame(candidate.timestamp, width, height,
                                           spool.read(width * height * channels), channels))
            self._write_thumbs(ctx, sampler, candidates, pixels)
        return candidates, maximum, sampled, len(hashes), strong

    # ── 内部 ──
    def _sampler(self, ctx: ProbeContext):
        cached = ctx.shared.get("sampler")
        if cached is not None:
            return cached
        from censor.sampling import FrameSampler

        ffmpeg = str(ctx.ffmpeg or "")
        ffprobe = str(ctx.ffprobe or "")
        if not ffmpeg:
            raise DetectorError("没有可用的 ffmpeg，无法抽帧")
        sampler = FrameSampler(
            ffmpeg,
            ffprobe,
            width=int(ctx.cfg_get("censor_sample_width", 1280) or 1280),
            height=int(ctx.cfg_get("censor_sample_height", 720) or 720),
            pixel_format="gray",
            timeout=float(ctx.cfg_get("censor_sampling_timeout", 900)),
        )
        ctx.shared["sampler"] = sampler
        return sampler

    def _write_thumbs(
        self,
        ctx: ProbeContext,
        sampler,
        candidates: list[FrameCandidate],
        frames: list[SampledFrame],
    ) -> None:
        """给候选帧落缩略图 —— 复核界面直接显示，不必再解码。"""
        if not candidates or ctx.work_dir is None:
            return
        stem = Path(ctx.path).stem[:60]
        by_time = {frame.timestamp: frame for frame in frames}
        for idx, cand in enumerate(candidates):
            ctx.progress('保存证据图', idx, len(candidates))
            if ctx.should_stop():
                raise SamplingCancelled("缩略图生成已取消")
            out = Path(ctx.work_dir) / f"{stem}_{idx:02d}.png"
            if sampler.write_sampled_thumbnail(
                by_time[cand.timestamp], out, should_stop=ctx.should_stop, box=cand.box
            ):
                cand.thumb_path = str(out)
            if cand.box:
                crop = out.with_name(out.stem + '_crop.png')
                if sampler.write_sampled_thumbnail(by_time[cand.timestamp], crop,
                        should_stop=ctx.should_stop, box=cand.box, crop=True):
                    cand.crop_path = str(crop)
