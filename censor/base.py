"""检测器基类 —— 所有"有码/无码"证据的生产者都实现它。

一个检测器只需要回答三件事：

``available()``  现在能不能用（依赖缺失要老实说，别在 detect 里抛异常）
``supports()``   这部片需不需要它跑（例如没解析出番号就不必查作品库）
``detect()``     产出 0~N 条 ``Evidence``

**检测器不许自己下结论**，也不许做文件重命名/删除这类副作用；
它只产出证据，由 :mod:`censor.fusion` 统一裁定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from censor.models import Evidence
from censor.numbering import MovieNumber

__all__ = ["ProbeContext", "BaseDetector", "DetectorError"]


class DetectorError(RuntimeError):
    """检测器内部的可预期失败（缺 ffmpeg、文件读不了…）。"""


def _noop(_msg: str) -> None:
    return None


def _never_stop() -> bool:
    return False


@dataclass
class ProbeContext:
    """一次检测所需的全部输入。**检测器只能读它，不能改全局状态。**"""

    path: str
    cfg: Any = None
    number: MovieNumber = field(default_factory=MovieNumber)
    size: int = 0
    mtime: float = 0.0
    duration: float = 0.0
    #: 外部工具路径（由服务层解析好再传进来，检测器不自己去翻 PATH）
    ffmpeg: str = ""
    ffprobe: str = ""
    #: Jellyfin / Emby 库里给这部片打的标签（可能为空）
    server_tags: list[str] = field(default_factory=list)
    #: 同目录 NFO 里的标签（可能为空）
    nfo_tags: list[str] = field(default_factory=list)
    #: 缩略图等中间产物放哪
    work_dir: Optional[Path] = None
    #: 协作取消：返回 True 就尽快收手
    should_stop: Callable[[], bool] = _never_stop
    #: 进度/日志回调
    log: Callable[[str], None] = _noop
    #: 检测器之间共享的临时数据（例如画面检测算出来的候选帧）
    shared: dict[str, Any] = field(default_factory=dict)
    #: 上一次缓存的结论（供"只补跑失败的检测器"用）
    previous: dict[str, Any] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return Path(self.path).name

    def cfg_get(self, key: str, default=None):
        """安全读配置：``cfg`` 可能是 dataclass、dict 或 None。"""
        if self.cfg is None:
            return default
        if isinstance(self.cfg, dict):
            return self.cfg.get(key, default)
        return getattr(self.cfg, key, default)


class BaseDetector:
    """检测器基类。子类至少要实现 ``detect``。"""

    #: 注册表里的名字，也是配置里开关用的名字
    name: str = ""
    #: 界面显示名
    title: str = ""

    def available(self) -> tuple[bool, str]:
        """(是否可用, 不可用原因)。默认可用。"""
        return True, ""

    def supports(self, ctx: ProbeContext) -> bool:  # noqa: ARG002 - 子类可覆写
        return True

    def detect(self, ctx: ProbeContext) -> list[Evidence]:  # pragma: no cover - 抽象
        raise NotImplementedError

    # ── 便捷工具 ──
    def require_available(self) -> None:
        ok, why = self.available()
        if not ok:
            raise DetectorError(why or f"{self.name} 不可用")

    def timed(self, fn: Callable[[], Any]) -> tuple[Any, float]:
        """跑一段逻辑并返回 (结果, 耗时秒)。"""
        start = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - start

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"
