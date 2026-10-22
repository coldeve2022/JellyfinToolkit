"""Jellyfin Toolkit — 无窗口启动入口。

此文件用 .pyw 后缀，Windows 关联 pythonw.exe 运行，不弹出控制台窗口。

**为什么要包一层异常处理**
------------------------
.pyw 没有控制台，一旦启动阶段就失败（解释器缺 PySide6、DLL 加载失败、
依赖缺失），Windows 只会弹一个 ``Application Error 0xc0000142`` 之类的框，
甚至什么都不弹 —— 你根本不知道原因。

所以这里：
1. 把真实异常写进 ``data/startup-error.log``（直接把这个文件发我即可）；
2. 用系统 MessageBox 把关键信息弹出来（无控制台也能看见）；
3. 明确提示"该用哪个解释器" —— 这条本身就是最常见的原因。
"""

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _log_path():
    folder = os.path.join(_HERE, "data")
    try:
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "startup-error.log")
    except OSError:
        return os.path.join(_HERE, "startup-error.log")


def _report(title, text, detail=""):
    """无控制台环境下也要让人看见错误：写日志 + 弹窗。"""
    path = _log_path()
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"\n===== {title} =====\n{detail or text}\n")
    except OSError:
        path = "(日志写入失败)"
    body = (
        f"{text}\n\n"
        f"详细信息：{path}\n"
        f"当前解释器：{sys.executable}\n\n"
        f"{detail}"
    ).strip()
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, body[:2000], f"Jellyfin Toolkit — {title}", 0x10)
    except Exception:  # noqa: BLE001 - 弹窗失败就只能靠日志
        pass


def main():
    try:
        import PySide6  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _report(
            "缺少 PySide6",
            "当前 Python 没有安装 PySide6，程序无法启动。\n\n"
            "正确做法：双击项目里的「启动.bat」，它会用项目自带的 .venv 运行。",
            traceback.format_exc() or str(exc),
        )
        return 1
    try:
        import runpy

        runpy.run_path(os.path.join(_HERE, "main.py"), run_name="__main__")
    except SystemExit:
        pass
    except BaseException:  # noqa: BLE001 - 启动期任何错误都要让人看见
        _report("启动失败", "程序在启动阶段出错了，把这段内容发给我即可定位。",
                traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
