"""状态模型的用例 —— 结论（有没有码）与处理（还需不需要人）必须正交。

这组用例守的是"不遗漏、不重复"的底层契约：
- 每个结果都能落到唯一一个处理桶里；
- 「待我处理」= 排队中 + 未完成 + 待复核，低风险与已延后**不算**；
- 视图与结论两个筛选维度是独立的 AND，不再有"两个菜单项其实一样"这种事。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from censor import CensorState, VideoVerdict  # noqa: E402
from censor.fusion import fuse  # noqa: E402
from censor.models import (  # noqa: E402
    Evidence, EvidenceSource, FrameCandidate, ProcessingStatus,
)
from censor.review_route import (  # noqa: E402
    ReviewBucket, WorkflowView, bucket, conclusion_label, in_view, needs_review,
)


def _result(state=CensorState.UNKNOWN, *, manual=False, status=ProcessingStatus.COMPLETED,
            candidates=(), review_required=None, path=r"C:\lib\ABC-123.mp4"):
    result = VideoVerdict(path=path, status=status)
    evidence = [Evidence(EvidenceSource.VISUAL, state, 0.9, "画面",
                         review_required=review_required)]
    if manual:
        evidence.append(Evidence(EvidenceSource.MANUAL, state, 1.0, "人工"))
    result.verdict = fuse(evidence)
    result.candidates = list(candidates)
    return result


# ── 处理桶 ─────────────────────────────────────────────────

def test_manual_evidence_wins_over_everything():
    result = _result(CensorState.CENSORED, manual=True, status=ProcessingStatus.FAILED)
    assert bucket(result) is ReviewBucket.CONFIRMED
    assert not needs_review(result)


def test_incomplete_scan_is_failed_not_a_conclusion():
    result = _result(CensorState.CENSORED, status=ProcessingStatus.FAILED)
    assert bucket(result) is ReviewBucket.FAILED
    assert needs_review(result)


def test_auto_conclusion_only_goes_positive():
    """算法只能定「有码」，**不能**定「无码」——没检出网格 ≠ 证明整片没有。

    这条是体系里最重要的一个取舍：所以「结论=无码」只可能来自人工确认，
    自动结果最多到"未定 + 低风险"。界面把两列分开，正是为了让这件事看得出来。
    """
    censored = _result(CensorState.CENSORED)
    assert conclusion_label(censored) == "有码"
    assert bucket(censored) is ReviewBucket.UNREVIEWED

    negative = VideoVerdict(path=r"C:\lib\D-4.mp4")
    negative.verdict = fuse([Evidence(EvidenceSource.VISUAL, CensorState.UNCENSORED,
                                      0.9, "未见马赛克网格")])
    assert negative.state is CensorState.UNKNOWN, "画面没检出也不能自动判无码"
    assert conclusion_label(negative) == "未定"
    assert bucket(negative) is ReviewBucket.UNREVIEWED


def test_uncensored_conclusion_requires_manual_confirmation():
    result = _result(CensorState.UNCENSORED, manual=True)
    assert conclusion_label(result) == "无码"
    assert bucket(result) is ReviewBucket.CONFIRMED


def test_low_risk_is_not_a_todo():
    result = _result(CensorState.UNKNOWN, review_required=False)
    result.verdict.needs_review = False
    assert bucket(result) is ReviewBucket.LOW_RISK
    assert not needs_review(result), "抽样低风险刻意不进待办"
    assert conclusion_label(result) == "未定", "低风险不是结论，仍是未定"


def test_deferred_name_clue_is_not_a_todo():
    """没有画面证据、但文件名明说是无码/流出 → 延后，不占待办。"""
    result = VideoVerdict(path=r"C:\lib\ABC-123 无码流出.mp4")
    result.verdict = fuse([])
    assert bucket(result) is ReviewBucket.DEFERRED
    assert not needs_review(result)
    assert conclusion_label(result) == "未定", "延后不是结论"


def test_candidate_pixels_push_unknown_into_review():
    candidate = FrameCandidate(timestamp=10.0, score=8.0)
    result = _result(CensorState.UNKNOWN, candidates=[candidate])
    assert bucket(result) is ReviewBucket.UNREVIEWED


def test_pending_means_imported_but_never_scanned():
    assert bucket(None) is ReviewBucket.PENDING
    assert ReviewBucket.PENDING.is_todo


# ── 视图 ───────────────────────────────────────────────────

def test_todo_view_is_exactly_the_work_left():
    todo = [b for b in ReviewBucket if in_view(WorkflowView.TODO, b)]
    assert set(todo) == {ReviewBucket.PENDING, ReviewBucket.FAILED, ReviewBucket.UNREVIEWED}


def test_parked_view_holds_low_risk_and_deferred():
    parked = [b for b in ReviewBucket if in_view(WorkflowView.PARKED, b)]
    assert set(parked) == {ReviewBucket.LOW_RISK, ReviewBucket.DEFERRED}


def test_every_bucket_belongs_to_some_view():
    """不允许出现"哪个视图都进不去"的桶 —— 那就是会遗失的结果。"""
    for value in ReviewBucket:
        views = [v for v in WorkflowView if in_view(v, value)]
        assert views, value


def test_all_view_contains_everything():
    assert all(in_view(WorkflowView.ALL, b) for b in ReviewBucket)


# ── 界面接线 ───────────────────────────────────────────────

@pytest.fixture()
def cfg():
    from config import ToolkitConfig

    return ToolkitConfig.load()


@pytest.fixture
def page(qapp, cfg):
    from ui.pages.censorship import CensorshipPage

    cfg.release_auto_lookup = False
    widget = CensorshipPage(cfg)
    yield widget
    widget.shutdown()


def test_columns_split_conclusion_and_process(page):
    from ui.pages.censorship import _COLUMNS

    assert _COLUMNS[2] == "结论" and _COLUMNS[3] == "处理"
    assert page.table.horizontalHeaderItem(2).text() == "结论"
    assert page.table.horizontalHeaderItem(3).text() == "处理"


def test_view_and_conclusion_filters_are_independent(page):
    page._on_item(_result(CensorState.CENSORED, path=r"C:\lib\A-1.mp4"))
    page._on_item(_result(CensorState.UNCENSORED, path=r"C:\lib\B-2.mp4"))
    page._on_item(_result(CensorState.UNKNOWN, review_required=False,
                          path=r"C:\lib\C-3.mp4"))
    page._results[r"C:\lib\C-3.mp4"].verdict.needs_review = False

    def visible():
        return {page.table.item(r, 0).data(0) for r in range(page.table.rowCount())
                if not page.table.isRowHidden(r)}

    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.ALL.value))
    assert visible() == {"A-1.mp4", "B-2.mp4", "C-3.mp4"}

    page.combo_conclusion.setCurrentIndex(1)          # 有码
    assert visible() == {"A-1.mp4"}

    page.combo_conclusion.setCurrentIndex(0)          # 全部结论
    page.combo_filter.setCurrentIndex(page.combo_filter.findData(WorkflowView.TODO.value))
    assert visible() == {"A-1.mp4", "B-2.mp4"}, "低风险不该出现在待办里"


def test_view_labels_carry_counts(page):
    page._on_item(_result(CensorState.CENSORED, path=r"C:\lib\A-1.mp4"))
    page._refresh_stats()
    texts = [page.combo_filter.itemText(i) for i in range(page.combo_filter.count())]
    assert any(text.endswith("待我处理 (1)") for text in texts), texts


def test_manual_label_moves_row_to_confirmed(page, monkeypatch):
    page._on_item(_result(CensorState.CENSORED, path=r"C:\lib\A-1.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()
    monkeypatch.setattr(page._service, "label",
                        lambda path, state, note="", base=None: _result(
                            state, manual=True, path=path))
    page._label(CensorState.UNCENSORED)
    assert page.table.item(0, 2).text() == "无码"
    assert page.table.item(0, 3).text() == "已确认"


def test_import_skips_files_with_valid_cache(page, monkeypatch, tmp_path, qtbot):
    """重复导入同一批文件：有有效结果的不重算，只把要算的送进队列。"""
    from censor import CensorOptions

    cached = _result(CensorState.CENSORED, path=str(tmp_path / "done.mp4"))
    fresh = str(tmp_path / "todo.mp4")

    def fake_cached(path, options=None, **_kwargs):
        assert isinstance(options, CensorOptions)
        return cached if str(path).endswith("done.mp4") else None

    monkeypatch.setattr(page._service, "cached_result", fake_cached)
    started: list[list[str]] = []
    monkeypatch.setattr(page, "_set_running", lambda running: None)

    class _Signal:
        def connect(self, *_args, **_kwargs):
            return None

    class FakeWorker:
        item_done = discovered = task_status = error = all_done = _Signal()

        def __init__(self, service, paths, options, **kwargs):
            started.append(list(paths))

        def isRunning(self):  # noqa: N802 - Qt 约定
            return False

        def start(self):
            return None

    monkeypatch.setattr("ui.pages.censorship.CensorScanWorker", FakeWorker)
    monkeypatch.setattr("ui.pages.censorship.start_task",
                        lambda parent, worker, title: worker.start())

    page._start_scan([str(tmp_path / "done.mp4"), fresh])
    qtbot.waitUntil(lambda: bool(started))
    qtbot.waitUntil(lambda: cached.path in page._results)
    assert page._results, "有缓存的那条应通过有界队列呈现，不重算"
    assert started and started[0] == [fresh], "只该把没缓存的送进扫描队列"


def test_import_all_cached_does_not_start_a_scan(page, monkeypatch, tmp_path, qtbot):
    cached = _result(CensorState.CENSORED, path=str(tmp_path / "done.mp4"))
    monkeypatch.setattr(page._service, "cached_result",
                        lambda path, options=None, **_: cached)
    started: list = []
    monkeypatch.setattr("ui.pages.censorship.CensorScanWorker",
                        lambda *a, **k: started.append(a))
    page._start_scan([str(tmp_path / "done.mp4")])
    qtbot.waitUntil(lambda: '沿用上次结果' in page._stats_label.text())
    assert not started, "全部命中缓存时不该起扫描线程"
    assert "沿用上次结果" in page._stats_label.text()


def test_restore_pending_loads_history_and_switches_view(page, monkeypatch):
    """关掉程序再打开，待处理项要自动接上 —— 这是"遗漏"最实际的来源。"""
    pending = [_result(CensorState.UNKNOWN, path=rf"C:\lib\old{i}.mp4") for i in range(3)]
    monkeypatch.setattr(page._service, "review_queue", lambda limit=500: pending)
    page._restore_pending()
    assert page.table.rowCount() == 3
    assert page._current_view() is WorkflowView.TODO
    assert "已自动载入 3 个待处理项" in page._stats_label.text()


def test_restore_does_not_duplicate_existing_rows(page, monkeypatch):
    page._on_item(_result(CensorState.UNKNOWN, path=r"C:\lib\old0.mp4"))
    pending = [_result(CensorState.UNKNOWN, path=r"C:\lib\old0.mp4")]
    monkeypatch.setattr(page._service, "review_queue", lambda limit=500: pending)
    page._restore_pending()
    assert page.table.rowCount() == 1, "已经在列表里的不该再加一行"


# ── 两个阶段：定标记 → 找替换 ─────────────────────────────

def test_awaits_replacement_only_for_confirmed_censored():
    """检索只服务于"已确认有码"。算法猜的、已确认无码的都不进。"""
    from censor.review_route import awaits_replacement

    assert awaits_replacement(_result(CensorState.CENSORED, manual=True))
    assert not awaits_replacement(_result(CensorState.UNCENSORED, manual=True))
    assert not awaits_replacement(_result(CensorState.CENSORED)), "算法判的不算确认"
    assert not awaits_replacement(_result(CensorState.UNKNOWN))
    assert not awaits_replacement(None)


def test_confirming_censored_enqueues_number(page, monkeypatch):
    """标记「有码」= 进阶段二清单：这里只入队，不联网。"""
    queued: list[list[str]] = []
    monkeypatch.setattr(page._release_service, "queue",
                        lambda numbers, **kwargs: queued.append(list(numbers)))
    page._on_item(_result(CensorState.UNKNOWN, path=r"C:/lib/A-123.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()

    confirmed = _result(CensorState.CENSORED, manual=True, path=r"C:/lib/A-123.mp4")
    confirmed.number = "A-123"
    monkeypatch.setattr(page._service, "label",
                        lambda path, state, note="", base=None: confirmed)
    page._label(CensorState.CENSORED)
    assert queued and queued[-1] == ["A123"], queued
    assert "找无码版" in page._stats_label.text()


def test_confirming_uncensored_never_enqueues(page, monkeypatch):
    queued: list[list[str]] = []
    monkeypatch.setattr(page._release_service, "queue",
                        lambda numbers, **kwargs: queued.append(list(numbers)))
    page._on_item(_result(CensorState.UNKNOWN, path=r"C:/lib/B-456.mp4"))
    page.table.selectRow(0)
    page._on_row_selected()

    confirmed = _result(CensorState.UNCENSORED, manual=True, path=r"C:/lib/B-456.mp4")
    confirmed.number = "B-456"
    monkeypatch.setattr(page._service, "label",
                        lambda path, state, note="", base=None: confirmed)
    page._label(CensorState.UNCENSORED)
    assert queued == []


def test_lookup_is_not_on_the_checking_page_anymore(page):
    """检查页不再显示检索结果 —— 那是「找无码版」页的事。"""
    from ui.pages.censorship import _COLUMNS

    assert "无码版" not in _COLUMNS
    assert not hasattr(page, "release_panel")
    assert not hasattr(page, "_start_lookup")


def test_stats_point_at_the_replace_page(page):
    page._on_item(_result(CensorState.CENSORED, manual=True, path=r"C:/lib/A-1.mp4"))
    page._refresh_stats()
    text = page._stats_label.text()
    assert "已确认有码 1" in text, text
    assert "找无码版" in text, text
