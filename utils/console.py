"""控制台输出编码兼容层 —— 只依赖标准库，可被任何入口脚本导入。

**为什么需要它**：脚本往控制台打印中文，在有些环境下会直接崩掉。

- GitHub 的 Windows runner 上 ``sys.stdout.encoding`` 是 **cp1252**，
  打印任何中文都会
  ``UnicodeEncodeError: 'charmap' codec can't encode characters`` ——
  实测就是这么把 release workflow 的构建步骤打死的（构建脚本第一步就挂）。
- 中文 Windows 上是 cp936（GBK）：能打印，但**管道/重定向**出去就变成 GBK 字节，
  贴到 GitHub 上整段乱码。

**做法**：只在**非交互**（管道 / 重定向）时把 stdout/stderr 切到 UTF-8；
直接跑在控制台里时保持系统编码 —— 否则中文控制台自己的显示反而会花屏。

配套建议：构建脚本派生**子进程**时额外给它 ``PYTHONIOENCODING=utf-8``，
这样即使子脚本自己忘了调用本函数也不会踩坑（双保险）。
"""

from __future__ import annotations

import sys

__all__ = ["force_utf8_stdout"]

_UTF8_ALIASES = {"utf8", "utf_8", "u8", "cp65001"}


def _is_utf8(stream) -> bool:
    enc = (getattr(stream, "encoding", "") or "").lower()
    return enc.replace("-", "_") in _UTF8_ALIASES


def force_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8（仅在输出被管道/重定向时）。

    幂等：重复调用没有副作用；stream 为 None（GUI 版 exe 没有控制台）时安全跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if getattr(stream, "isatty", lambda: False)():
                continue          # 交互式控制台：保持系统编码，否则中文会花屏
            if _is_utf8(stream):
                continue          # 已经是 UTF-8，不用重复设置
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 不支持 reconfigure 的流（老版本 / 被替换过的对象）直接忽略
            continue
