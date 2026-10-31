"""未完成的分析也要入库，但不得顶掉已完成的结论。

背景（用户报障的代码级根因）：`probe` 以前只在 ``status is COMPLETED`` 时落库，
只要有一个检测器抛异常（没装 numpy、ffmpeg 路径失效、单帧解码失败），
整批结果就变成 ``PARTIAL`` → 一行都不写库 → ``stats().total`` 为 0，
而界面上照样列出 226 行。用户看到的是"列表 226 条 / 已保存 0 条"，
重启之后那 226 行还会整批消失（``review_queue`` 只从库里取）。

修法两条一起才成立：
1. ``probe`` 按"要不要缓存"落库，与完成度无关；
2. ``_write_result`` 拒绝用未完成的结果覆盖已完成的结论。
"""

from __future__ import annotations

from censor.models import CensorState, ProcessingStatus, VideoVerdict
from censor.store import VerdictStore


def _verdict(path="sample", *, status=ProcessingStatus.COMPLETED, state=CensorState.CENSORED):
    result = VideoVerdict(path=path, file_identity="identity-1",
                          analysis_signature="sig-1", size=10, mtime=1.0, duration=60.0)
    result.status = status
    result.verdict.state = state
    result.verdict.confidence = 0.9
    result.error = "" if status is ProcessingStatus.COMPLETED else "visual.mosaic_cv: boom"
    return result


def test_partial_result_is_persisted_and_counted(tmp_path):
    """部分完成必须落库 —— 否则界面有行、库里没行，重启就整批消失。"""
    store = VerdictStore(tmp_path / "results.db")
    store.put(_verdict(status=ProcessingStatus.PARTIAL), persist=True)

    assert store.get("sample") is not None, "部分完成的结果应当能读回来"
    stats = store.stats()
    assert stats.total == 1
    assert stats.incomplete == 1


def test_partial_does_not_overwrite_completed(tmp_path):
    """一条好结论不能被后续失败的分析冲掉。"""
    store = VerdictStore(tmp_path / "results.db")
    store.put(_verdict(), persist=True)
    store.put(_verdict(status=ProcessingStatus.PARTIAL, state=CensorState.UNKNOWN), persist=True)

    cached = store.get("sample", file_identity="identity-1", analysis_signature="sig-1")
    assert cached is not None
    assert cached.status is ProcessingStatus.COMPLETED
    assert cached.verdict.state is CensorState.CENSORED
    assert store.stats().incomplete == 0


def test_partial_with_new_signature_is_a_cache_miss(tmp_path):
    """换了一套分析参数后失败 → 不能拿旧结论冒充新结论。"""
    store = VerdictStore(tmp_path / "results.db")
    store.put(_verdict(), persist=True)

    fresh = _verdict(status=ProcessingStatus.PARTIAL)
    fresh.analysis_signature = "sig-2"
    store.put(fresh, persist=True)

    assert store.get("sample", analysis_signature="sig-2") is None
    # 旧签名的结论还在（未被覆盖），仍然可以命中
    assert store.get("sample", analysis_signature="sig-1") is not None


def test_completed_overwrites_partial(tmp_path):
    """反过来必须允许：跑成功的一次要盖掉之前的失败记录。"""
    store = VerdictStore(tmp_path / "results.db")
    store.put(_verdict(status=ProcessingStatus.PARTIAL), persist=True)
    assert store.stats().incomplete == 1

    store.put(_verdict(), persist=True)
    stats = store.stats()
    assert stats.total == 1
    assert stats.incomplete == 0
    assert store.get("sample", analysis_signature="sig-1").status is ProcessingStatus.COMPLETED
