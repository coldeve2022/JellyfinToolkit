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

# 诊断用：C 层崩溃（Qt 析构、访问违例）不会抛 Python 异常，只会让进程静默退出，
# 表现为"所有用例都通过、却没有汇总行、退出码非零"。faulthandler 能把栈打出来。
import faulthandler  # noqa: E402

faulthandler.enable()

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试期间强制离屏渲染，避免在无显示环境（CI）里弹窗或崩溃
import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 测试期间禁止任何联网检索：无码版本检索走的是真实站点，
# 一旦某个用例触发它，CI 会卡在 20 秒超时上，且结果依赖外网。
# 需要验证网络逻辑的用例请自行 monkeypatch.delenv 后再跑（见 test_release_transport）。
os.environ.setdefault("JELLYFIN_TOOLKIT_OFFLINE", "1")
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


#: 哪些用例留下了后台任务（session 收尾时汇总打印）
_LEFTOVER_TASKS: list[str] = []


@pytest.fixture(autouse=True)
def _drain_background_tasks(request):
    """用例之间不留后台任务。

    `TaskManager` 是**进程级单例**，而 `start_task` 判断"能不能开始"用的是全局
    状态（有写任务在跑就拒绝）。上一个用例没停干净的 worker 会让下一个用例的
    启动被静默拒绝，表现为"某两个用例单独跑都过、全量跑必挂一个且每次挂的
    还不是同一个"——最典型的就是
    `test_gui_smoke::test_censorship_page_classifies` 与
    `test_input_workflows_followup::test_folder_picker_…[censorship-…]` 交替失败。

    这里做两件事：
    1. 每个用例开始前把残留任务收掉（顺带记录是谁留下的，收尾时汇总）；
    2. 实在收不掉就直接清空注册表 —— 测试宁可丢一个僵尸条目，
       也不要让污染传递给下一个用例。
    """
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        yield
        return

    import time

    from PySide6.QtCore import QObject
    from shiboken6 import isValid

    from ui.services import TaskManager

    manager = TaskManager.instance()
    manager._poll()
    if manager._active:
        _LEFTOVER_TASKS.append(f"{request.node.nodeid}：开始前残留 {len(manager._active)} 个")
        try:
            manager.stop_all()
        except Exception:  # noqa: BLE001 - 清理失败不该把这个用例判成失败
            pass
        # 关键：必须**真的等线程退出**再放手。
        # 早先这里直接 `manager._active.clear()`，等于把还在跑的 QThread 从注册表
        # 摘掉 —— 没有引用管理之后它们会随解释器收尾一起析构，触发
        # "QThread destroyed while running"，进程以非零码结束、且连 pytest 的
        # 汇总行都打不出来（表现为"全部用例都通过，CI 却红"）。
        for entry in list(manager._active.values()):
            worker = entry["worker"]
            stop = getattr(worker, "stop", None)
            if callable(stop):
                try:
                    stop()
                except (RuntimeError, TypeError):
                    pass
            if isinstance(worker, QObject) and isValid(worker):
                try:
                    worker.wait(5000)
                except (RuntimeError, TypeError):
                    pass
        deadline = time.monotonic() + 3.0
        while manager._active and time.monotonic() < deadline:
            manager._poll()
            QApplication.processEvents()
            time.sleep(0.02)
        if manager._active:
            # 极少数连 wait 都收不掉的，才强制摘除 —— 且必须记录，别静默
            _LEFTOVER_TASKS.append(
                f"{request.node.nodeid}：wait 后仍有 {len(manager._active)} 个，强制清空"
            )
            manager._active.clear()
    yield


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

    def _mark(text):
        """阶段标记：崩溃时用它定位死在哪一步（stderr 不会被 pytest 捕获）。"""
        print(f"[sessionfinish] {text}", file=sys.stderr, flush=True)

    _mark("start")

    if _LEFTOVER_TASKS:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        lines = ["", "⚠️ 以下用例留下了未结束的后台任务（已在本轮被强制收掉）："]
        lines += [f"   - {item}" for item in _LEFTOVER_TASKS]
        if reporter is not None:
            reporter.write_line("\n".join(lines))
        else:
            print("\n".join(lines))

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    _mark(f"app={app is not None}")

    # 1) 顶层部件：先真的销毁，别留给解释器收尾阶段
    if app is not None:
        widgets = list(app.topLevelWidgets())
        _mark(f"顶层部件 {len(widgets)} 个")
        for widget in widgets:
            try:
                widget.deleteLater()
            except RuntimeError:  # C++ 侧已先行销毁
                continue
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        _mark("顶层部件已清理")

    # 2) 模块级 Qt 单例（TaskManager 还挂着运行中的 QTimer）
    services = sys.modules.get("ui.services")
    _mark("开始清理 TaskManager")
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
    _mark("TaskManager 已清理")

    # 3) 在**运行时**（而不是解释器 finalizing 阶段）把循环垃圾收干净。
    #    PySide6 在 Py3.11 的退出期 GC 上会踩到 bool 引用计数断言，
    #    提前收集可以把这一步挪出收尾路径。
    for index in range(3):
        gc.collect()
        _mark(f"gc 第 {index + 1} 轮完成")
        if app is not None:
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
    _mark("finish")
