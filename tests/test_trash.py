"""trash 日志与 dedup 分组 单元测试（回收站删除依赖真实 Windows shell，不在单测覆盖）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.library import BUCKET_JAV, Item  # noqa: E402
from utils.scoring import dedup_groups  # noqa: E402
from utils.trash import DeleteJournal  # noqa: E402


def _mk(id_, num, unc=False, size=100, year=2022, cd=False, cd_idx=None):
    # Item.size 语义为字节（真实库 DB 的 Size 字段）；此处把 MB 转成字节，
    # 避免被 is_junk_item 的"<10MB 视为占位文件"规则误判为附属文件。
    return Item(id=id_, name=f"{num}-{id_}", path=f"D:\\{num}-{id_}.mp4",
                type_cn="Movie", year=year, size=size * 1024 * 1024, num=num, is_cd=cd,
                cd_index=cd_idx, bucket=BUCKET_JAV, is_uncensored=unc)


def test_journal_roundtrip(tmp_path):
    j = DeleteJournal(tmp_path / "j.db")
    j.record(r"D:\a.mp4", 100, "test")
    j.record(r"D:\b.mp4", 200, "test2")
    assert j.count() == 2
    assert j.total_size() == 300
    rows = j.all()
    assert rows[0]["path"] == r"D:\b.mp4"  # 最新在前
    assert rows[1]["reason"] == "test"


def test_dedup_groups_loser_and_cd_protect():
    coded = _mk("c", "MIAA-743", size=500)
    unc = _mk("u", "MIAA-743", unc=True, size=2000)
    g = dedup_groups([coded, unc])[0]
    assert g["num"] == "MIAA-743"
    assert not g["all_cd"]
    assert g["losers"] == {coded.id}   # 有码差版本

    c1 = _mk("c1", "FC2-123", cd=True, cd_idx=1)
    c2 = _mk("c2", "FC2-123", cd=True, cd_idx=2)
    g2 = dedup_groups([c1, c2])[0]
    assert g2["all_cd"]
    assert g2["losers"] == set()       # CD 分卷全保护


def test_dedup_groups_single_ignored():
    assert dedup_groups([_mk("a", "A-1")]) == []
