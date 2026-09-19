"""验证打包产物真能跑起来 —— 进程「活着」不等于没崩。

窗口版 exe 没有控制台，构造页面时的异常不会打印到任何地方，
只会让主窗口压根不显示，而进程还活着好几秒。所以这里做三件事：

1. 用 ``DETACHED_PROCESS`` 起 exe（否则宿主会在命令结束时回收子进程树，
   看起来像"自己死了"）；
2. 每秒采样进程是否仍在，连续存活 N 秒视为没崩；
3. **读应用自己的错误日志** ``<数据目录>/app-error.log`` —— 这是唯一
   能暴露"窗口没显示出来"的东西。

用法：
    python tools/dev/smoke_exe.py                       # 用 dist/ 下最新产物
    python tools/dev/smoke_exe.py --exe path/to/x.exe
    python tools/dev/smoke_exe.py --seconds 8
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

STILL_ACTIVE = 259


def _find_exe() -> Path | None:
    candidates = sorted((ROOT / "dist").glob("*/JellyfinToolkit.exe"))
    return candidates[-1] if candidates else None


def _still_running(proc: subprocess.Popen) -> bool:
    if proc.poll() is not None:
        return False
    if sys.platform != "win32":
        return True
    import ctypes

    code = ctypes.c_ulong()
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, proc.pid)  # QUERY_LIMITED
    if not handle:
        return proc.poll() is None
    try:
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包产物启动冒烟测试")
    parser.add_argument("--exe", default="")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--data-dir", default="",
                        help="自定义数据目录（默认用临时目录，避免污染真实配置）")
    args = parser.parse_args()

    exe = Path(args.exe).expanduser() if args.exe else _find_exe()
    if not exe or not exe.exists():
        print("[跳过] 没找到打包产物（先跑 scripts/build_release.py）")
        return 0

    data_dir = Path(args.data_dir) if args.data_dir else Path(
        tempfile.mkdtemp(prefix="jellyfin-smoke-"))

    env = dict(os.environ)
    env["JELLYFIN_TOOLKIT_DATA_DIR"] = str(data_dir)
    env.pop("CODEBUDDY_SESSION_ID", None)
    env.pop("CLAUDE_SESSION_ID", None)

    log = data_dir / "app-error.log"
    if log.exists():
        log.unlink()

    print(f"启动: {exe}")
    print(f"数据目录: {data_dir}")
    proc = subprocess.Popen([str(exe)], env=env, creationflags=DETACHED_FLAGS)

    alive_checks = 0
    total = max(3, int(args.seconds))
    for _ in range(total):
        time.sleep(1)
        if _still_running(proc):
            alive_checks += 1
        else:
            break

    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass

    ok = True
    print(f"存活采样: {alive_checks}/{total} 秒")
    if alive_checks < min(3, total):
        print("[失败] 进程过早退出 —— 大概率是构造期异常（窗口根本没显示）。")
        ok = False

    if log.exists() and log.stat().st_size:
        print("\n[失败] 应用错误日志非空，说明有未捕获异常：")
        print(log.read_text(encoding="utf-8", errors="replace")[:4000])
        ok = False
    else:
        print("错误日志为空 ✅")

    if ok:
        print("\n打包产物启动冒烟通过 ✅")
        return 0
    print("\n打包产物冒烟未通过 ❌")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
