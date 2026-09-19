"""dedup_groups 排除 Jellyfin 附属文件的单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import Item
from utils import scoring as S


def _mk(iid, num, name, path, size=None, is_cd=False, bucket="jav", unc=False):
    return Item(id=iid, name=name, path=path, type_cn="Movie", year=2021,
                genres=["剧情"], tags=["标签"], studios=["厂"], actors=["演员"],
                size=size, num=num, is_cd=is_cd, cd_index=1 if is_cd else None,
                bucket=bucket, is_uncensored=unc)


def test_dedup_excludes_junk_attachment():
    """theme/fanart/backdrop/trailer/极小文件 不参与去重分组。"""
    items = [
        # 真重复：同一番号两个正片版本
        _mk("v1", "MIAA-743", "MIAA-743 高清", "/lib/MIAA-743.mp4", size=2_000_000_000),
        _mk("v2", "MIAA-743", "MIAA-743 标清", "/lib/MIAA-743_480.mp4", size=900_000_000),
        # Jellyfin 附属文件：theme / 片头
        _mk("t1", "MIAA-743", "theme", "/lib/theme.mp4", size=20_000_000),
        _mk("t2", "MIAA-743", "fanart", "/lib/fanart.jpg", size=5_000_000),
    ]
    groups = S.dedup_groups(items)
    assert len(groups) == 1
    g = groups[0]
    ids = {it.id for it in g["items"]}
    assert ids == {"v1", "v2"}
    assert "t1" not in ids and "t2" not in ids


def test_dedup_keeps_cd_all_protected():
    """全 CD 分卷组：保护，losers 为空。"""
    items = [
        _mk("c1", "FC2-123", "FC2-123-CD1", "/lib/cd1.mp4", size=1_000_000_000, is_cd=True),
        _mk("c2", "FC2-123", "FC2-123-CD2", "/lib/cd2.mp4", size=1_000_000_000, is_cd=True),
    ]
    groups = S.dedup_groups(items)
    assert len(groups) == 1
    assert groups[0]["all_cd"] is True
    assert groups[0]["losers"] == set()


def test_dedup_marks_loser():
    """真重复：标记较差的为 loser（有码/小/老）。"""
    items = [
        _mk("u", "MIAA-777", "MIAA-777 无码", "/lib/u.mp4", size=3_000_000_000, unc=True),
        _mk("c", "MIAA-777", "MIAA-777 有码", "/lib/c.mp4", size=1_000_000_000),
    ]
    groups = S.dedup_groups(items)
    assert len(groups) == 1
    assert groups[0]["losers"] == {"c"}
