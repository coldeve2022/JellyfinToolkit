"""一个视频从"导入"到"处理完"的状态机 —— 全项目唯一的判定入口。

为什么要有这个模块
------------------
以前"这行是什么意思"散在三处：``models.CensorState``（结论）、``ProcessingStatus``
（执行结果）、``category()``（要不要人看）。UI 于是把它们压成 7 个混合标签、9 个
筛选菜单，其中「待确认」和「只看待复核」的代码完全一样 —— 说明分类本身就乱了。

现在拆成**两个正交维度**，各自只有一个来源：

``结论``（文件到底有没有码）
    = :class:`censor.models.CensorState`：有码 / 无码 / 未定。

``处理``（这个结果还需不需要人）
    = :class:`ReviewBucket`：排队中 / 未完成 / 待复核 / 已确认 / 低风险 / 已延后。

界面上就是两列，筛选项也只是这两个维度的组合，不再有"混合标签"。

生命周期
--------
::

    导入(排队中) → 检查中 → 检查完 ─┬─ 有码 ─┐
                                   ├─ 无码 ─┼→ 待复核 → 已确认
                                   └─ 未定 ─┘
                                     ↑
                          抽样低风险 / 名称线索延后（不进待办，但要能查到）
    检查失败/取消/部分完成 → 未完成（可重试）

``低风险`` 与 ``已延后`` **不是结论**，它们仍然是"未定"，只是不需要当下处理。
这一点必须显式表达，否则很容易被误读成"这部片没问题"。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from censor.models import CensorState, EvidenceSource, ProcessingStatus
from censor.tag_signals import classify_tag

__all__ = [
    "ReviewBucket", "WorkflowView", "BUCKET_LABELS", "VIEW_LABELS",
    "bucket", "category", "needs_review", "conclusion_label", "bucket_label",
    "in_view", "CONCLUDING_STATES", "awaits_replacement",
]


class ReviewBucket(str, Enum):
    """处理维度：这个结果还需不需要人。**与结论正交。**"""

    PENDING = "pending"        # 已导入、还没检查（排队中）
    FAILED = "failed"          # 检查没跑完：失败 / 取消 / 部分完成
    UNREVIEWED = "unreviewed"  # 检查完了，但证据不足以让程序下结论
    CONFIRMED = "confirmed"    # 人工已经拍过板（覆盖算法结论）
    LOW_RISK = "low_risk"      # 抽样覆盖足够、没找到网格 —— 自动归入，不作待办
    DEFERRED = "deferred"      # 文件名有明确无码/破解线索，按名称延后

    @property
    def label(self) -> str:
        return BUCKET_LABELS[self]

    @property
    def is_todo(self) -> bool:
        """是否算"待我处理"。低风险与已延后刻意不算。"""
        return self in (ReviewBucket.PENDING, ReviewBucket.FAILED,
                        ReviewBucket.UNREVIEWED)


BUCKET_LABELS: dict[ReviewBucket, str] = {
    ReviewBucket.PENDING: "排队中",
    ReviewBucket.FAILED: "未完成",
    ReviewBucket.UNREVIEWED: "待复核",
    ReviewBucket.CONFIRMED: "已确认",
    ReviewBucket.LOW_RISK: "低风险",
    ReviewBucket.DEFERRED: "已延后",
}


class WorkflowView(str, Enum):
    """顶部视图。**阶段一（定标记）与阶段二（找替换）是两件事，不混在一起算。**

    阶段一的目标只有一个：把"哪些片确实有码"这件事定下来（人工确认）。
    阶段二才对**已确认有码**的片去网上找无码版 —— 拿算法猜的"疑似有码"去查，
    等于用假设喂流程，白查还可能把你的判断带偏。
    """

    TODO = "todo"        # 阶段一 · 待我处理：排队中 + 未完成 + 待复核
    ALL = "all"          # 阶段一 · 全部记录
    DONE = "done"        # 阶段一 · 已确认（有码 + 无码都在这里，用结论列区分）
    REPLACE = "replace"  # 阶段二 · 找替换：只含"已确认有码"的片
    PARKED = "parked"    # 阶段一 · 暂不处理：低风险 + 已延后

    @property
    def label(self) -> str:
        return VIEW_LABELS[self]

    @property
    def stage(self) -> int:
        """1 = 定标记，2 = 找替换。给界面分组用。"""
        return 2 if self is WorkflowView.REPLACE else 1


VIEW_LABELS: dict[WorkflowView, str] = {
    WorkflowView.TODO: "① 待我处理",
    WorkflowView.ALL: "① 全部记录",
    WorkflowView.DONE: "① 已确认",
    WorkflowView.REPLACE: "② 找替换",
    WorkflowView.PARKED: "① 暂不处理",
}

#: 结论维度里"已经定性"的三个状态（用于「结论」筛选）
CONCLUDING_STATES = (CensorState.CENSORED, CensorState.UNCENSORED, CensorState.UNKNOWN)


def bucket(result) -> ReviewBucket:
    """一个检测结果落在哪个处理桶。**唯一判定入口。**"""
    if result is None:
        return ReviewBucket.PENDING
    if result.verdict.of(EvidenceSource.MANUAL) is not None:
        return ReviewBucket.CONFIRMED
    if result.status is not ProcessingStatus.COMPLETED:
        return ReviewBucket.FAILED
    visuals = [e for e in result.verdict.evidence if e.source is EvidenceSource.VISUAL]
    if result.state is not CensorState.UNKNOWN:
        return ReviewBucket.UNREVIEWED
    if (visuals and not result.needs_review
            and all(e.state is CensorState.UNKNOWN and e.review_required is False
                    for e in visuals)):
        return ReviewBucket.LOW_RISK
    if any(candidate.score > 0 for candidate in result.candidates):
        return ReviewBucket.UNREVIEWED
    uncensored, censored = classify_tag(Path(result.path).stem)
    if uncensored and not censored:
        return ReviewBucket.DEFERRED
    return ReviewBucket.UNREVIEWED


#: 旧接口的字符串值（历史代码/用例还在用，保留兼容）
_CATEGORY_ALIASES = {
    ReviewBucket.UNREVIEWED: "review",
    ReviewBucket.CONFIRMED: "manual",
    ReviewBucket.LOW_RISK: "low_risk",
    ReviewBucket.DEFERRED: "deferred",
    ReviewBucket.FAILED: "failed",
    ReviewBucket.PENDING: "pending",
}


def category(result) -> str:
    """兼容层：老代码按字符串判断，值保持不变（review / manual / ...）。"""
    return _CATEGORY_ALIASES[bucket(result)]


def needs_review(result) -> bool:
    """是否需要人工过一眼。**这就是"待我处理"的定义。**"""
    return bucket(result).is_todo


def conclusion_label(result) -> str:
    """结论维度的显示文本：有码 / 无码 / 未定。"""
    return {
        CensorState.CENSORED: "有码",
        CensorState.UNCENSORED: "无码",
        CensorState.UNKNOWN: "未定",
    }[result.state]


def bucket_label(result) -> str:
    return bucket(result).label


def awaits_replacement(result) -> bool:
    """这部片该不该进入阶段二（找替换）。

    **只有人工确认过、且结论是「有码」的片子**才需要去找无码版：
    算法判的"疑似有码"可能根本不是有码，拿它去查没有意义。
    """
    if result is None:
        return False
    if bucket(result) is not ReviewBucket.CONFIRMED:
        return False
    return result.state is CensorState.CENSORED


def in_view(view: WorkflowView, value: ReviewBucket) -> bool:
    """某个处理桶是否属于某个视图。

    ``REPLACEABLE`` 需要额外条件（无码版检索结果为确有/疑似），
    由界面叠加 —— 这里是纯函数，不认识网络。
    """
    if view is WorkflowView.ALL:
        return True
    if view is WorkflowView.TODO:
        return value.is_todo
    if view is WorkflowView.DONE:
        return value is ReviewBucket.CONFIRMED
    if view is WorkflowView.PARKED:
        return value in (ReviewBucket.LOW_RISK, ReviewBucket.DEFERRED)
    if view is WorkflowView.REPLACE:
        # 阶段二只装"已确认有码"的片，由界面用结论+处理两个维度判定
        return value is ReviewBucket.CONFIRMED
    return True
