"""跑外部命令行程序，并把它的输出**按行实时回传** —— 两个第三方工具集成共用。

为什么单独抽出来
----------------
批量字幕与马赛克破解都要驱动一个控制台程序，而这类程序有两个共同点：

1. **跑得久**（几分钟到几十分钟），所以必须能边跑边把输出喂给界面，
   否则用户只看到界面卡住；
2. **会以奇怪的退出码结束**（例如 Windows 的 ``0xC0000409``），
   所以需要把退出码翻译成人能看懂的话，而不是只报一个数字。

另外这里的实现刻意**不经过 shell**（不用 ``shell=True``）：参数里的路径可能带空格、
中文或引号，拼字符串再交给 shell 是一类经典的坑。用列表形式传参由系统自己处理转义。

``psutil`` 是**可选**依赖：装了就能把子进程绑到指定核心 / 调优先级，
没装就跳过（不让一个锦上添花的能力变成硬依赖）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

__all__ = ["run_streaming", "creation_flags", "apply_process_prefs", "psutil_available"]

_NO_WINDOW = 0x08000000
_HIGH_PRIORITY = 0x00000080
_BELOW_NORMAL_PRIORITY = 0x00004000
_NEW_PROCESS_GROUP = 0x00000200


def psutil_available() -> bool:
    """``psutil`` 是否可用（用于绑核 / 调优先级这类可选优化）。"""
    try:
        import psutil  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def creation_flags(*, new_group: bool = False, high_priority: bool = False,
                   low_priority: bool = False) -> int:
    """Windows 上需要的 ``creationflags``（其它平台返回 0）。

    - ``CREATE_NO_WINDOW``：这些 exe 都是控制台程序，不屏蔽就会**弹出黑窗口**；
    - ``CREATE_NEW_PROCESS_GROUP``：停止时要能整组结束，避免 ffmpeg 之类的
      子进程残留（残留进程会一直占着显存，是最难查的一类"工具没反应"）；
    - 优先级：破解跑很久时，别把机器拖到没法用。
    """
    if os.name != "nt":
        return 0
    flags = _NO_WINDOW
    if new_group:
        flags |= _NEW_PROCESS_GROUP
    if high_priority:
        flags |= _HIGH_PRIORITY
    elif low_priority:
        flags |= _BELOW_NORMAL_PRIORITY
    return flags


def apply_process_prefs(proc, *, cpu_affinity=None, keep_awake: bool = False) -> bool:
    """给已启动的进程绑核 / 防休眠。返回是否真的应用成功。

    ``cpu_affinity`` 为空或 ``psutil`` 未安装时静默跳过 —— 这类优化失败
    不该影响主流程。
    """
    if not cpu_affinity:
        return False
    try:
        import psutil

        p = psutil.Process(proc.pid)
        p.cpu_affinity(list(cpu_affinity))
        if keep_awake and hasattr(p, "nice"):
            try:
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
            except Exception:  # noqa: BLE001
                pass
        return True
    except Exception:  # noqa: BLE001
        return False


def run_streaming(cmd,
                  *,
                  on_line: Optional[Callable[[str], None]] = None,
                  should_stop: Optional[Callable[[], bool]] = None,
                  timeout: int = 0,
                  cwd=None,
                  extra_env: Optional[dict] = None,
                  cpu_affinity=None,
                  high_priority: bool = False) -> dict:
    """同步跑完 ``cmd``，边跑边把每一行输出交给 ``on_line``。

    返回 ``{"code", "stopped", "timed_out", "error", "lines"}``。

    ``should_stop`` 每收到一行输出就查一次；返回 True 时**结束整个进程组**
    （Windows 上这些工具内部还会拉起 ffmpeg，只 kill 主进程会留下占显存的孤儿）。

    超时用 ``timeout`` 秒控制（0 = 不限）。超时同样走"结束进程组"这条路。
    """
    on_line = on_line or (lambda _s: None)
    if not cmd:
        return {"code": None, "stopped": False, "timed_out": False,
                "error": "命令为空", "lines": []}

    env = dict(os.environ)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    # 让被调用的程序也用 UTF-8 说话，否则中文日志在管道里会变成乱码。
    #
    # `utf-8:ignore`（带 errors 部分）是照着**用户原来那份可用脚本**取的：
    # 它用 `PYTHONIOENCODING="utf-8:ignore"`，即使编码对不上也不会抛异常。
    # 注意：对 PyInstaller 冻结的 exe（如 infer.exe）这两个变量可能**根本不被读取**
    # —— 那种情况下靠的是控制台代码页（用户的脚本还配了 `chcp 65001` +
    # `CREATE_NEW_CONSOLE`）。我们改用管道捕获日志，所以对这类工具另有
    # `utils.whisper_tool.patch_encoding()` 兜底。
    env.setdefault("PYTHONIOENCODING", "utf-8:ignore")
    env.setdefault("PYTHONUTF8", "1")

    flags = creation_flags(new_group=True, high_priority=high_priority)
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(cwd) if cwd else None,
            env=env, creationflags=flags,
        )
    except (OSError, ValueError) as e:
        return {"code": None, "stopped": False, "timed_out": False,
                "error": f"无法启动程序：{e}", "lines": []}

    apply_process_prefs(proc, cpu_affinity=cpu_affinity)

    lines: list = []
    stopped = False
    timed_out = False
    deadline = (time.monotonic() + timeout) if timeout else None

    # 单独一个线程读 stdout：主线程负责看停止信号与超时。
    # 直接 for line in proc.stdout 会在"程序很久不输出"时卡住，
    # 那时用户点停止是没反应的。
    def _reader():
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.rstrip("\r\n")
                lines.append(line)
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001  界面回调出错不该拖垮采集
                    pass
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_reader, name="proc-reader", daemon=True)
    reader.start()

    while True:
        if proc.poll() is not None:
            break
        if should_stop and should_stop():
            stopped = True
            _terminate(proc)
            break
        if deadline and time.monotonic() > deadline:
            timed_out = True
            _terminate(proc)
            break
        time.sleep(0.15)

    reader.join(timeout=5)
    try:
        proc.stdout.close()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    code = proc.returncode
    error = ""
    if stopped:
        error = "用户已停止"
    elif timed_out:
        error = f"超时（超过 {timeout} 秒）"
    return {"code": code, "stopped": stopped, "timed_out": timed_out,
            "error": error, "lines": lines}


def _terminate(proc) -> None:
    """结束进程**整组**（先温和后强硬），并等它真的退出。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # 结束整棵进程树：这些工具内部会拉起 ffmpeg，只杀主进程会留孤儿
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_NO_WINDOW, timeout=20)
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def python_command(code: str) -> list:
    """构造一个"用当前解释器执行一段代码"的命令（自检/演示用）。"""
    return [sys.executable, "-c", code]
