"""在本机查找第三方可执行文件（``infer.exe`` / ``lada-cli.exe``）。

为什么需要它
------------
用户把工具解压到哪儿是自由的，之前只按**固定的几个目录名**（``lada``、
``faster-whisper``…）去猜，实测必然落空：真实目录常带版本号和平台后缀，
比如 ``…/faster_whisper_transwithai_windows_cu122-chickenrice``、
``…/lada-v0.11.0_windows_nvidia`` —— 一个都猜不中，于是"自动检测"
只会弹一个"没找到"的提示框，用户看到的就是"点了没反应"。

所以改成**按关键词在盘上的浅层目录里找**：

1. 显式配置的路径（支持填 exe 本身，或它所在的目录）；
2. 环境变量 / 调用方给的额外目录；
3. 每个盘根下**一级子目录**里直接找（快，一次目录列举 + 若干次 stat）；
4. 名字里**含关键词**的目录里找（一层、两层都看）——这一条才覆盖上面那种真实命名；
5. 名字像"工具目录"的（tools/apps/软件/工具…）再往里看一层。

全程受 ``time_budget`` 约束并跳过系统目录，返回**搜索过程本身**（扫了多少目录、
花了多久、有没有提前收工），界面可以如实告诉用户，而不是只给一个"没找到"。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterable, Optional

__all__ = ["find_executable", "drives", "exe_in_dir", "CONTAINER_HINTS", "SKIP_DIR_NAMES"]

#: 明显不该进去的目录（系统目录 + 回收站 + 依赖目录），避免把时间浪费掉
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", "recovery", "perflogs",
    "msocache", "node_modules", "__pycache__", ".git", ".venv", "venv",
    "site-packages", "_internal", "dist", "build", "temp", "tmp",
}

#: 名字像"放工具的地方"，值得再往里看一层
CONTAINER_HINTS = (
    "tool", "app", "soft", "program", "portable", "green", "bin", "dev",
    "project", "work", "download", "解压", "工具", "软件", "程序", "便携",
)


def drives() -> list:
    """本机所有存在的盘根。非 Windows 给出常见挂载点。"""
    if os.name != "nt":
        return [Path("/opt"), Path("/usr/local"), Path.home() / "Apps"]
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if root.is_dir():
                out.append(root)
        except OSError:
            continue
    return out


def exe_in_dir(directory, exe_names: Iterable[str], max_depth: int = 1) -> Optional[Path]:
    """在 ``directory`` 里找指定的可执行文件（默认也看一层子目录）。

    解压出来多套一层目录是最常见的情况（``lada-v0.11.0_windows_nvidia\\`` 里
    往往还有一层），所以默认就多找一层，而不是要求用户必须填到那一层。
    """
    if not directory:
        return None
    base = Path(directory)
    if base.is_file():
        return base
    if not base.is_dir():
        return None
    names = [n.lower() for n in exe_names]

    def _look(d: Path) -> Optional[Path]:
        try:
            entries = list(d.iterdir())
        except OSError:
            return None
        for entry in entries:
            if entry.is_file() and entry.name.lower() in names:
                return entry
        return None

    hit = _look(base)
    if hit or max_depth <= 0:
        return hit
    try:
        children = [c for c in base.iterdir()
                    if c.is_dir() and c.name.lower() not in SKIP_DIR_NAMES]
    except OSError:
        return None
    for child in children:
        hit = _look(child)
        if hit:
            return hit
    return None


def _keyword_hit(name: str, keywords: Iterable[str]) -> bool:
    low = name.lower()
    return any(k and k.lower() in low for k in keywords)


def _container_like(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in CONTAINER_HINTS)


def find_executable(exe_names, keywords, *, configured: str = "",
                    extra_dirs: Optional[list] = None, time_budget: float = 12.0,
                    log=None) -> dict:
    """查找可执行文件，返回**结果 + 搜索过程**。

    返回值：``{"path", "source", "scanned", "elapsed", "stopped_early"}``

    ``configured`` 最优先；找不到时**不抛异常**，把过程数据交给界面去解释。
    """
    started = time.monotonic()
    stats = {"path": None, "source": "", "scanned": 0, "elapsed": 0.0,
             "stopped_early": False}

    def note(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:  # noqa: BLE001  日志回调不该影响搜索
                pass

    def expire() -> bool:
        if time_budget and (time.monotonic() - started) > time_budget:
            stats["stopped_early"] = True
            return True
        return False

    # 1) 用户显式配置的（可以是 exe，也可以是它所在目录）
    if configured:
        p = Path(str(configured).strip().strip('"'))
        hit = exe_in_dir(p, exe_names)
        if hit:
            stats.update(path=hit, source="设置里指定的位置",
                         elapsed=time.monotonic() - started)
            return stats

    # 2) 调用方给的额外目录 / 环境变量
    for d in (extra_dirs or []):
        hit = exe_in_dir(d, exe_names)
        if hit:
            stats.update(path=hit, source="环境变量或调用方指定",
                         elapsed=time.monotonic() - started)
            return stats

    names = tuple(exe_names)
    for root in drives():
        if expire():
            break
        # 3) 盘根下的一级子目录里直接找（最常见：解压到某个盘的某个文件夹）
        try:
            level1 = [c for c in root.iterdir()
                      if c.is_dir() and c.name.lower() not in SKIP_DIR_NAMES]
        except OSError:
            continue
        stats["scanned"] += 1

        for d1 in level1:
            if expire():
                break
            # 3a) 名字含关键词 → 这里最可能（lada-0.11.0 / faster_whisper_...）
            if _keyword_hit(d1.name, keywords):
                hit = exe_in_dir(d1, names, max_depth=2)
                if hit:
                    stats.update(path=hit, source=f"按名称找到：{d1}",
                                 elapsed=time.monotonic() - started)
                    return stats
                continue
            hit = exe_in_dir(d1, names, max_depth=0)
            if hit:
                stats.update(path=hit, source=f"按名称找到：{d1}",
                             elapsed=time.monotonic() - started)
                return stats

            # 4) 名字像"工具目录"的（tools / 软件 / 工具…），再往里看一层
            if not _container_like(d1.name):
                continue
            try:
                level2 = [c for c in d1.iterdir()
                          if c.is_dir() and c.name.lower() not in SKIP_DIR_NAMES]
            except OSError:
                continue
            stats["scanned"] += 1
            for d2 in level2:
                if expire():
                    break
                # 既然已经进到"工具目录"里了，就逐个子目录看过再走
                hit = exe_in_dir(d2, names, max_depth=1)
                if hit:
                    stats.update(path=hit, source=f"按名称找到：{d2}",
                                 elapsed=time.monotonic() - started)
                    return stats

    stats["elapsed"] = time.monotonic() - started
    note(f"搜索完成：扫过 {stats['scanned']} 个目录，用时 {stats['elapsed']:.1f} 秒"
         + ("（到时间上限提前收工）" if stats["stopped_early"] else ""))
    return stats
