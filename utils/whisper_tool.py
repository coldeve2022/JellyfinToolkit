"""第三方「批量字幕生成」工具（Faster-Whisper / TransWithAI 打包版）的定位与调用。

为什么单独一个模块
------------------
这个功能**不像别的模块那样自带核心能力** —— 真正干活的是第三方发布的
``infer.exe``（faster-whisper + 自定义 VAD 的中文优化打包版）。
所以本模块只做三件事：

1. **找到它**：按「用户配置 → 环境变量 → 常见位置」的顺序定位，全程可自定义；
2. **问它会什么**：解析 ``infer.exe --help``，按**实际支持的参数**拼命令行，
   而不是把参数名写死 —— 实测这个包的选项在不同版本之间改过
   （``--language`` 根本不存在；模型参数是 ``--model_name_or_path`` 而不是
   ``--model``，后者只是 argparse 的**前缀缩写**碰巧能过）。写死参数名
   会在用户机器上直接报「未识别参数」，而这恰恰是最难自查的一类问题；
3. **说不清就直说**：找不到工具时返回可执行的指引（去哪下、放哪、怎么配），
   而不是抛一个看不懂的异常。

本仓库**不包含**任何第三方二进制，也不会替你下载它们。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "TOOL_NAME", "EXE_NAME", "HOMEPAGE", "MODEL_CHOICES",
    "candidate_dirs", "find_infer_exe", "probe", "build_command",
    "default_audio_suffixes", "prerequisites_hint", "describe_exe",
]

TOOL_NAME = "Faster-Whisper 批量字幕（TransWithAI 打包版）"
EXE_NAME = "infer.exe"
HOMEPAGE = "https://github.com/TransWithAI/Faster-Whisper-TransWithAI-ChickenRice"

#: 说明性常量：这些名字来自该工具的**文档**，不是硬编码逻辑。
#: 真正可用的取值以 ``probe()`` 读到的 ``--help`` 为准（用户目录里可能只有一部分）。
MODEL_CHOICES = ("", "base", "small", "medium", "large-v2", "large-v3")

#: 上游把模型放在与 exe 同级的 models/ 目录下；留空表示沿用引擎自带默认。
DEFAULT_MODEL_SUBDIR = "models"

_AUDIO_EXTS = ("mp3", "wav", "flac", "m4a", "aac", "ogg", "wma")
_SUB_FORMATS = ("srt", "vtt", "txt", "lrc")

_ENV_KEYS = ("JELLYFIN_TOOLKIT_WHISPER_DIR", "WHISPER_TOOL_DIR")

#: 目录名里出现这些词就认为"可能放着 infer.exe"。
#: 实测用户的目录叫 ``faster_whisper_transwithai_windows_cu122-chickenrice``，
#: 只认固定名字是找不到的。
SEARCH_KEYWORDS = ("whisper", "transwithai", "trans_with_ai")

#: 常见落点：让"把工具解压到某个盘"的用户不必手工填路径。
#: 这里只列**相对名**，由候选目录逻辑去各盘根拼，避免出现具体盘符。
_DROP_IN_NAMES = (
    "faster-whisper",
    "faster_whisper",
    "whisper",
    "Faster-Whisper-TransWithAI-ChickenRice",
    "faster_whisper_transwithai",
)


def _drives() -> list[Path]:
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


def candidate_dirs(configured: str = "", extra: Optional[list] = None) -> list[Path]:
    """按**优先级**给出可能放着 ``infer.exe`` 的目录。

    顺序：显式配置 → 环境变量 → 程序目录旁 → 各盘根下的常见名字。
    全部是"猜"，所以任何一个命中的目录里都必须真的有 ``infer.exe`` 才算数
    （见 :func:`find_infer_exe`）。
    """
    out: list[Path] = []

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

    # 程序目录旁边（用户常把工具解压到一起）
    try:
        here = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        base = here.parent
        for up in (base, base.parent):
            for name in ("bin", "tools"):
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


def _exe_in(directory: Path) -> Optional[Path]:
    """目录里是否有可用的 ``infer.exe``（也接受别名的 .exe）。"""
    if not directory or not directory.is_dir():
        return None
    direct = directory / EXE_NAME
    if direct.is_file():
        return direct
    # 解压出来多套一层目录是很常见的情况
    try:
        for child in directory.iterdir():
            if child.is_dir():
                nested = child / EXE_NAME
                if nested.is_file():
                    return nested
    except OSError:
        pass
    return None


def search_infer_exe(configured: str = "", extra_dirs: Optional[list] = None,
                     time_budget: float = 12.0, log=None) -> dict:
    """定位 ``infer.exe``，**连搜索过程一起返回**（界面要用它解释"搜了哪儿"）。

    顺序：显式配置 → 已知常见位置（快） → 按名称关键词在盘上浅层搜索（慢但管用）。

    最后一档是必需的：用户实际把它解压在
    ``…\\faster_whisper_transwithai_windows_cu122-chickenrice`` 这种带版本号与
    平台后缀的目录里，只认固定目录名的话必然找不到，用户看到的就是
    "点了自动检测只弹一个提示框"。
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


def find_infer_exe(configured: str = "", extra_dirs: Optional[list] = None) -> Optional[Path]:
    """定位 ``infer.exe``；找不到返回 ``None``（**不抛异常**）。

    ``configured`` 允许直接指向 exe 本身，也允许指向它所在的目录 ——
    用户在设置里两种填法都会出现，没必要让他们猜。
    """
    return search_infer_exe(configured, extra_dirs)["path"]


# ── 能力探测 ────────────────────────────────────────────────

_OPT_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9_\-]*)")


def parse_options(help_text: str) -> set:
    """从 ``--help`` 文本里抽出它真正认识的选项名。

    只认行首缩进的选项定义行，避免把说明文字里顺带提到的选项也算进来
    （上游帮助文本里就会引用别的参数名）。
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


def probe(infer_exe: Path, timeout: int = 60) -> dict:
    """问工具"你会什么"：读 ``--help``，返回它支持的选项集合。

    这是本模块的核心：**参数按探测结果决定**，不写死。
    探测失败（工具坏了 / 版本不认 ``--help``）时不抛异常，而是返回
    ``ok=False`` 让界面照常可用（退回到"只传必需参数"的最小命令）。
    """
    info = {"ok": False, "options": set(), "raw": "", "error": ""}
    if not infer_exe or not Path(infer_exe).is_file():
        info["error"] = f"找不到可执行文件：{infer_exe}"
        return info
    try:
        proc = subprocess.run(
            [str(infer_exe), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = f"无法运行 {EXE_NAME}：{e}"
        return info
    text = (proc.stdout or "") + (proc.stderr or "")
    info["raw"] = text
    options = parse_options(text)
    if not options:
        info["error"] = (
            f"{EXE_NAME} 没有输出版本/选项信息（退出码 {proc.returncode}）。"
            "可能不是预期的那个工具，或文件已损坏。")
        return info
    info["options"] = options
    info["ok"] = True
    return info


def _no_window_flags() -> int:
    """在 Windows 上不要弹控制台窗口（这些 exe 都是控制台程序）。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def default_audio_suffixes(video_extensions=None) -> str:
    """``--audio_suffixes`` 的默认值。

    刻意**复用主程序已有的视频扩展名配置**，而不是再抄一份常量 ——
    用户在「设置」里加了新扩展名时，字幕工具这边跟着生效才符合直觉。
    """
    exts = []
    for e in list(video_extensions or []) + list(_AUDIO_EXTS):
        e = str(e).strip().lstrip(".").lower()
        if e and e not in exts:
            exts.append(e)
    return ",".join(exts)


# ── 命令行构建 ──────────────────────────────────────────────

def build_command(
    infer_exe,
    media_path,
    *,
    options: Optional[set] = None,
    model: str = "",
    device: str = "",
    audio_suffixes: str = "",
    sub_formats: str = "srt",
    output_dir: str = "",
    overwrite: bool = True,
    language: str = "",
    compute_type: str = "",
    enable_batching: bool = False,
    batch_size: int = 0,
    max_batch_size: int = 0,
    merge_segments: Optional[bool] = None,
    merge_max_gap_ms: int = 0,
    merge_max_duration_ms: int = 0,
    vad_threshold: float = 0.0,
    generation_config: str = "",
) -> list:
    """拼出调用 ``infer.exe`` 的命令行（列表形式，不经过 shell）。

    ``options`` 是 :func:`probe` 的结果：

    - 传了（推荐）：**只有确实在支持列表里的参数才会被发出**，其余自动跳过，
      并能通过 :func:`skipped_arguments` 告诉用户跳了哪些。不把不认识的参数丢给
      命令行，因为 argparse 遇到未知参数会**直接以退出码 2 失败**，
      用户只会看到"跑不起来"而不知道为什么；
    - 传 None（还没探测过）：假设工具支持，把已提供的参数都发出去。
      这是"能用就行"的降级路径，所以界面上建议先点一下「探测能力」。
    """
    cmd = [str(infer_exe)]

    def has(name: str) -> bool:
        return options is None or name in options

    def add(flag: str, value) -> None:
        if value not in (None, "", 0, 0.0):
            cmd.append(f"{flag}={value}")

    # 模型：优先用 canonical 名，退而用缩写（都探测不到就不传，走引擎默认）
    if model:
        if has("--model_name_or_path"):
            add("--model_name_or_path", model)
        elif has("--model"):
            add("--model", model)
    if device and has("--device"):
        add("--device", device)
    if compute_type and has("--compute_type"):
        add("--compute_type", compute_type)
    if audio_suffixes and has("--audio_suffixes"):
        add("--audio_suffixes", audio_suffixes)
    if sub_formats and has("--sub_formats"):
        add("--sub_formats", sub_formats)
    if output_dir and has("--output_dir"):
        add("--output_dir", output_dir)
    if generation_config and has("--generation_config"):
        add("--generation_config", generation_config)
    if overwrite and has("--overwrite"):
        cmd.append("--overwrite")
    # 语言：**上游没有这个参数**（模型自带语言能力），探测不到就不传。
    if language and has("--language"):
        add("--language", language)
    if enable_batching and has("--enable_batching"):
        cmd.append("--enable_batching")
        if batch_size:
            add("--batch_size", int(batch_size))
        if max_batch_size:
            add("--max_batch_size", int(max_batch_size))
    if merge_segments is True and has("--merge_segments"):
        cmd.append("--merge_segments")
    elif merge_segments is False and has("--no_merge_segments"):
        cmd.append("--no_merge_segments")
    if has("--merge_max_gap_ms"):
        add("--merge_max_gap_ms", int(merge_max_gap_ms) if merge_max_gap_ms else 0)
    if has("--merge_max_duration_ms"):
        add("--merge_max_duration_ms", int(merge_max_duration_ms) if merge_max_duration_ms else 0)
    if has("--vad_threshold"):
        add("--vad_threshold", vad_threshold)

    cmd.append(str(media_path))
    return cmd


def skipped_arguments(requested: dict, options: Optional[set]) -> list:
    """列出"用户设了但该版本工具不支持、因此被跳过"的参数，供界面提示。

    静默丢参数是最坏的做法：用户会以为设置生效了。
    """
    if options is None:
        return []
    out = []
    for key, flag in (
        ("model", "--model_name_or_path"), ("device", "--device"),
        ("compute_type", "--compute_type"), ("audio_suffixes", "--audio_suffixes"),
        ("sub_formats", "--sub_formats"), ("output_dir", "--output_dir"),
        ("language", "--language"),
    ):
        if requested.get(key) and flag not in options:
            out.append(f"{key}（{flag}）")
    return out


# ── 面向用户的说明 ──────────────────────────────────────────

def describe_exe(infer_exe: Optional[Path]) -> str:
    """一句话说明当前状态，供界面直接显示。"""
    if not infer_exe:
        return "未找到 infer.exe —— 请在「设置 → 字幕生成工具」里指定它的位置（见下方「前置条件」）"
    try:
        size_mb = Path(infer_exe).stat().st_size / 1048576
    except OSError:
        size_mb = 0.0
    return f"{infer_exe}（{size_mb:.0f} MB）"


def prerequisites_hint() -> str:
    """找不到工具时给出的**可执行**指引（不是一句"请先安装"）。"""
    return (
        f"这个功能依赖第三方工具「{TOOL_NAME}」，它不在本仓库里（也不该在）。\n\n"
        f"1. 到上游下载：{HOMEPAGE}\n"
        "   （Release 页有打包好的压缩包，解压后目录里应该有 infer.exe 和 models/ 文件夹）\n"
        "2. 解压到任意目录（位置随意，只要本程序能找到它）\n"
        "3. 回到「设置 → 字幕生成工具」，把 infer.exe 的路径填进去（也可直接填它所在的目录）\n\n"
        "前置条件：\n"
        "· 想用 GPU 加速需要 NVIDIA 显卡 + 较新的驱动；显存不足可以选 CPU 或关掉批处理\n"
        "· 首次运行会因为杀软扫描而偏慢，属正常现象\n"
        "· 模型权重由该工具自带（models/ 目录），不需要另外下载"
    )


def describe_exit_code(code) -> str:
    """把 ``infer.exe`` 的退出码翻译成人话。

    这些码是实际踩出来的，直接抄给用户比"进程返回 3221226505"有用得多。
    """
    if code == 0:
        return ""
    if code == 3221226505:      # 0xC0000409 STATUS_STACK_BUFFER_OVERRUN
        return (
            "底层引擎发生未处理异常（Windows 0xC0000409）。"
            "字幕文件可能**仍已生成**，请先检查输出目录；"
            "若没有生成，可关闭批处理加速、把批处理大小调小，或改用 CPU 重试这个文件。")
    if code == 3221225477:      # 0xC0000005 访问冲突
        return "底层引擎访问冲突（0xC0000005）。通常是显存不足或驱动问题，可尝试降低批处理大小 / 关闭批处理 / 更新驱动。"
    if code == 1:
        return "底层引擎返回错误（Exit Code: 1），通常是文件读取失败或参数不被支持。"
    if code == 2:
        return "命令行参数不被该版本支持（Exit Code: 2）。请在「设置」里把对应项恢复为默认。"
    return f"底层引擎异常中断（Exit Code: {code}）"


def run_one(infer_exe, media_path, *, on_line=None, should_stop=None,
            timeout: int = 0, **kwargs) -> dict:
    """跑一次识别（``kwargs`` 原样交给 :func:`build_command`）。

    返回 ``utils.proc.run_streaming`` 的结果，另外补上 ``error`` 的人类可读说明。
    """
    from utils import proc as proc_utils

    options = kwargs.pop("options", None)
    cmd = build_command(infer_exe, media_path, options=options, **kwargs)
    result = proc_utils.run_streaming(
        cmd, on_line=on_line, should_stop=should_stop, timeout=timeout,
        extra_env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    if not result["error"] and result["code"] not in (0, None):
        result["error"] = describe_exit_code(result["code"])
    return result
