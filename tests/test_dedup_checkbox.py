"""DedupPage 组头"全选"勾选框 ↔ 子项联动 单元测试（需 Qt，offscreen 运行）。

覆盖修复：用户反馈"每个视频有一个全选标志，点了没反应"。
根因：① 只设了组头 ItemIsUserCheckable 却没实现联动；
     ② QTreeWidgetItem 默认 flags 就含 ItemIsUserCheckable（实测），
        导致全 CD 保护组的组头也带勾选框 → 可能误删分卷。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _wait(ms=50):
    """用真实事件循环让 QTimer.singleShot(0) 触发（itemChanged 的延迟处理）。"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _mk(iid, num, path, size_mb=500, unc=False, cd=False, cd_idx=None):
    from utils.library import Item
    return Item(id=iid, name=path.split("/")[-1], path=path, type_cn="Movie", year=2021,
                size=size_mb * 1024 * 1024, num=num, is_cd=cd, cd_index=cd_idx,
                is_uncensored=unc, genres=["剧情"], tags=["标签"], studios=["厂"], actors=["演员"])


def _make_page(tmp_path, qapp, losers=None):
    import config
    # 必须用 set_data_root：直接写 config.DATA_DIR 不会同步到那些
    # `from config import DATA_DIR` 的模块（导入期绑定），
    # 而且会破坏对象身份，让后续 set_data_root 再也重绑不上。
    config.set_data_root(tmp_path)

    from ui.pages.dedup import DedupPage
    from ui.widgets import LogPanel

    page = DedupPage.__new__(DedupPage)
    page._in_check_sync = False
    if losers is None:
        losers = {"c"}
    page.groups = [
        {"num": "MIAA-743", "all_cd": False,
         "items": [_mk("c", "MIAA-743", "D:/c.mp4", 500),
                   _mk("u", "MIAA-743", "D:/u.mp4", 2000, unc=True)],
         "losers": set(losers)},
        {"num": "FC2-123", "all_cd": True,
         "items": [_mk("c1", "FC2-123", "D:/cd1.mp4", 1000, cd=True, cd_idx=1),
                   _mk("c2", "FC2-123", "D:/cd2.mp4", 1000, cd=True, cd_idx=2)],
         "losers": set()},
    ]
    from PySide6.QtWidgets import QTreeWidget
    page.tree = QTreeWidget()
    page.tree.setHeaderLabels(["版本文件", "辅助标签", "大小", "路径"])
    page.log = LogPanel()
    page._item2id = {}
    page.tree.itemChanged.connect(page._on_item_check_changed)  # 等同 _setup_ui 里连接
    page._render_tree()
    return page


def test_real_duplicate_group_head_checkable(qapp, tmp_path):
    """真重复组头可勾选（全选）。"""
    page = _make_page(tmp_path, qapp)
    g0 = page.tree.topLevelItem(0)
    assert g0.flags() & Qt.ItemIsUserCheckable


def test_all_cd_group_head_not_checkable(qapp, tmp_path):
    """全 CD 保护组头不可勾选（已移除默认 flag），避免误删分卷。"""
    page = _make_page(tmp_path, qapp)
    g1 = page.tree.topLevelItem(1)
    assert not (g1.flags() & Qt.ItemIsUserCheckable)


def test_group_head_check_selects_all_children(qapp, tmp_path):
    """点组头全选 → 组内所有子项都勾选。"""
    page = _make_page(tmp_path, qapp)
    g0 = page.tree.topLevelItem(0)
    g0.setCheckState(0, Qt.Checked)
    _wait()
    assert [g0.child(i).checkState(0) for i in range(2)] == [Qt.Checked, Qt.Checked]


def test_group_head_uncheck_deselects_all(qapp, tmp_path):
    """点组头取消 → 组内所有子项恢复不勾。"""
    page = _make_page(tmp_path, qapp)
    g0 = page.tree.topLevelItem(0)
    g0.setCheckState(0, Qt.Checked); _wait()
    g0.setCheckState(0, Qt.Unchecked); _wait()
    assert [g0.child(i).checkState(0) for i in range(2)] == [Qt.Unchecked, Qt.Unchecked]


def test_partial_child_updates_head_tristate(qapp, tmp_path):
    """只勾一个子项 → 组头进入三态半选（PartiallyChecked）。

    用无默认勾选的组（losers=set），确保勾选第一个子项时确实发生状态变化并触发信号。
    """
    page = _make_page(tmp_path, qapp, losers=set())
    g0 = page.tree.topLevelItem(0)
    assert g0.child(0).checkState(0) == Qt.Unchecked   # 初始不勾
    g0.child(0).setCheckState(0, Qt.Checked); _wait()
    assert g0.checkState(0) == Qt.PartiallyChecked


def test_all_children_checked_updates_head_checked(qapp, tmp_path):
    """子项全勾 → 组头变勾选。"""
    page = _make_page(tmp_path, qapp)
    g0 = page.tree.topLevelItem(0)
    g0.child(0).setCheckState(0, Qt.Checked); _wait()
    g0.child(1).setCheckState(0, Qt.Checked); _wait()
    assert g0.checkState(0) == Qt.Checked


def test_cd_child_check_does_not_make_head_checkable(qapp, tmp_path):
    """全 CD 组子项勾选 → 组头仍不可勾选（保护不被破坏）。"""
    page = _make_page(tmp_path, qapp)
    g1 = page.tree.topLevelItem(1)
    g1.child(0).setCheckState(0, Qt.Checked); _wait()
    assert not (g1.flags() & Qt.ItemIsUserCheckable)
