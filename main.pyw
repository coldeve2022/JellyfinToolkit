"""Jellyfin Toolkit — 无窗口启动入口。

此文件用 .pyw 后缀，Windows 关联 pythonw.exe 运行，不弹出控制台窗口。
内容与 main.py 等价（双击本文件即可启动）。
"""
import os
import runpy
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

runpy.run_path(os.path.join(_here, "main.py"), run_name="__main__")
