"""编排层 —— 把"一个路径"变成"一条带证据的结论"，并负责缓存与取消。

这一层是**唯一**知道"检测器有哪些、按什么顺序跑、什么时候读缓存"的地方。
UI 只调 :meth:`CensorService.probe` / :meth:`probe_many`，不认识任何具体检测器；
新增一种检测方式时，改的是 ``censor/__init__.py`` 的注册表，不是这里。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from censor.base import BaseDetector, ProbeContext
from censor.fusion import FusionPolicy, fuse
from censor.models import CensorState, Evidence, EvidenceSource, VideoVerdict
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
    force: bool = False                 # 忽略缓存重算
    detect_visual: bool = True          # 是否抽帧看画面
    skip_visual_when_series: bool = True  # 已确认天生无码就不必再抽帧
    detectors: Optional[list[str]] = None  # None = 全部启用的检测器


class CensorService:
    """有码检测服务（无 UI 依赖，可在脚本 / 测试里直接用）。"""

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
        if self._tools is not None:
            return self._tools
        ffmpeg = ffprobe = ""
        try:
            from utils.tools import resolve_tool

            ffmpeg = resolve_tool("ffmpeg") or ""
            ffprobe = resolve_tool("ffprobe") or ""
        except Exception:  # noqa: BLE001 - 独立使用时退化为直接找 PATH
            import shutil

            ffmpeg = shutil.which("ffmpeg") or ""
            ffprobe = shutil.which("ffprobe") or ""
        if not ffmpeg and self.cfg is not None:
            ffmpeg = str(getattr(self.cfg, "ffmpeg_path", "") or "")
        self._tools = (ffmpeg, ffprobe)
        return self._tools

    # ── 单部 ──
    def probe(
        self,
        path: str | Path,
        options: Optional[CensorOptions] = None,
        *,
        should_stop: Callable[[], bool] = _never_stop,
        log: Callable[[str], None] = _noop,
        server_tags: Optional[list[str]] = None,
    ) -> VideoVerdict:
        """检测一部片。任何异常都不外抛，转为 ``error`` 字段返回。"""
        opts = options or CensorOptions()
        target = str(path)
        start = time.perf_counter()
        try:
            stat = Path(target).stat()
            size, mtime = int(stat.st_size), float(stat.st_mtime)
        except OSError as exc:
            return VideoVerdict(path=target, error=f"读不到文件: {exc}")

        label = self.store.get_label(target)
        if not opts.force:
            cached = self.store.get(target, size, mtime)
            if cached is not None and opts.use_cache:
                if label is None or cached.verdict.of(EvidenceSource.MANUAL) is not None:
                    cached.elapsed = 0.0
                    return cached

        ctx = ProbeContext(
            path=target, cfg=self.cfg, size=size, mtime=mtime,
            should_stop=should_stop, log=log, server_tags=list(server_tags or []),
            work_dir=self._thumb_dir(target),
        )
        ctx.number = parse_from_path(target)
        active = self._active_detectors(opts)
        # 只有真要抽帧时才去解析 ffmpeg —— 纯元数据模式不该依赖外部工具
        if any(d.name == "visual.mosaic_cv" for d in active):
            ctx.ffmpeg, ctx.ffprobe = self.tools()

        evidence: list[Evidence] = []
        if label is not None:
            evidence.append(Evidence(EvidenceSource.MANUAL, label[0], 1.0,
                                     label[1] or "人工确认"))

        error = ""
        for detector in active:
            if should_stop():
                break
            try:
                if not detector.supports(ctx):
                    continue
                # 已人工确认 / 番号已定无码 时跳过最贵的画面检测
                if (detector.name == "visual.mosaic_cv"
                        and (label is not None
                             or (opts.skip_visual_when_series
                                 and any(ev.source is EvidenceSource.SERIES
                                         and ev.state is CensorState.UNCENSORED
                                         for ev in evidence)))):
                    continue
                evidence.extend(detector.detect(ctx))
            except Exception as exc:  # noqa: BLE001 - 单个检测器失败不拖垮整条链
                error = f"{detector.name}: {exc}"
                log(f"⚠️ 检测器 {detector.name} 失败：{exc}")

        verdict = fuse(evidence, self.policy)
        result = VideoVerdict(
            path=target, size=size, mtime=mtime, duration=ctx.duration,
            number=ctx.number.number, provider=ctx.number.provider,
            verdict=verdict,
            candidates=list(ctx.shared.get("candidates") or []),
            visual_score=float(ctx.shared.get("visual_score") or 0.0),
            elapsed=time.perf_counter() - start, error=error,
        )
        try:
            self.store.put(result)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不该影响结果
            log(f"⚠️ 结果缓存写入失败：{exc}")
        return result

    def _active_detectors(self, opts: CensorOptions) -> list[BaseDetector]:
        """按注册表优先级实例化检测器，按配置/选项过滤。"""
        allowed = opts.detectors
        if allowed is None:
            allowed = self._cfg_detector_names()
        blocked = set() if opts.detect_visual else {"visual.mosaic_cv"}
        out: list[BaseDetector] = []
        for spec in detector_registry.specs():
            if allowed and spec.name not in allowed:
                continue
            if spec.name in blocked:
                continue
            try:
                out.append(spec.create())
            except Exception:  # noqa: BLE001 - 构造失败 = 该检测器不可用
                continue
        return out

    def _cfg_detector_names(self) -> list[str]:
        if self.cfg is None:
            return list(detector_registry.names())
        names = getattr(self.cfg, "censor_detectors", None)
        if isinstance(names, (list, tuple)) and names:
            return [str(n) for n in names]
        return list(detector_registry.names())

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
        self.store.set_label(target, state, note)
        cached = self.store.get(target) or base
        if cached is None:
            cached = VideoVerdict(path=target)
        others = [ev for ev in cached.verdict.evidence
                  if ev.source is not EvidenceSource.MANUAL]
        evidence = [Evidence(EvidenceSource.MANUAL, state, 1.0, note or "人工确认")]
        cached.verdict = fuse(evidence + others, self.policy)
        self.store.put(cached)
        return cached

    def stats(self) -> StoreStats:
        return self.store.stats()

    def review_queue(self, limit: int = 500) -> list[VideoVerdict]:
        return self.store.review_queue(limit)
