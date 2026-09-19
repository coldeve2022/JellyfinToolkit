"""无界面自动化流水线入口。

用法示例：
  # 只生成报告，不执行任何写入
  python auto_pipeline.py --report report.json

  # 对 ready 分集组执行无损合并（原分集不移动）
  python auto_pipeline.py --apply-merge

  # 指定 ffmpeg/ffprobe
  python auto_pipeline.py --ffmpeg <ffmpeg 可执行文件> --ffprobe <ffprobe 可执行文件>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ToolkitConfig
from automation.pipeline import resolve_tools, run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description="Jellyfin Toolkit 自动化联动流水线")
    parser.add_argument("--roots", nargs="*", default=None, help="手动指定视频目录/文件；缺省从 Jellyfin DB 采集")
    parser.add_argument("--apply-merge", action="store_true", help="执行 ready 分集合并（默认仅报告）")
    parser.add_argument("--check-old", action="store_true", help="检测历史合并是否损坏（不解码）")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", default="", help="ffprobe 可执行文件路径")
    parser.add_argument("--report", default="", help="把报告写为 JSON 文件")
    parser.add_argument("--pretty", action="store_true", help="控制台按多行打印报告")
    args = parser.parse_args()

    cfg = ToolkitConfig.load()
    ffmpeg, ffprobe = resolve_tools(cfg, args.ffmpeg, args.ffprobe)

    def _log(message: str) -> None:
        print(message, file=sys.stderr)

    if not ffmpeg:
        _log("警告：未找到 ffmpeg，合并执行与旧合并检测将跳过。")

    report = run_pipeline(
        cfg,
        roots=args.roots,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        apply_merge=args.apply_merge,
        check_old=args.check_old,
        log=_log,
    )

    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False))

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
