"""ReviewPage 卡片队列锚点定位逻辑单元测试。

背景：打标(keep/delete)会触发后台重排(_rebuild_deck)。旧实现重排后强制 pos=0，
导致"打标后回到开头 / 之前跳过的又冒出来"。本测试固化「锚点定位」行为：
keep/delete 后应定位到当前这张的下一张，而不是跳回开头。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_page():
    """构造一个仅含队列逻辑所需属性的 ReviewPage（绕过 __init__ 的 UI/DB 依赖）。"""
    import ui.pages.review as R
    page = R.ReviewPage.__new__(R.ReviewPage)
    page.results = []
    page.labels = {}
    page.deck_ids = []
    page.pos = 0
    page._label_anchor = None
    page._order_mode = "score"   # 命中度升序（默认）
    page._skipped = set()        # 跳过(搁置)集合
    # 屏蔽真正的卡片渲染(需 Qt+card_lay)，只记录被定位到的索引，专注队列/锚点逻辑
    page._shown = []
    def _fake_show():
        if page.deck_ids:
            idx = min(page.pos, len(page.deck_ids) - 1)
            page._shown.append(idx)
    page._show_current = _fake_show
    return page


def _mk_result(iid, score):
    from utils.library import Item
    from utils import scoring as S
    item = Item(id=iid, name=f"作品{iid}", path="", type_cn="Movie", year=2020,
                genres=["x"], actors=["a"])
    obj = type("FakeResult", (), {})()
    obj.item = item
    obj.decision = S.DEC_REVIEW
    obj.s_content = score
    obj.reason = "r"
    obj.featureless = False
    obj.is_seen = False
    return obj


def test_rebuild_deck_sorts_by_score_start_zero():
    """空锚点时队列按命中度升序，从头开始。"""
    page = _make_page()
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    page._rebuild_deck()
    assert page.deck_ids == ["a", "b", "c"]
    assert page.pos == 0


def test_rebuild_deck_anchors_to_target():
    """传入 anchor_id 时定位到它，而非回开头。"""
    page = _make_page()
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    page._rebuild_deck(anchor_id="c")
    assert page.deck_ids == ["a", "b", "c"]
    assert page.pos == 2  # 定位到 c


def test_label_keeps_anchor_after_deck_rebuild():
    """打标(keep)后重排应定位到当前这张的下一张(anchor)，不打回开头。

    场景：队列 [a(0.1) b(0.5) c(0.9)]，skip a → 展示 b；keep b → b 打标移除，
    anchor 设为 c；重排后队列 [a, c]，应定位到 c（pos=1），而非回 a（pos=0）。
    """
    page = _make_page()
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    page._rebuild_deck()
    assert page.pos == 0
    # skip a（不写标签，仅前进）
    page.pos += 1
    assert page.deck_ids[page.pos] == "b"
    # keep b → 设 anchor=c，b 打标从队列移除
    page._label_anchor = "c"
    page.labels["b"] = "KEEP"
    remaining = [r for r in page.results if r.item.id not in page.labels]
    page.results = remaining
    page._rebuild_deck(anchor_id=page._label_anchor)
    assert page.deck_ids == ["a", "c"]
    assert page.pos == 1  # 定位到 c，未回开头 a


def test_anchor_missing_falls_back_to_zero():
    """anchor_id 不在重新构建的队列里(如已被其它打标移除) → 回退到开头。"""
    page = _make_page()
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5)]
    page._rebuild_deck(anchor_id="z")  # z 不存在
    assert page.pos == 0


def test_random_deck_is_permutation():
    """随机模式：队列是原待复核集合的一个排列，元素不重复、不遗漏。"""
    import random
    page = _make_page()
    page._order_mode = "random"
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    # 固定种子，避免测试不稳定
    random.seed(42)
    page._rebuild_deck()
    assert set(page.deck_ids) == {"a", "b", "c"}
    assert page._shown and page._shown[0] == 0


def test_skipped_excluded_from_deck():
    """跳过的作品不再出现在队列里。"""
    page = _make_page()
    page._skipped = {"b"}
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    page._rebuild_deck()
    assert page.deck_ids == ["a", "c"]  # b 被搁置，不出现


def test_restore_skipped_clears_marker():
    """恢复跳过的：从队列里去掉 skip 标记后，作品重新可入队。"""
    page = _make_page()
    page._skipped = {"b"}
    page.results = [_mk_result("a", 0.1), _mk_result("b", 0.5), _mk_result("c", 0.9)]
    page._rebuild_deck()
    assert page.deck_ids == ["a", "c"]
    # 恢复
    page._skipped.clear()
    page._rebuild_deck()
    assert page.deck_ids == ["a", "b", "c"]
