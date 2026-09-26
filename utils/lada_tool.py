"""第三方「马赛克破解」工具（Lada）的定位、能力探测与调用。

为什么单独一个模块
------------------
和字幕工具一样，真正干活的是第三方发布的 ``lada-cli.exe``。本模块只负责：

1. **找到它**：用户配置 → 环境变量 → 程序目录旁 → 各盘常见名字，全程可自定义；
2. **问它会什么**：跑 ``--list-encoding-presets`` / ``--list-mosaic-detection-models``
   / ``--list-mosaic-restoration-models`` / ``--list-devices`` / ``--help``，
   把**这台机器上真正可用**的预设、模型、设备读出来喂给界面。
   这样用户看到的下拉项都是真实存在的，不会选了之后才发现名字不对；
3. **说不清就直说**：找不到工具时给出可执行的指引（去哪下、怎么放、前置条件）。

本仓库**不包含**任何第三方二进制，也不会替你下载它们。

关于 ``model_weights``
----------------------
Lada 的 Windows 发行包里**自带模型权重**（解压后在 ``_internal/model_weights/``），
用户不需要另外下载模型。所以这里只做"探测有哪些"，不做"帮你下模型"。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "TOOL_NAME", "EXE_NAME", "HOMEPAGE", "DOWNLOAD_PAGE",
    "candidate_dirs", "find_lada_cli", "probe", "parse_table",
    "build_command", "prerequisites_hint", "describe_cli", "OUTPUT_PATTERN",
]

TOOL_NAME = "Lada（马赛克破解）"
EXE_NAME = "lada-cli.exe"
#: 主仓库在 Codeberg，GitHub 只是镜像 —— 下载页要用主仓库那个。
HOMEPAGE = "https://codeberg.org/ladaapp/lada"
DOWNLOAD_PAGE = "https://codeberg.org/ladaapp/lada/releases"
MIRROR = "https://github.com/ladaapp/lada"

#: 与 Lada 自己的默认一致（用了它才能让输出文件名可预期）。
OUTPUT_PATTERN = "{orig_file_name}.restored.mp4"

_ENV_KEYS = ("JELLYFIN_TOOLKIT_LADA_DIR", "LADA_CLI", "LADA_HOME")

#: 目录名含这些词就认为"可能放着 lada-cli.exe"（用户实际叫 lada-v0.11.0_windows_nvidia）
SEARCH_KEYWORDS = ("lada",)

#: 常见落点。只列**相对名**，由候选目录逻辑去各盘根拼，源码里不出现具体盘符。
_DROP_IN_NAMES = (
    "lada",
    "lada-windows",
    "lada-nvidia",
    "ladaapp",
)

_OPT_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9_\-]*)")


def _drives() -> list:
    """所有存在的盘根（Windows）/ 常见挂载点（其它平台）。"""
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


def candidate_dirs(configured: str = "", extra: Optional[list] = None) -> list:
    """按优先级给出可能放着 ``lada-cli.exe`` 的目录（都是"猜"，命中才作数）。"""
    out: list = []

    def add(p) -> None:
        if not p:
            return
        try:
            q = Path(p).expanduser()
        except (OSError, ValueError):
            return
        if q not in out:
            out.append(q)

    add(configured)
    for key in _ENV_KEYS:
        add(os.environ.get(key, ""))
    try:
        here = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        base = here.parent
        for up in (base, base.parent, base.parent.parent):
            for name in ("lada", "tools", "bin"):
                add(up / name)
    except (OSError, ValueError):
        pass
    for root in _drives():
        for name in _DROP_IN_NAMES:
            add(root / name)
            add(root / "tools" / name)
    for p in (extra or []):
        add(p)
    return out


def _cli_in(directory: Path) -> Optional[Path]:
    """目录里是否有可用的 ``lada-cli.exe``（含多套一层目录的常见情况）。"""
    if not directory or not directory.is_dir():
        return None
    direct = directory / EXE_NAME
    if direct.is_file():
        return direct
    try:
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                nested = child / EXE_NAME
                if nested.is_file():
                    return nested
    except OSError:
        pass
    return None


def search_lada_cli(configured: str = "", extra_dirs: Optional[list] = None,
                    time_budget: float = 12.0, log=None) -> dict:
    """定位 ``lada-cli.exe``，**连搜索过程一起返回**。

    最后一档按名称关键词在盘上浅层搜索 —— 用户的目录叫
    ``lada-v0.11.0_windows_nvidia``，只认固定名字同样找不到。
    """
    from utils import tool_search

    if configured:
        hit = tool_search.exe_in_dir(Path(configured.strip().strip('"')), (EXE_NAME,))
        if hit:
            return {"path": hit, "source": "设置里指定的位置", "scanned": 0,
                    "elapsed": 0.0, "stopped_early": False}
    for d in candidate_dirs("", extra=extra_dirs):
        hit = tool_search.exe_in_dir(d, (EXE_NAME,))
        if hit:
            return {"path": hit, "source": f"已知常见位置：{d}", "scanned": 0,
                    "elapsed": 0.0, "stopped_early": False}
    return tool_search.find_executable(
        (EXE_NAME,), SEARCH_KEYWORDS, extra_dirs=extra_dirs,
        time_budget=time_budget, log=log)


def find_lada_cli(configured: str = "", extra_dirs: Optional[list] = None) -> Optional[Path]:
    """定位 ``lada-cli.exe``；找不到返回 ``None``。

    接受三种填法：exe 本身、它所在目录、Lada 解压后的顶层目录。
    """
    return search_lada_cli(configured, extra_dirs)["path"]


# ── 能力探测 ────────────────────────────────────────────────

def parse_table(text: str) -> list:
    """解析 Lada 输出的对齐表格，返回 ``[{"name":..., "description":..., "path":...}, ...]``。

    实现要点：**用那行 ``-----`` 分隔线的列位置来切列**，而不是按"两个以上空格"切。
    后者在"描述为空"的行上会把路径挤到描述列里 —— 而 `v2` 这个模型正好就是
    描述为空的。用分隔线定界才能 100% 对位。
    """
    lines = [l.rstrip() for l in (text or "").splitlines()]
    sep_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if s and set(s) <= {"-", " "} and "-" in s:
            sep_idx = i
            break
    if sep_idx < 0:
        return []
    sep = lines[sep_idx]
    # 分隔线的连续 '-' 段 = 各列的范围
    spans = [(m.start(), m.end()) for m in re.finditer(r"-+", sep)]
    if not spans:
        return []
    rows = []
    for line in lines[sep_idx + 1:]:
        if not line.strip():
            continue
        cells = []
        for start, end in spans:
            cells.append(line[start:end].strip() if start < len(line) else "")
        cells += [""] * (3 - len(cells))
        if not cells[0]:
            continue
        rows.append({
            "name": cells[0],
            "description": cells[1] if len(cells) > 1 else "",
            "path": cells[2] if len(cells) > 2 else "",
        })
    return rows


def _no_window_flags() -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # 低优先级：探测不该和用户正在跑的破解任务抢 CPU/GPU
    flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    return flags


def _run(cli: Path, args: list, timeout: int) -> tuple:
    """跑一次 lada-cli，返回 (退出码, 合并后的输出文本)。"""
    try:
        proc = subprocess.run(
            [str(cli), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return -1, f"{type(e).__name__}: {e}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def parse_options(help_text: str) -> set:
    """从 ``--help`` 文本里抽出 Lada 真正认识的选项名。

    只认行首缩进的选项定义行，避免把说明文字里顺带提到的参数也算进来
    （Lada 的帮助文本里会引用 ``--list-devices`` 之类的其它参数）。
    """
    found = set()
    for line in (help_text or "").splitlines():
        if not line[:1].isspace():
            continue
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        for name in _OPT_RE.findall(stripped.split("  ")[0]):
            found.add(name)
    return found


def probe(cli: Path, timeout: int = 120) -> dict:
    """问 Lada "你会什么"：预设、检测模型、修复模型、设备、以及支持的命令行选项。

    每次调用都要起一次 ``lada-cli.exe``（它会加载 ultralytics，约 1-3 秒），
    所以**调用方应该放在后台线程里**，不要卡住界面。

    任何一步失败都只影响那一项，不会让整个探测失败 —— 用户至少还能看到
    "工具在、但读不出预设"这种有用的状态。
    """
    info = {
        "ok": False, "error": "", "options": set(),
        "presets": [], "detection_models": [], "restoration_models": [], "devices": [],
        "raw": {},
    }
    if not cli or not Path(cli).is_file():
        info["error"] = f"找不到可执行文件：{cli}"
        return info

    rc, text = _run(cli, ["--help"], timeout)
    info["raw"]["help"] = text
    info["options"] = parse_options(text)

    for key, args in (
        ("presets", ["--list-encoding-presets"]),
        ("detection_models", ["--list-mosaic-detection-models"]),
        ("restoration_models", ["--list-mosaic-restoration-models"]),
        ("devices", ["--list-devices"]),
    ):
        _rc, out = _run(cli, args, timeout)
        info["raw"][key] = out
        info[key] = parse_table(out)

    if info["options"]:
        info["ok"] = True
    elif not info["presets"] and not info["devices"]:
        # 全都读不出来才算真失败；把最有信息量的那行错误带给用户
        tail = ""
        lines = [l for l in (text or "").splitlines() if l.strip()]
        if lines:
            tail = lines[-1][:200]
        info["error"] = (
            f"{EXE_NAME} 无法正常响应（--help 退出码 {rc}）。"
            "常见原因：文件不完整、被杀软拦截、或 HOME/USERPROFILE 环境变量缺失。\n"
            f"原始输出：{tail or '(空)'}"
        )
    return info


# ── 命令行构建 ──────────────────────────────────────────────

def build_command(
    cli,
    input_path,
    output_dir,
    *,
    options: Optional[set] = None,
    device: str = "cuda:0",
    encoding_preset: str = "",
    detection_model: str = "",
    restoration_model: str = "",
    max_clip_length: int = 0,
    fp16: bool = True,
    temporary_dir: str = "",
    detect_face_mosaics: Optional[bool] = None,
    mp4_fast_start: bool = False,
    output_pattern: str = OUTPUT_PATTERN,
) -> list:
    """拼出调用 ``lada-cli`` 的命令行（列表形式，不经过 shell）。

    ``options`` 为 :func:`probe` 的结果；不支持的参数会被**自动跳过**，
    避免 argparse 因未知参数直接失败（用户只会看到"跑不起来"）。
    空字符串/0 表示"不指定"，交给 Lada 用它自己的默认值。
    """
    cmd = [str(cli)]
    if options is None or "--input" in options:
        cmd += ["--input", str(input_path)]
    if options is None or "--output" in options:
        cmd += ["--output", str(output_dir)]
    if device and (options is None or "--device" in options):
        cmd += ["--device", device]
    if encoding_preset and (options is None or "--encoding-preset" in options):
        cmd += ["--encoding-preset", encoding_preset]
    if detection_model and (options is None or "--mosaic-detection-model" in options):
        cmd += ["--mosaic-detection-model", detection_model]
    if restoration_model and (options is None or "--mosaic-restoration-model" in options):
        cmd += ["--mosaic-restoration-model", restoration_model]
    if max_clip_length and (options is None or "--max-clip-length" in options):
        cmd += ["--max-clip-length", str(int(max_clip_length))]
    if temporary_dir and (options is None or "--temporary-directory" in options):
        cmd += ["--temporary-directory", str(temporary_dir)]
    if output_pattern and (options is None or "--output-file-pattern" in options):
        cmd += ["--output-file-pattern", output_pattern]

    if fp16 and (options is None or "--fp16" in options):
        cmd.append("--fp16")
    elif not fp16 and (options is None or "--no-fp16" in options):
        cmd.append("--no-fp16")
    if detect_face_mosaics is not None:
        if detect_face_mosaics and (options is None or "--detect-face-mosaics" in options):
            cmd.append("--detect-face-mosaics")
        elif not detect_face_mosaics and (options is None or "--no-detect-face-mosaics" in options):
            cmd.append("--no-detect-face-mosaics")
    if mp4_fast_start and (options is None or "--mp4-fast-start" in options):
        cmd.append("--mp4-fast-start")
    return cmd


def expected_output(output_dir, input_path) -> Path:
    """按 :data:`OUTPUT_PATTERN` 推出这次会生成哪个文件（用于校验产物）。

    与 Lada 的默认命名规则一致，所以用户换成自定义 pattern 时要一起改 ——
    这里只提供默认值，界面允许覆盖。
    """
    stem = Path(str(input_path)).stem
    return Path(str(output_dir)) / OUTPUT_PATTERN.replace("{orig_file_name}", stem)


_PROGRESS_RE = re.compile(r"Processing video:\s*(\d+)\s*%")


def parse_progress(line: str) -> Optional[int]:
    """从 Lada 的输出行里解析进度百分比（解析不出返回 ``None``）。

    上游只在处理视频时按 ``Processing video: NN%`` 输出，所以这是**唯一**的
    进度来源；解析不出来不代表出错（准备阶段本来就没进度）。
    """
    m = _PROGRESS_RE.search(line or "")
    if not m:
        return None
    try:
        return max(0, min(100, int(m.group(1))))
    except ValueError:
        return None


def validate_output_timing(input_path, output_path, ffprobe: str = "ffprobe",
                           tolerance_ratio: float = 0.05, min_tolerance: float = 2.0,
                           timeout: int = 60) -> tuple:
    """校验产出视频的时长是否与输入相符，返回 ``(是否可信, 说明)``。

    **为什么需要**：Lada 偶尔会以退出码 0 结束、却没写出可用文件（或写出时间轴
    明显不对的文件）。只看退出码会把这种"静默失败"当成成功，用户后面才发现
    文件是坏的 —— 那时代码已经跑了几十分钟。用 ffprobe 对一下时长是最便宜的兜底。

    读不出元数据时倾向"放行"（返回 True），因为校验本身失败不等于产物有问题。
    """
    import json
    import subprocess

    def _video_duration(path):
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries",
                 "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                creationflags=(0x08000000 if os.name == "nt" else 0),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"{type(e).__name__}: {e}"
        if proc.returncode != 0:
            return None, "ffprobe 读取失败"
        try:
            meta = json.loads(proc.stdout or "{}")
        except ValueError:
            return None, "ffprobe 输出无法解析"
        for stream in meta.get("streams", []):
            if stream.get("codec_type") == "video":
                d = stream.get("duration")
                if d not in (None, "N/A"):
                    try:
                        return float(d), ""
                    except (TypeError, ValueError):
                        continue
        fmt = meta.get("format", {}).get("duration")
        if fmt not in (None, "N/A"):
            try:
                return float(fmt), ""
            except (TypeError, ValueError):
                pass
        return None, "没有可用的时长信息"

    out_dur, err = _video_duration(output_path)
    if out_dur is None or out_dur <= 0:
        return False, f"输出文件没有有效时长（{err or '未知原因'}）"
    in_dur, _err = _video_duration(input_path)
    if in_dur is None or in_dur <= 0:
        return True, "输入时长读不到，跳过对比"
    tolerance = max(min_tolerance, in_dur * tolerance_ratio)
    if abs(out_dur - in_dur) > tolerance:
        return False, f"输出时长异常：{out_dur:.1f}s（输入 {in_dur:.1f}s，容差 {tolerance:.1f}s）"
    return True, ""


def run_one(cli, input_path, output_dir, *, on_line=None, progress=None,
            should_stop=None, timeout: int = 0, cpu_affinity=None,
            **kwargs) -> dict:
    """跑一次破解（``kwargs`` 原样交给 :func:`build_command`）。

    ``on_line`` 收原始输出行；``progress`` 收解析出来的百分比（0~100），
    两者可以同时给。
    """
    from utils import proc as proc_utils

    options = kwargs.pop("options", None)
    cmd = build_command(cli, input_path, output_dir, options=options, **kwargs)

    def _on_line(line: str) -> None:
        if on_line:
            on_line(line)
        if progress:
            pct = parse_progress(line)
            if pct is not None:
                progress(pct)

    result = proc_utils.run_streaming(
        cmd, on_line=_on_line, should_stop=should_stop, timeout=timeout,
        cpu_affinity=cpu_affinity,
    )
    result["cmd"] = cmd
    return result


# ── 面向用户的说明 ──────────────────────────────────────────

def describe_cli(cli: Optional[Path]) -> str:
    """一句话说明当前状态，供界面直接显示。"""
    if not cli:
        return "未找到 lada-cli.exe —— 请在「设置 → 马赛克破解（Lada）」里指定它的位置"
    try:
        size_mb = Path(cli).stat().st_size / 1048576
    except OSError:
        size_mb = 0.0
    return f"{cli}（{size_mb:.0f} MB）"


def prerequisites_hint() -> str:
    """找不到工具时给出的**可执行**指引。"""
    return (
        f"这个功能依赖第三方工具「{TOOL_NAME}」，它不在本仓库里（也不该在）。\n\n"
        f"1. 到官方发布页下载 Windows 包：{DOWNLOAD_PAGE}\n"
        f"   （项目主页在 Codeberg：{HOMEPAGE}；GitHub 只是镜像 {MIRROR}）\n"
        "2. 用 7-Zip 解压（发行包是 .7z），解压后会看到 lada.exe 与 lada-cli.exe\n"
        "3. 回到「设置 → 马赛克破解（Lada）」，指向 lada-cli.exe 或它所在目录\n\n"
        "前置条件：\n"
        "· 需要 NVIDIA 显卡（Turing 及更新：RTX 20 系 ~ 50 系）或 Intel Arc，显存 4-6GB 起\n"
        "· 1080p 建议 6-8GB 内存；4K 需要明显更多\n"
        "· 请把显卡驱动更新到较新版本\n"
        "· **模型权重由发行包自带**，不需要另外下载\n"
        "· 首次启动会因为杀软扫描而明显偏慢，属正常现象\n"
        "· 官方建议下载后核对 Release 公告里的 SHA256"
    )
