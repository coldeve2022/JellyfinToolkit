"""标签同义合并 - 单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, Item  # noqa: E402
from utils.scoring import _feat_values  # noqa: E402
from utils.tag_norm import (  # noqa: E402
    canon_tag,
    filter_content_tags,
    is_non_content_tag,
    norm_key,
    prioritize_tags,
)


def _mk(genres=(), tags=()):
    return Item(id="x", name="x", path=r"D:\x.mp4", type_cn="Movie", year=2022,
                genres=list(genres), tags=list(tags), studios=[], actors=[],
                size=2_000_000_000, bucket=BUCKET_JAV)


def test_canon_tag_mappings():
    assert canon_tag("中出し") == "中出"
    assert canon_tag("潮吹き") == "潮吹"
    assert canon_tag("素人娘") == "素人"
    assert canon_tag("ハメ撮り") == "はめ撮り"
    assert canon_tag("個人撮影") == "个人摄影"
    assert canon_tag("美少女系") == "美少女"
    assert canon_tag("無碼") == "无码"
    # 日汉变体
    assert canon_tag("顔射") == "颜射"
    assert canon_tag("巨乳") == "巨乳"     # 未命中保持不变


def test_norm_key_equivalence():
    assert norm_key("ハメ撮り") == norm_key("はめ撮り")
    assert norm_key("顔射") == norm_key("颜射")
    assert norm_key("無碼") == norm_key("无码")


def test_feat_values_merge_synonyms():
    it = _mk(genres=["中出し", "中出"], tags=["顔射", "颜射", "巨乳"])
    feats = dict(_feat_values(it))
    assert feats["genre"] == ["中出"]          # 合并为一个
    assert feats["tag"] == ["颜射", "巨乳"]     # 颜射归一去重


def test_non_content_tag_filter():
    assert is_non_content_tag("4K")
    assert is_non_content_tag("中文字幕")
    assert is_non_content_tag("经典老片")
    assert not is_non_content_tag("中出")
    assert not is_non_content_tag("制服")


def test_filter_content_tags_dedup():
    assert filter_content_tags(["中出", "4K", "字幕", "制服", "制服"]) == ["中出", "制服"]


def test_prioritize_tags_puts_content_first():
    ordered = prioritize_tags(["字幕", "制服", "中出"])
    assert ordered[0] in {"制服", "中出"}
    assert "字幕" not in ordered
