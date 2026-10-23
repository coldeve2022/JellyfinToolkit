"""检索编排 —— 把"一个番号"变成"这部作品到底有没有无码版"。

判定规则（这就是"确有 / 疑似 / 未见 / 未查"的定义）
-------------------------------------------------
1. 任一站点给出**硬依据**（站点自带的無碼标记、无码厂牌片商、类别含流出版）
   → ``CONFIRMED`` 确有。硬依据的阈值是候选自带的 ``uncensored_confidence >= 0.8``。
2. 只有**软依据**（例如某个站的标题里写着 Uncensored/Decensored）
   → ``LIKELY`` 疑似。软依据不会让程序替你下结论，只在列表里排前面。
3. 查到了作品条目但没有任何无码版本 → ``NONE`` 未见。
4. 没番号 / 所有站点都失败 / 一条都没查到 → ``UNKNOWN`` 未查，
   并把每个站点的成败写进 ``report.sources``。

**关键设计**：``UNKNOWN`` 与 ``NONE`` 必须区分。把"站点被墙"说成"没有无码版"
是这类工具最容易犯、也最坑人的错 —— 用户会据此放弃一部其实有资源的片子。
"""

from __future__ import annotations

import time
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor

from censor.releases.base import ReleaseSource
from censor.releases.base import same_number
from censor.releases.models import (
    Availability, ReleaseCandidate, ReleaseQuery, ReleaseReport, normalize_number,
)
from censor.releases.store import ReleaseStats, ReleaseStore
from censor.releases.transport import TransportBlocked, build_transport
from censor.releases.transport import TransportCancelled, query_scope
from censor.models import ProcessingStatus

__all__ = ["ReleaseService", "ReleaseOptions", "decide_availability", "ALGORITHM_VERSION"]

#: 判定逻辑变更时递增，用来让旧缓存失效
ALGORITHM_VERSION = "3"

#: 硬依据的最低置信度
HARD_EVIDENCE = 0.8


def _never_stop() -> bool:
    return False


def _noop(_msg: str) -> None:
    return None


@dataclass
class ReleaseOptions:
    """一次检索的开关。"""

    use_cache: bool = True
    force: bool = False
    ttl_days: float = 7.0
    #: 只跑这些源（None = 用配置里的名单）
    sources: Optional[list[str]] = None


def decide_availability(candidates: Iterable[ReleaseCandidate]) -> Availability:
    """纯函数：由候选条目推出四态结论。单独抽出来是为了能直接测。"""
    items = [c for c in candidates]
    if not items:
        return Availability.UNKNOWN
    hits = [c for c in items if c.uncensored]
    if not hits:
        return Availability.NONE
    if max(c.uncensored_confidence for c in hits) >= HARD_EVIDENCE:
        return Availability.CONFIRMED
    return Availability.LIKELY


class ReleaseService:
    """无码版本检索服务（无 UI 依赖，可在脚本 / 测试里直接用）。"""

    def __init__(
        self,
        cfg=None,
        store: Optional[ReleaseStore] = None,
        *,
        data_dir: str | Path | None = None,
        transport=None,
        factory: Callable[[str], ReleaseSource] | None = None,
    ) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir) if data_dir else None
        self.store = store or ReleaseStore(self._default_db_path())
        self._transport = transport
        self._factory = factory
        self._source_errors = {}
        self._source_sessions = {}

    # ── 基础设施 ──
    def _default_db_path(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir / "censor.db"
        if self.cfg is not None and hasattr(self.cfg, "data_dir"):
            return Path(self.cfg.data_dir) / "censor.db"
        from config import DATA_DIR

        return Path(DATA_DIR) / "censor.db"

    def _cfg_get(self, key, default=None):
        if self.cfg is None:
            return default
        if isinstance(self.cfg, dict):
            return self.cfg.get(key, default)
        return getattr(self.cfg, key, default)

    @property
    def transport(self):
        if self._transport is None:
            self._transport = build_transport(self.cfg)
        return self._transport

    def build_sources(self, names: Optional[list[str]] = None) -> list[ReleaseSource]:
        """按注册表优先级实例化启用中的源。"""
        from censor.registry import sources as registry
        self._source_errors = {}

        allowed = names
        if allowed is None:
            configured = self._cfg_get("release_sources", None)
            if isinstance(configured, (list, tuple)):
                allowed = [str(name) for name in configured]

        if self._factory is not None:
            # 注入模式（测试 / 第三方插件）：按名字直接造，不依赖注册表
            target = allowed if allowed is not None else registry.names()
            out: list[ReleaseSource] = []
            for name in target:
                try:
                    out.append(self._factory(name))
                except Exception as exc:
                    self._source_errors[name] = f"初始化失败：{exc}"
            return out

        out = []
        for spec in registry.specs():
            if allowed is not None and spec.name not in allowed:
                continue
            try:
                session = self._source_sessions.get(spec.name)
                if session is None:
                    transport = self.transport
                    session = transport.fork() if hasattr(transport, 'fork') else transport
                    self._source_sessions[spec.name] = session
                out.append(spec.create(session, self.cfg))
            except Exception as exc:
                self._source_errors[spec.name] = f"初始化失败：{exc}"
        if allowed is not None:
            for name in set(allowed) - set(registry.names()):
                self._source_errors[name] = "初始化失败：未注册的检索源"
        return out

    def signature(self, sources: Optional[list[ReleaseSource]] = None) -> str:
        """源集合 + 版本 + 判定算法版本 —— 任一变化都让缓存失效。"""
        items = sources if sources is not None else self.build_sources()
        config = {name: self._cfg_get(name, default) for name, default in (
            ("release_sources", None), ("release_supjav_engines", None),
            ("release_supjav_mode", "auto"),
            ("release_detail_limit", 4))}
        digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
        return f"v{ALGORITHM_VERSION}|{digest}|" + "|".join(
            f"{s.name}:{s.version}" for s in items
        )

    # ── 主流程 ──
    def lookup(
        self,
        number: str,
        *,
        duration: float = 0.0,
        options: Optional[ReleaseOptions] = None,
        should_stop: Callable[[], bool] = _never_stop,
        log: Callable[[str], None] = _noop,
        title_hint: str = "",
    ) -> ReleaseReport:
        """查一个番号。任何站点失败都不会抛异常，而是写进 ``report.sources``。"""
        opts = options or ReleaseOptions(ttl_days=float(self._cfg_get('release_cache_days', 7)))
        start = time.perf_counter()
        query = ReleaseQuery(
            number=str(number or "").strip(),
            title_hint=str(title_hint or "").strip(),
            duration_seconds=float(duration or 0.0),
        )
        if not query.usable:
            return ReleaseReport(
                number="", availability=Availability.UNKNOWN,
                note="没有解析出番号", elapsed=time.perf_counter() - start,
                queried_at=time.time(),
            )

        key = normalize_number(query.key)
        if not self._cfg_get("release_enabled", True):
            return ReleaseReport(number=key, note="检索已禁用", status=ProcessingStatus.FAILED,
                                 queried_at=time.time())
        sources = self.build_sources(opts.sources)
        signature = self.signature(sources)
        if opts.use_cache and not opts.force and not should_stop():
            cached = self.store.get_fresh(key, ttl_seconds=opts.ttl_days * 86400)
            attempt = self.store.latest_attempt(key)
            incomplete = (attempt is not None and attempt.status is not ProcessingStatus.COMPLETED
                          and (cached is None or attempt.queried_at >= cached.queried_at))
            if cached is not None and not incomplete and self.store.signature_matches(key, signature):
                cached.elapsed = 0.0
                cached.cache_hit = True
                self.store.set_queue_status(key, 'done')
                return cached

        candidates: list[ReleaseCandidate] = []
        statuses: dict[str, str] = dict(self._source_errors)
        blocked = 0
        failed = len(statuses)
        succeeded = 0
        cancelled = should_stop()
        self.store.set_queue_status(key, "running")
        def search_source(source):
            if should_stop():
                return 'cancelled', '已取消', []
            try:
                ok, why = source.available()
                if not ok:
                    return 'failed', f'不可用：{why}', []
                found = source.search(query, should_stop=should_stop, log=log,
                                      limit=int(self._cfg_get('release_detail_limit', 4)))
                reason = getattr(source, 'incomplete_reason', '')
                return ('partial', f'部分完成：{reason}', found) if reason else (
                    'ok', f'命中 {len(found)} 条', found)
            except TransportCancelled:
                return 'cancelled', '已取消', []
            except TransportBlocked as exc:
                return 'blocked', f'被拦截：{exc}', []
            except Exception as exc:
                return 'failed', f'失败：{exc}', []

        with query_scope(self._cfg_get("release_max_requests", 40), should_stop):
            workers = max(1, min(3, int(self._cfg_get('release_source_workers', 1)), len(sources) or 1))
            if workers == 1:
                results = [search_source(source) for source in sources]
            else:
                # Each source owns routing/circuit state, while context copies
                # share this work's atomic budget and host scheduling session.
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='release-source') as pool:
                    futures = [pool.submit(copy_context().run, search_source, source) for source in sources]
                    results = [future.result() for future in futures]
            for source, (kind, detail, found) in zip(sources, results):
                statuses[source.name] = detail
                cancelled |= kind == 'cancelled'
                blocked += kind == 'blocked'
                failed += kind in ('failed', 'partial')
                succeeded += kind in ('ok', 'partial')
                candidates.extend(c for c in found if not query.number or same_number(c.number, query.number))
        cancelled = cancelled or should_stop()

        availability = decide_availability(candidates)
        note = ""
        status = ProcessingStatus.COMPLETED
        if cancelled:
            status, note = ProcessingStatus.CANCELLED, "检索已取消，可继续未完成任务"
        elif failed or blocked:
            status = ProcessingStatus.PARTIAL if succeeded else ProcessingStatus.FAILED
            if availability is Availability.NONE:
                availability = Availability.UNKNOWN
            note = "部分来源未完成，可重试" if succeeded else "站点请求失败，未能查证"
            if blocked and not succeeded:
                note = "站点被拦截，未能查证"
        elif not sources:
            status, note = ProcessingStatus.FAILED, "没有可用检索源，请检查来源设置"
        if availability is Availability.UNKNOWN and not note:
            if blocked and not candidates:
                note = "站点被拦截，未能查证"
            elif failed and not candidates:
                note = "站点请求失败，未能查证"
            elif not candidates:
                note = "站点无此番号"

        report = ReleaseReport(
            number=key,
            availability=availability,
            candidates=tuple(candidates),
            sources=statuses,
            note=note,
            elapsed=time.perf_counter() - start,
            queried_at=time.time(),
            status=status,
        )
        try:
            self.store.record_attempt(report)
            if opts.use_cache and status is ProcessingStatus.COMPLETED:
                self.store.put(report, signature)
            self.store.set_queue_status(key, "done" if status is ProcessingStatus.COMPLETED
                                        else "pending" if cancelled else "failed")
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不该影响结果
            log(f"⚠️ 检索结果写入失败：{exc}")
        return report

    def lookup_many(
        self,
        numbers: Iterable[str],
        *,
        options: Optional[ReleaseOptions] = None,
        should_stop: Callable[[], bool] = _never_stop,
        log: Callable[[], None] | None = None,
        progress: Callable[[int, int, ReleaseReport], None] | None = None,
    ):
        """逐个查并 yield，供界面边查边显示。"""
        items = [str(n) for n in numbers]
        total = len(items)
        for index, number in enumerate(items, 1):
            if should_stop():
                return
            report = self.lookup(number, options=options, should_stop=should_stop,
                                 log=log or _noop)
            if progress is not None:
                progress(index, total, report)
            yield report

    # ── 其它 ──
    def cached(self, number: str) -> ReleaseReport | None:
        return self.store.get(normalize_number(number))

    def fresh(self, number: str, options: Optional[ReleaseOptions] = None) -> ReleaseReport | None:
        opts = options or ReleaseOptions(ttl_days=float(self._cfg_get("release_cache_days", 7)))
        if not self._cfg_get("release_enabled", True):
            return None
        report = self.store.get_fresh(number, ttl_seconds=opts.ttl_days * 86400)
        if report is None or not self.store.signature_matches(number, self.signature()):
            return None
        attempt = self.store.latest_attempt(number)
        if (attempt and attempt.queried_at >= report.queried_at
                and attempt.status is not ProcessingStatus.COMPLETED):
            return None
        return report

    def cached_many(self, numbers: Iterable[str]) -> dict[str, ReleaseReport]:
        return self.store.get_many(numbers)

    def fresh_many(self, numbers, options: Optional[ReleaseOptions] = None):
        opts = options or ReleaseOptions(ttl_days=float(self._cfg_get('release_cache_days', 7)))
        if not self._cfg_get('release_enabled', True) or opts.force or not opts.use_cache:
            return {}
        return self.store.fresh_many(numbers, signature=self.signature(), ttl_seconds=opts.ttl_days * 86400)

    def queue(self, numbers: Iterable[str], *, priority: int = 1, note: str = "") -> int:
        return self.store.enqueue(
            [normalize_number(n) for n in numbers if normalize_number(n)],
            priority=priority, note=note,
        )

    def pending(self, limit: int = 200) -> list[str]:
        return self.store.pending(limit)

    def stats(self) -> ReleaseStats:
        return self.store.stats()
