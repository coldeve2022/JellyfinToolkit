"""DeckState 持久化测试：排序模式 + 跳过(搁置)集合。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.deck_state import DeckState, ORDER_RANDOM, ORDER_SCORE


def _tmp_db(tmp_path):
    return DeckState(tmp_path / "smart.db")


def test_default_order_is_score(tmp_path):
    ds = _tmp_db(tmp_path)
    assert ds.get_order() == ORDER_SCORE


def test_set_and_persist_order(tmp_path):
    ds = _tmp_db(tmp_path)
    ds.set_order(ORDER_RANDOM)
    # 重新实例化（模拟下次打开）仍读取到随机
    ds2 = DeckState(str(tmp_path / "smart.db"))
    assert ds2.get_order() == ORDER_RANDOM


def test_invalid_order_falls_back_to_score(tmp_path):
    ds = _tmp_db(tmp_path)
    ds.set_order("bogus")
    assert ds.get_order() == ORDER_SCORE


def test_skipped_add_and_remove(tmp_path):
    ds = _tmp_db(tmp_path)
    ds.add_skipped("a")
    ds.add_skipped("b")
    ds.add_skipped("a")  # 重复添加无害
    assert ds.skipped() == {"a", "b"}
    ds.remove_skipped("a")
    assert ds.skipped() == {"b"}


def test_skipped_persist_across_reopen(tmp_path):
    ds = _tmp_db(tmp_path)
    ds.add_skipped("x")
    ds2 = DeckState(str(tmp_path / "smart.db"))
    assert ds2.skipped() == {"x"}


def test_clear_skipped(tmp_path):
    ds = _tmp_db(tmp_path)
    ds.add_skipped("a")
    ds.add_skipped("b")
    ds.clear_skipped()
    assert ds.skipped() == set()
