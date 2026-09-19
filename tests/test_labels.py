"""labels 打标存储单元测试。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.labels import LabelStore  # noqa: E402
from utils.scoring import LABEL_DISLIKE, LABLE_KEEP  # noqa: E402


def test_set_get_and_override(tmp_path):
    store = LabelStore(tmp_path / "smart.db")
    store.set("a", LABLE_KEEP)
    assert store.get("a") == LABLE_KEEP
    store.set("a", LABEL_DISLIKE)  # 覆盖
    assert store.get("a") == LABEL_DISLIKE
    assert store.get("nope") is None


def test_all_summary_clear(tmp_path):
    store = LabelStore(tmp_path / "s.db")
    store.set("a", LABLE_KEEP)
    store.set("b", LABLE_KEEP)
    store.set("c", LABEL_DISLIKE)
    assert store.count() == 3
    assert store.all() == {"a": LABLE_KEEP, "b": LABLE_KEEP, "c": LABEL_DISLIKE}
    s = store.summary()
    assert s[LABLE_KEEP] == 2 and s[LABEL_DISLIKE] == 1
    store.remove("c")
    assert store.count() == 2
    store.clear()
    assert store.count() == 0


def test_invalid_label_raises(tmp_path):
    store = LabelStore(tmp_path / "s2.db")
    try:
        store.set("a", "BOGUS")
        assert False, "应报错"
    except ValueError:
        pass
