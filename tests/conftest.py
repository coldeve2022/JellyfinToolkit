"""pytest 全局配置。

两个职责：
1. 让项目根目录可导入（main.py 与各包同级）；
2. **会话级**把配置/数据目录重定向到临时目录 —— 否则
   ``ToolkitConfig.save()`` / ``LabelStore()`` / ``DeleteJournal()``
   会往项目目录写 config.json、data/*.db，跑一次测试就在仓库里留下运行时产物。

注意：很多模块写的是 ``from config import DATA_DIR``（**导入期绑定**），
只改 ``config.DATA_DIR`` 对它们无效。``config.set_data_root()`` 会遍历
``sys.modules`` 把仍指向旧对象的同名属性一起换掉。
"""

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试期间强制离屏渲染，避免在无显示环境（CI）里弹窗或崩溃
import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    # 离屏模式下 Qt 找不到系统字体，中文会渲染成方块 —— 显式指到系统字体目录
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


@pytest.fixture(scope="session", autouse=True)
def _isolate_data_dir():
    """把数据目录挪到临时目录，测试绝不写仓库。"""
    import config

    tmp = Path(tempfile.mkdtemp(prefix="jellyfin-toolkit-tests-"))
    # 必须用 set_data_root 还原，不能直接给 config 的三个属性赋值 ——
    # 那样会把对象身份换掉，之后 set_data_root 的按身份重绑就再也匹配不上，
    # 其他模块会一直指向临时目录。
    original = config.BASE_DIR
    config.set_data_root(tmp, rebind_modules=True)
    try:
        yield tmp
    finally:
        config.set_data_root(original, rebind_modules=True)


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """每个用例前清掉 ffmpeg 能力探测缓存，避免用例间互相影响。"""
    from utils import tools

    tools.clear_probe_cache()
    yield


@pytest.fixture(scope="session")
def qapp():
    """离屏模式下的 QApplication 单例。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
