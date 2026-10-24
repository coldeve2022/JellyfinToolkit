"""Manual review label defaults and conservative saved-config migration."""
import json
from pathlib import Path

import pytest

import config


KEYS = ("censor_sync_tag_censored", "censor_sync_tag_uncensored")


def pair(value):
    return tuple(value[key] for key in KEYS)


def test_default_manual_review_tags_match_example():
    assert pair(config.DEFAULT_CONFIG) == ("有码", "无码")
    assert tuple(getattr(config.ToolkitConfig(), key) for key in KEYS) == ("有码", "无码")
    example = json.loads((Path(config.__file__).parent / "config.example.json").read_text(encoding="utf-8"))
    assert pair(example) == ("有码", "无码")


def test_load_migrates_exact_old_pair_without_writing_saved_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "saved-config.json")
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    saved = json.dumps(dict(zip(KEYS, ("像素化:有", "像素化:未见"))), ensure_ascii=False)
    config.CONFIG_FILE.write_text(saved, encoding="utf-8")
    loaded = config.ToolkitConfig.load()
    assert tuple(getattr(loaded, key) for key in KEYS) == ("有码", "无码")
    assert config.CONFIG_FILE.read_text(encoding="utf-8") == saved


@pytest.mark.parametrize("saved", [
    ("custom-positive", "custom-negative"),
    ("像素化:有", "custom-negative"),
    ("custom-positive", "像素化:未见"),
    ("像素化:有", "无码"),
])
def test_custom_or_mixed_pairs_are_preserved(saved):
    assert pair(config._coerce_config(dict(zip(KEYS, saved)))) == saved


def test_tag_normalization_and_duplicate_validation_still_apply():
    assert pair(config._coerce_config(dict(zip(KEYS, (" custom ", " other "))))) == ("custom", "other")
    assert pair(config._coerce_config(dict(zip(KEYS, ("Same", "same"))))) == ("有码", "无码")
    assert pair(config._coerce_config(dict(zip(KEYS, ("", "x\ny"))))) == ("有码", "无码")
