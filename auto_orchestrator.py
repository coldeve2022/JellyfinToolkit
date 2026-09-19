"""整体自动化编排器入口。

默认只做只读审计；每个写操作都需要显式开关：

  # 全库只读审计
  python auto_orchestrator.py --report report.json

  # 审计 + 分集合并 + 旧合并体检
  python auto_orchestrator.py --apply-merge --check-old

  # 更激进的清理（都会先进入回收站，不是物理删除）
  python auto_orchestrator.py --apply-dedup --apply-delete

  # Jellyfin 脏标签清洗（先备份再清洗）
  python auto_orchestrator.py --apply-jellyfin-clean
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import ToolkitConfig
from automation.orchestrator import run_orchestrator


def main() -> int:
    parser = argparse.ArgumentParser(description="Jellyfin Toolkit 整体自动化编排器")
    parser.add_argument("--roots", nargs="*", default=None, help="手动指定视频目录；缺省从 Jellyfin DB 采集")
    parser.add_argument("--ffmpeg", default="", help="ffmpeg 路径")
    parser.add_argument("--ffprobe", default="", help="ffprobe 路径")
    parser.add_argument("--scan-video", action="store_true", help="逐文件检测损坏/不兼容（CPU 较重）")
    parser.add_argument("--check-old", action="store_true", help="不解码检查历史合并")

    parser.add_argument("--apply-merge", action="store_true", help="合并 ready 分集组（原分集不移动）")
    parser.add_argument("--apply-nfo-fix", action="store_true", help="修正 NFO 与关联文件名")
    parser.add_argument("--nfo-dirs", nargs="*", default=[], help="NFO 修复目录列表")
    parser.add_argument("--apply-replace", action="store_true", help="将 restored 破解视频替换回原库")
    parser.add_argument("--restored", nargs="*", default=[], help="破解完成视频/目录")
    parser.add_argument("--library", nargs="*", default=[], help="原始媒体库视频/目录")
    parser.add_argument("--apply-dedup", action="store_true", help="把重复版本建议删除者移入回收站")
    parser.add_argument("--apply-delete", action="store_true", help="把低分未看作品移入回收站")
    parser.add_argument("--apply-jellyfin-clean", action="store_true", help="清洗 Jellyfin 脏标签")
    parser.add_argument("--no-refresh", action="store_true", help="Jellyfin 清洗后不触发全库刷新")

    parser.add_argument("--report", default="", help="报告输出 JSON 路径")
    parser.add_argument("--pretty", action="store_true", help="多行打印报告")
    args = parser.parse_args()

    cfg = ToolkitConfig.load()

    def _log(message: str) -> None:
        print(message, file=sys.stderr)

    options = {
        "roots": args.roots,
        "ffmpeg": args.ffmpeg,
        "ffprobe": args.ffprobe,
        "scan_video": args.scan_video,
        "check_old": args.check_old,
        "apply_merge": args.apply_merge,
        "apply_nfo_fix": args.apply_nfo_fix,
        "nfo_dirs": args.nfo_dirs,
        "apply_replace": args.apply_replace,
        "restored_paths": args.restored,
        "library_paths": args.library,
        "apply_dedup": args.apply_dedup,
        "apply_delete": args.apply_delete,
        "apply_jellyfin_clean": args.apply_jellyfin_clean,
        "refresh_library": not args.no_refresh,
    }

    report = run_orchestrator(cfg, options, log=_log)

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
