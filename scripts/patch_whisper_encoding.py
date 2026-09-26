"""命令行入口：给第三方字幕工具打「UTF-8 stdout」补丁（中文 Windows 上不打必崩）。

补丁逻辑本身在 ``utils/whisper_tool.py`` 里 —— 放在那儿是为了让 GUI 也能调用，
并且会被打进 exe（``scripts/`` 目录不会）。

    python scripts/patch_whisper_encoding.py --check
    python scripts/patch_whisper_encoding.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.whisper_tool import (  # noqa: E402
    find_infer_source, is_encoding_patched, patch_encoding,
)


def force_utf8_stdout() -> None:
    """本脚本自己的输出也别在 GBK/cp1252 控制台下崩。

    **不能依赖项目里的 utils.console** —— 以 ``python scripts/x.py`` 运行时
    ``sys.path[0]`` 是 ``scripts/``，那条 import 会失败；之前在 CI 上这么丢过。
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="给第三方字幕工具打 UTF-8 补丁（中文 Windows 必需）")
    ap.add_argument("--infer", default="", help="infer.exe 或其所在目录；留空则自动查找")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .orig 备份")
    ap.add_argument("--check", action="store_true", help="只检查是否已打补丁")
    args = ap.parse_args()

    force_utf8_stdout()

    if args.check:
        src = find_infer_source(args.infer)
        if not src:
            print("❌ 没找到 infer.exe。先用「自动检测」定位工具，或用 --infer 指定。")
            return 1
        print(f"目标文件：{src}")
        patched = is_encoding_patched(src)
        print("状态：" + ("✅ 已打补丁" if patched else "❌ 未打补丁（中文系统上会崩）"))
        return 0 if patched else 1

    changed, note, src = patch_encoding(args.infer or None, backup=not args.no_backup)
    if src:
        print(f"目标文件：{src}")
    print(("✅ " if changed else "ℹ️ ") + note)
    if changed:
        print("\n下一步：回程序里点「探测能力」或直接开始生成字幕。")
        print("注意：第三方工具重新解压/升级后需要再跑一次本脚本。")
    return 0 if src else 1


if __name__ == "__main__":
    raise SystemExit(main())
