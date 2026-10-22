"""有码检测子系统 —— 对外只有一个入口，内部全部可插拔。

分层
----
``models``    领域模型：证据、结论、候选帧。不依赖 Qt / numpy / 网络。
``numbering`` 番号解析（纯字符串）。
``registry``  插件注册表（检测器 + 检索源各一张表）。
``base``      检测器基类与 :class:`ProbeContext`。
``file_signals`` 文件名 / NFO / 服务器标签 / 番号系列 —— 便宜的先跑。
``mosaic_cv`` 画面网格检测（numpy，贵，放最后）。
``fusion``    证据融合 → 结论（"检码宪法"写在模块注释里）。
``store``     SQLite 结果缓存 + 人工标注表。
``service``   编排：缓存、取消、批量。

加一种新检测方式
----------------
1. 新建一个模块，实现 :class:`censor.base.BaseDetector` 的 ``detect``；
2. 在本文件 :func:`register_builtins` 里 ``detectors.add(...)`` 一行；
3. 完。UI、缓存、融合都不用改。

无码版本检索在 :mod:`censor.releases` 里，用同一张 ``sources`` 注册表：

    from censor import releases          # 导入即注册 javdb / javbus / supjav
    report = releases.ReleaseService(cfg).lookup("PIYO-170")
"""

from __future__ import annotations

from censor.base import BaseDetector, DetectorError, ProbeContext
from censor.fusion import FusionPolicy, explain, fuse
from censor.models import (
    CensorState, Evidence, EvidenceSource, FrameCandidate, Verdict, VideoVerdict,
)
from censor.numbering import MovieNumber, parse_from_path, parse_number
from censor.registry import PluginSpec, Registry, detectors, sources
from censor.service import CensorOptions, CensorService
from censor.store import StoreStats, VerdictStore
from censor import releases  # noqa: F401 - 导入即注册检索源

__all__ = [
    "BaseDetector", "DetectorError", "ProbeContext",
    "CensorOptions", "CensorService", "StoreStats", "VerdictStore",
    "CensorState", "Evidence", "EvidenceSource", "FrameCandidate",
    "Verdict", "VideoVerdict",
    "FusionPolicy", "fuse", "explain",
    "MovieNumber", "parse_from_path", "parse_number",
    "PluginSpec", "Registry", "detectors", "sources",
    "register_builtins", "releases",
]


def register_builtins() -> None:
    """注册内置检测器。**重复调用安全**（覆盖同名注册）。"""
    # 延迟 import：避免 numpy 缺失时连累整个包
    from censor.file_signals import (
        FileNameDetector, NfoTagDetector, SeriesDetector, ServerTagDetector,
    )
    from censor.mosaic_cv import MosaicCvDetector

    detectors.add("series.rules", SeriesDetector, priority=10,
                  title="番号系列规则",
                  description="番号属于 FC2 / HEYZO 等天生无码厂牌即判无码",
                  tags=("file", "cheap"), replace=True)
    detectors.add("filename.rules", FileNameDetector, priority=20,
                  title="文件名关键词",
                  description="文件名关键词与 -U 后缀（弱证据）",
                  tags=("file", "cheap"), replace=True)
    detectors.add("nfo.tags", NfoTagDetector, priority=30,
                  title="NFO 标签",
                  description="同目录 .nfo 里的标签（弱证据）",
                  tags=("file", "cheap"), replace=True)
    detectors.add("server.tags", ServerTagDetector, priority=40,
                  title="媒体服务器标签",
                  description="Jellyfin / Emby 库里的 Genres 与 Tags（弱证据）",
                  tags=("file", "cheap"), replace=True)
    detectors.add("visual.mosaic_cv", MosaicCvDetector, priority=90,
                  title="画面网格检测",
                  description="抽关键帧检测等宽马赛克网格（需要 numpy + ffmpeg）",
                  tags=("visual", "expensive"), replace=True)


register_builtins()
