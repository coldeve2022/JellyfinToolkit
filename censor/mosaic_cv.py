"""画面网格检测 —— 用传统图像算法找"马赛克"的物理特征。

判据（只用一条，但要求很硬）
--------------------------
日本 AV 的马赛克本质是**等宽正方形网格 + 块内被抹平 + 块边界有台阶**。
所以在一个局部窗口里，沿 x 方向的阶跃强度曲线 ``|∂g/∂x|`` 应该长成
"每 N 像素一个尖峰、尖峰之间几乎是平的"，也就是一条**周期性阶梯**。

于是算法是：

1. 把窗口内的阶跃曲线按候选周期 ``N`` 折叠成 ``N`` 个余数类；
2. 若某个余数类吃掉了绝大部分台阶能量（对齐率高），
   且块边界能量远大于块内能量（比值大），就是网格；
3. 两个方向都要成立、周期还要一致，才算命中。

先用合成样本验证过机制：给真实无码帧原地打上 N=16/24 的块平均马赛克，
比值从 ~1.5 跳到 10⁶ 量级；原帧不动。

**这个检测器不负责下结论。** 真实 1080p 片源经过一次有损编码后，
块内不再"绝对平坦"，比值会掉到个位数，与纹理周期性强的正常画面重叠。
所以它只产出"候选帧 + 分数"，由 :mod:`censor.fusion` 和人工复核收口。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from censor.base import BaseDetector, DetectorError, ProbeContext
from censor.models import CensorState, Evidence, EvidenceSource, FrameCandidate
from censor.sampling import SampledFrame

__all__ = ["MosaicParams", "MosaicHit", "analyze_frame", "analyze_frames",
           "MosaicCvDetector", "numpy_available"]


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

    window: int = 160            # 滑窗边长（像素）
    stride: int = 120            # 滑窗步长
    min_block: int = 6           # 最小马赛克块边长
    max_block: int = 56          # 最大马赛克块边长
    edge_threshold: float = 1.5  # 只统计超过此强度的阶跃（压掉传感器噪声与压缩噪点）
    min_step: float = 2.0        # 边界平均台阶下限，低于此不算"有台阶"
    row_step: int = 2            # 每隔几行采样一次（2 = 减半算力，精度几乎不变）
    min_chunks: int = 5          # 一个窗口至少要能排下几个块才可信（防大周期假命中）
    min_coverage: float = 0.60   # 边界位置里"真有台阶"的比例下限
    min_ratio: float = 3.0       # 边界/块内 能量比下限
    ratio_cap: float = 40.0      # 计分时的比值上限（防单点爆表）
    strong_score: float = 32.0   # 达到此分才自动判"有码"
    review_score: float = 6.0    # 达到此分就进人工复核队列

    @classmethod
    def from_config(cls, cfg: Any) -> "MosaicParams":
        get = (cfg.get if isinstance(cfg, dict) else
               (lambda k, d=None: getattr(cfg, k, d)))
        return cls(
            window=int(get("censor_window", cls.window) or cls.window),
            stride=int(get("censor_stride", cls.stride) or cls.stride),
            min_block=int(get("censor_min_block", cls.min_block) or cls.min_block),
            max_block=int(get("censor_max_block", cls.max_block) or cls.max_block),
            edge_threshold=float(get("censor_edge_threshold", cls.edge_threshold)),
            min_coverage=float(get("censor_min_coverage", cls.min_coverage)),
            min_ratio=float(get("censor_min_ratio", cls.min_ratio)),
            strong_score=float(get("censor_strong_score", cls.strong_score)),
            review_score=float(get("censor_review_score", cls.review_score)),
        )


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


def _skin_ratio(frame: SampledFrame, np) -> float:
    """肤色像素占比（经典 RGB 阈值规则）。只用来给候选帧排序。"""
    if frame.channels < 3:
        return 0.0
    rgb = np.frombuffer(frame.data, np.uint8).reshape(frame.height, frame.width, 3)
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    mask = (r > 95) & (g > 40) & (b > 20) & (r > g) & (r > b) & (r - g > 15) & (r - b > 20)
    return float(mask.mean())


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
        folded = mat[:, : m * n].reshape(rows, m, n).sum(axis=1)   # (rows, n)
        peak = folded.argmax(axis=1)
        peak_energy = folded[np.arange(rows), peak]
        total = folded.sum(axis=1) + 1e-6
        boundary = peak_energy / m
        inner = (total - peak_energy) / (m * (n - 1))
        ratio = boundary / (inner + 1e-6)
        cover = peak_energy / total        # 对齐类占全部阶跃能量的比例
        good = ((boundary >= p.min_step) & (cover >= p.min_coverage)
                & (ratio >= p.min_ratio))
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


def analyze_frame(frame: SampledFrame, params: Optional[MosaicParams] = None) -> MosaicHit:
    """单帧检测。返回最强网格证据（没有就返回空 hit）。"""
    ok, why = numpy_available()
    if not ok:
        raise DetectorError(why)
    import numpy as np

    p = params or MosaicParams()
    skin = _skin_ratio(frame, np)
    gray = _to_gray(frame, np)
    y0, y1, x0, x1 = _content_bounds(gray, np)
    gray = gray[y0:y1, x0:x1]
    h, w = gray.shape
    if h < p.window or w < p.window:
        return MosaicHit(skin_ratio=skin)

    hit = MosaicHit(skin_ratio=skin)
    for wy in range(0, h - p.window + 1, p.stride):
        for wx in range(0, w - p.window + 1, p.stride):
            win = gray[wy:wy + p.window, wx:wx + p.window]
            if float(win.std()) < 4.0:      # 纯色/近纯色窗，不可能有网格
                continue
            score_x, n_x, cov_x, ratio_x = _best_grid(
                _step_matrix(win, 1, p.edge_threshold, np), p, np)
            if score_x <= 0:
                continue
            score_y, n_y, cov_y, ratio_y = _best_grid(
                _step_matrix(win, 0, p.edge_threshold, np), p, np)
            if score_y <= 0:
                continue
            # 两个方向的块必须基本一样大（正方形网格）
            if abs(n_x - n_y) > max(2, 0.15 * max(n_x, n_y)):
                continue
            score = min(score_x, score_y)
            if score > hit.score:
                hit = MosaicHit(
                    score=score, block_size=int(round((n_x + n_y) / 2)),
                    box=(wx + x0, wy + y0, wx + x0 + p.window, wy + y0 + p.window),
                    ratio=min(ratio_x, ratio_y), coverage=min(cov_x, cov_y),
                    skin_ratio=skin,
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
    hits: list[FrameCandidate] = []
    for frame in frames:
        if should_stop is not None and should_stop():
            break
        hit = analyze_frame(frame, p)
        if hit.score > 0:
            hits.append(FrameCandidate(
                timestamp=frame.timestamp, score=hit.score,
                block_size=hit.block_size, box=hit.box,
                skin_ratio=hit.skin_ratio,
            ))
    hits.sort(key=lambda c: (c.score, c.skin_ratio), reverse=True)
    max_score = hits[0].score if hits else 0.0
    return hits[:top], max_score


class MosaicCvDetector(BaseDetector):
    """把 :func:`analyze_frame` 接到检测器框架上。"""

    name = "visual.mosaic_cv"
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
        frames = sampler.sample(
            ctx.path,
            count=int(ctx.cfg_get("censor_frame_samples", 60) or 60),
            duration=ctx.duration,
            should_stop=ctx.should_stop,
            log=ctx.log,
        )
        ctx.duration = ctx.duration or sampler.duration(ctx.path)
        candidates, max_score = analyze_frames(
            frames, p, top=int(ctx.cfg_get("censor_candidates", 5) or 5),
            should_stop=ctx.should_stop,
        )
        self._write_thumbs(ctx, sampler, candidates)
        ctx.shared["candidates"] = candidates
        ctx.shared["visual_score"] = max_score

        if max_score >= p.strong_score:
            best = candidates[0]
            return [Evidence(
                EvidenceSource.VISUAL, CensorState.CENSORED, 0.85,
                f"画面命中马赛克网格：{best.hms} 块边长≈{best.block_size}px 分数{best.score:.1f}",
            )]
        return [Evidence(
            EvidenceSource.VISUAL, CensorState.UNKNOWN, 0.0,
            f"未见马赛克网格（最高分 {max_score:.1f}，阈值 {p.strong_score:.0f}）",
        )]

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
            ffmpeg, ffprobe,
            width=int(ctx.cfg_get("censor_sample_width", 1280) or 1280),
            height=int(ctx.cfg_get("censor_sample_height", 720) or 720),
            pixel_format="rgb24",
        )
        ctx.shared["sampler"] = sampler
        return sampler

    def _write_thumbs(self, ctx: ProbeContext, sampler, candidates: list[FrameCandidate]) -> None:
        """给候选帧落缩略图 —— 复核界面直接显示，不必再解码。"""
        if not candidates or ctx.work_dir is None:
            return
        stem = Path(ctx.path).stem[:60]
        for idx, cand in enumerate(candidates):
            out = Path(ctx.work_dir) / f"{stem}_{idx:02d}.jpg"
            if sampler.write_thumbnail(ctx.path, cand.timestamp, out):
                cand.thumb_path = str(out)
