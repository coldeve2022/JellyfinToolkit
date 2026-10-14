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
def _restore_data_root(tmp_path):
    """Isolate persisted UI state and runtime redirection for every test."""
    import config

    original = config.BASE_DIR
    config.set_data_root(tmp_path / "runtime", rebind_modules=True)
    try:
        yield
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


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """会话收尾时，趁 QApplication 还在，把残留的 Qt 对象先有序释放掉。

    为什么必须做：本套件里很多页面/窗口是**不带父部件**单独构造的
    （``DeletePage(cfg)``、``MainWindow()``…），它们彼此成环、又被若干闭包
    握着，因此不会被引用计数立即回收，只能等到**解释器退出阶段**才由
    PySide 析构。那时 QApplication 已经先被销毁，PySide6 6.12 起会直接触发
    访问违例（Linux/macOS 段错误、Windows 异常退出）；即便侥幸不崩，
    收尾期 GC 仍会在 Python 3.11 上撞到 ``bool_dealloc`` 引用计数断言。
    两者的共同表现都是「所有用例通过、进程却以非零状态码结束」，
    把 CI 的发布门禁卡红 —— 而且失败原因和被测代码毫无关系。

    真实程序的退出路径不受影响：页面挂在 ``MainWindow`` 下，随窗口一起
    有序析构（已验证）。所以这里只需要在测试收尾补一次有序清理。

    放在 hook 而不是 fixture 里：部分测试文件自带 module 级 ``qapp``，
    session 级 fixture 去依赖它会触发 pytest 的 ScopeMismatch。
    """
    import gc
    import sys

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    # 1) 顶层部件：先真的销毁，别留给解释器收尾阶段
    if app is not None:
        for widget in list(app.topLevelWidgets()):
            try:
                widget.deleteLater()
            except RuntimeError:  # C++ 侧已先行销毁
                continue
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

    # 2) 模块级 Qt 单例（TaskManager 还挂着运行中的 QTimer）
    services = sys.modules.get("ui.services")
    if services is not None:
        manager = getattr(services.TaskManager, "_instance", None)
        services.TaskManager._instance = None
        if manager is not None:
            try:
                manager._timer.stop()
            except RuntimeError:
                pass
            manager.deleteLater()
        if app is not None:
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()

    # 3) 在**运行时**（而不是解释器 finalizing 阶段）把循环垃圾收干净。
    #    PySide6 在 Py3.11 的退出期 GC 上会踩到 bool 引用计数断言，
    #    提前收集可以把这一步挪出收尾路径。
    for _ in range(3):
        gc.collect()
        if app is not None:
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
