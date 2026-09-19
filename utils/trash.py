"""安全删除 - 移入回收站(可恢复)，绝不物理删除；删除日志留痕。

Windows 使用 SHFileOperationW(FO_DELETE | FOF_ALLOWUNDO) 实现回收站删除，
无第三方依赖；失败时返回错误信息，由上层决定保留原文件。
"""
from __future__ import annotations

import ctypes
import os
import sqlite3
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# ── 回收站删除 ─────────────────────────────
_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def trash_file(path: str) -> tuple[bool, str]:
    """将单个文件/文件夹移入回收站。

    Returns:
        (True, "") 成功； (False, 错误信息) 失败（文件保留，不物理删除）
    """
    p = Path(path)
    if not p.exists():
        return False, f"路径不存在: {path}"
    if os.name != "nt":
        return False, "非 Windows 平台暂不支持回收站删除"

    abs_path = os.path.abspath(str(p))
    buf = ctypes.create_unicode_buffer(abs_path + "\x00", len(abs_path) + 2)  # 双空结尾
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res == 0 and not op.fAnyOperationsAborted:
        return True, ""
    # 兜底审计：操作报错但文件已不存在（如被其它进程/系统移走）→ 仍按成功记录，保证留痕
    if not p.exists():
        return True, "文件已不存在(可能已被移动/删除，已记录留痕)"
    return False, f"SHFileOperation 失败 code={res}, aborted={op.fAnyOperationsAborted}"


# ── 删除日志（留痕，供追溯/恢复指引）──────────
class DeleteJournal:
    """记录已移入回收站的文件，便于核对与恢复指引。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS deleted(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL,
                    size_bytes INTEGER,
                    reason TEXT DEFAULT '',
                    deleted_at TEXT NOT NULL
                )"""
            )

    def record(self, path: str, size_bytes: int | None = None, reason: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO deleted(path, size_bytes, reason, deleted_at) VALUES(?,?,?,?)",
                (path, size_bytes, reason, datetime.now().isoformat(timespec="seconds")),
            )

    def all(self) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM deleted ORDER BY id DESC")]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM deleted").fetchone()[0]

    def total_size(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COALESCE(SUM(size_bytes),0) FROM deleted").fetchone()[0]
