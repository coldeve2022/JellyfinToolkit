"""Evidence, processing status and pixelation candidates.

This module has no Qt, network, FFmpeg or NumPy dependency. Serialized state
values remain compatible with historical caches; metadata is only a clue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProcessingStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PARTIAL = "partial"

    @property
    def label(self) -> str:
        return {"completed": "已完成", "cancelled": "已取消",
                "failed": "失败", "partial": "部分完成"}[self.value]


class CensorState(str, Enum):
    """一部片「有没有码」的判定结果。"""

    CENSORED = "censored"        # 画面里有马赛克
    UNCENSORED = "uncensored"    # 画面里没有马赛克（天生无码 / 已破解 / 流出版）
    UNKNOWN = "unknown"          # 现有证据不足以判定

    @property
    def label(self) -> str:
        return {"censored": "有码", "uncensored": "无码", "unknown": "未定"}[self.value]


class EvidenceSource(str, Enum):
    """证据来源。**置信度上限由来源决定**，避免某个弱信号自称很确定。"""

    FILENAME = "filename"        # 文件名规则（无码厂牌 / -U 后缀 …）
    NFO = "nfo"                  # 同目录 NFO 里的标签
    SERVER_TAG = "server_tag"    # Jellyfin / Emby 库里存的 Genres / Tags
    SERIES = "series"            # 番号系列规则：FC2 / HEYZO 这类天生无码厂牌
    WORK_DB = "work_db"          # 在线作品库：这部作品本身是否存在无码版
    VISUAL = "visual"            # 画面检测
    MANUAL = "manual"            # 人工确认（最高优先级）

    @property
    def ceiling(self) -> float:
        """该来源可声称的最高置信度。"""
        return {
            EvidenceSource.MANUAL: 1.00,
            EvidenceSource.VISUAL: 0.95,
            EvidenceSource.SERIES: 0.80,
            EvidenceSource.WORK_DB: 0.85,
            EvidenceSource.NFO: 0.60,
            EvidenceSource.SERVER_TAG: 0.55,
            EvidenceSource.FILENAME: 0.45,
        }[self]


@dataclass
class Evidence:
    """一条证据：某个来源认为"有码 / 无码"，以及它有多确定。"""

    source: EvidenceSource
    state: CensorState
    confidence: float = 0.5
    detail: str = ""
    review_required: Optional[bool] = None

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(float(self.confidence), self.source.ceiling))

    def __str__(self) -> str:  # 日志 / 表格直接用
        return f"[{self.source.value}] {self.state.label} {self.confidence:.2f} {self.detail}".strip()


@dataclass
class FrameCandidate:
    """一帧"疑似有马赛克"的候选画面（用于人工复核）。"""

    timestamp: float                  # 秒
    score: float                      # 网格分，越大越像马赛克
    block_size: int = 0               # 检测到的方块边长（像素）
    box: Optional[tuple[int, int, int, int]] = None  # 命中区域 x0,y0,x1,y1
    skin_ratio: float = 0.0           # 仅兼容旧缓存；通用检测不使用此字段
    thumb_path: str = ""              # 缩略图落盘路径（可为空）
    frame_hash: str = ""              # 分析像素摘要；重复帧不构成独立证据
    crop_path: str = ""

    @property
    def hms(self) -> str:
        """时间点格式化 ``01:23:45``。"""
        total = max(int(self.timestamp), 0)
        return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@dataclass
class Verdict:
    """融合后的最终结论。"""

    state: CensorState = CensorState.UNKNOWN
    confidence: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
    needs_review: bool = False
    reason: str = ""

    @property
    def is_decided(self) -> bool:
        return self.state is not CensorState.UNKNOWN

    def of(self, source: EvidenceSource) -> Evidence | None:
        for ev in self.evidence:
            if ev.source is source:
                return ev
        return None


@dataclass
class VideoVerdict:
    """一部视频的完整检测结果（缓存层就存它）。"""

    path: str
    size: int = 0
    mtime: float = 0.0
    duration: float = 0.0
    number: str = ""                  # 解析出的番号，如 ``PIYO-170``
    provider: str = ""                # 无码厂牌 / 片商线索
    verdict: Verdict = field(default_factory=Verdict)
    candidates: list[FrameCandidate] = field(default_factory=list)
    visual_score: float = 0.0         # 画面检测的最高分（无画面检测时为 0）
    elapsed: float = 0.0              # 本片耗时（秒），用于估算整库时间
    error: str = ""
    status: ProcessingStatus = ProcessingStatus.COMPLETED
    sampled_frames: int = 0
    analyzed_frames: int = 0
    file_identity: str = ""
    analysis_signature: str = ""
    detector_runs: dict = field(default_factory=dict)
    # Runtime provenance, deliberately not persisted as a file fact.
    cache_hit: bool = False
    from_history: bool = False
    scheduling_note: str = ''

    @property
    def state(self) -> CensorState:
        return self.verdict.state

    @property
    def is_decided(self) -> bool:
        return self.verdict.is_decided

    @property
    def needs_review(self) -> bool:
        return self.verdict.needs_review
