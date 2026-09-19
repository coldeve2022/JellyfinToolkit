"""utils.library 纯函数单元测试 — 番号提取 / CD 分卷 / 分桶。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import (  # noqa: E402
    BUCKET_HENTAI, BUCKET_JAV, BUCKET_OTHER, BUCKET_TRAILER, BUCKET_UNC,
    Item, classify_bucket, detect_cd, extract_number, is_bare_code_name, summarize,
)


def test_extract_number_regular():
    assert extract_number("MIAA-743 新人.mp4") == "MIAA-743"
    assert extract_number("REAL-641_x.mp4") == "REAL-641"
    assert extract_number("ABCDE12345.mp4") == "ABCDE-12345"
    assert extract_number("MOVIE.mkv") is None


def test_extract_number_fc2():
    assert extract_number("FC2-3581881-cd4.mp4") == "FC2-3581881"
    assert extract_number("fc2_1234567.mp4") == "FC2-1234567"


def test_detect_cd():
    idx, is_cd = detect_cd("FC2-3581881-cd4.mp4")
    assert is_cd and idx == 4
    assert detect_cd("MIAA-743.mp4") == (None, False)
    # 不以边界命中的 'CD'（如番号 SCD-xxx）不应误判
    assert detect_cd("SCD-123.mp4") == (None, False)


def test_classify_bucket():
    assert classify_bucket("REAL-641", "", "REAL-641", "x.Movie")[0] == BUCKET_JAV
    assert classify_bucket("FC2-3581881", "", "FC2-3581881", "x.Movie")[0] == BUCKET_UNC
    b, unc = classify_bucket("[opiumud] Yoshiwara rose", "", None, "x.Movie")
    assert b == BUCKET_HENTAI and not unc
    assert classify_bucket("trailer", "", None, "x.Trailer")[0] == BUCKET_TRAILER
    assert classify_bucket("weird name", "", None, "x.Movie")[0] == BUCKET_OTHER


def _mk(**kw):
    base = dict(id="x", name="N", path="P", type_cn="Movie")
    base.update(kw)
    return Item(**base)


def test_summarize_counters():
    items = [
        _mk(id="1", num="A-1", bucket=BUCKET_JAV, is_favorite=True, is_played=True,
            year=2022, actors=["甲"], play_count=3),
        _mk(id="2", num="A-1", bucket=BUCKET_JAV, is_favorite=False, is_cd=True,
            year=2022, actors=["乙"], play_count=0),
        _mk(id="3", num="B-2", bucket=BUCKET_UNC, is_favorite=False,
            year=2019, actors=["甲"], play_count=0),
    ]
    s = summarize(items)
    assert s["total"] == 3
    assert s["favorite"] == 1
    assert s["played"] == 1
    # 同番号 A-1 出现两次且含 CD → 记为 CD 组
    assert s["dup_groups"] == 1
    assert s["dup_cd_groups"] == 1
    assert s["dup_true"] == 0
    # 未看（待预测池，≠删除）：id2 与 id3 从未播放
    assert s["unwatched"] == 2
    # 未看且老：id3(2019) 命中；id2(2022) 也命中(≤2022)
    assert s["unwatched_old"] == 2


def test_is_bare_code_name():
    assert is_bare_code_name("FC2-3581881") is True
    assert is_bare_code_name("FC2-3581881 cd1 cd2") is True
    assert is_bare_code_name("FC2-PPV-3581881") is True
    assert is_bare_code_name("MIAA-743-cd1") is True
    assert is_bare_code_name("MIAA-743 新人") is False
    assert is_bare_code_name("") is True
    assert is_bare_code_name("某电影标题") is False
