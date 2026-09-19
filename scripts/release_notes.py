"""生成 Release 的正文：**把本次更新内容直接写进去**，而不是只丢一个链接。

为什么需要
----------
Release 页面是用户第一眼看到的东西。如果正文只有「完整变更见 CHANGELOG.md」，
用户得再点一次才能知道这版改了什么 —— 多一步，而且多数人不会点。

所以这里从 ``CHANGELOG.md`` 里**抽取当前版本那一节**，连同安装说明一起拼成正文，
再由 release workflow 通过 ``body_path`` 交给 GitHub。

约定
----
``CHANGELOG.md`` 里每个版本以 ``## [x.y.z] — 日期`` 开头（方括号里的版本号是关键字），
到下一个 ``## [`` 之前为止。抽取时**沿用原文**，不重新组织 —— 保证 Release 与
CHANGELOG 永远一致，不会出现"两处各写一份、时间一长就对不上"的情况。

用法
----
    python scripts/release_notes.py --version 3.8.0 --out release_body.md
    python scripts/release_notes.py --version 3.8.0 --print
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

__all__ = ["extract_section", "build_body", "VERSION_HEAD_RE"]


def _force_utf8_stdout() -> None:
    """把 stdout/stderr 切到 UTF-8（仅在被管道/重定向时）。

    **这里刻意不去 import ``utils.console``**：以 ``python scripts/xxx.py`` 方式运行时
    ``sys.path[0]`` 是 ``scripts/`` 而不是仓库根，那条 import 会失败 ——
    实测就这么在 CI 上把编码修复静默丢掉了，后面打印中文时直接
    ``UnicodeEncodeError: 'charmap' codec can't encode``（runner 的 stdout 是 cp1252）。
    所以这个脚本保持**自包含**，顺便也让它能被单独拷出去用。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if getattr(stream, "isatty", lambda: False)():
                continue
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "_")
            if enc in {"utf8", "utf_8", "u8", "cp65001"}:
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue

#: 版本小节标题：``## [3.8.0] — 2026-09-19`` / ``## [3.8.0]`` / ``## 3.8.0``
VERSION_HEAD_RE = re.compile(r"^##\s+\[?(\d+(?:\.\d+)*)\]?", re.M)

INSTALL_BLOCK = """## 安装

1. 下载下面的 `JellyfinToolkit-*.zip`
2. 解压到任意目录
3. 双击 `JellyfinToolkit.exe`

> 首次运行前建议核对压缩包的 SHA256（附件里有 `.sha256` 文件）。

## 系统要求

- Windows 10/11（64 位）
- FFmpeg / ffprobe（可选，但视频修复、转码、分集合并需要）
  下载后放到程序目录的 `bin/` 子目录，或在「设置 → FFmpeg 转码设置」里指定路径

## 数据位置

`%APPDATA%\\JellyfinToolkit\\` —— 想做成便携版，就在程序目录里新建一个空的 `portable.txt`。
"""


def extract_section(changelog_text: str, version: str) -> str:
    """从 CHANGELOG 文本里抽出 ``version`` 那一节（不含版本标题行）。

    找不到返回空字符串 —— 调用方据此退回"只给链接"，而不是让整个发布失败。
    """
    want = str(version).strip().lstrip("vV")
    heads = list(VERSION_HEAD_RE.finditer(changelog_text or ""))
    for i, m in enumerate(heads):
        if m.group(1) != want:
            continue
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(changelog_text)
        # 去掉紧随标题行的那一行（通常是 "## [3.8.0] — 日期" 里的日期部分）
        body = changelog_text[start:end]
        body = re.sub(r"^\s*[—\-–]?\s*\d{4}-\d{2}-\d{2}\s*\n", "", body)
        return body.strip("\n").rstrip() + "\n"
    return ""


def build_body(version: str, changelog_text: str, repo_url: str = "") -> str:
    """拼出完整的 Release 正文：本次更新 → 安装 → 系统要求 → 数据位置。"""
    tag = "v" + str(version).strip().lstrip("vV")
    section = extract_section(changelog_text, version)

    parts = []
    if section:
        parts.append(f"## 本次更新（{tag}）\n\n{section}")
    else:
        parts.append(f"## 本次更新（{tag}）\n\n"
                     "本次未在 CHANGELOG.md 找到对应小节。")

    parts.append(INSTALL_BLOCK.rstrip())
    if repo_url:
        parts.append(f"完整历史见 [CHANGELOG.md]({repo_url}/blob/main/CHANGELOG.md)。")
    return "\n\n---\n\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 Release 正文（含本次更新内容）")
    ap.add_argument("--version", required=True, help="版本号，如 3.8.0")
    ap.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    ap.add_argument("--repo-url", default="https://github.com/coldeve2022/JellyfinToolkit")
    ap.add_argument("--out", default="", help="写入文件；留空则打印到标准输出")
    args = ap.parse_args()

    # 必须在任何 print 之前：CI 的 Windows runner 上 stdout 是 cp1252
    _force_utf8_stdout()

    path = Path(args.changelog)
    if not path.is_file():
        print(f"找不到 {path}", file=sys.stderr)
        return 1
    body = build_body(args.version, path.read_text(encoding="utf-8"), args.repo_url)

    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        section = extract_section(path.read_text(encoding="utf-8"), args.version)
        print(f"已写入 {args.out}"
              f"（本次更新部分 {len(section.splitlines())} 行，正文共 "
              f"{len(body.splitlines())} 行）")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
